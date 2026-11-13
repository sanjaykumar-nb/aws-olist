#!/usr/bin/env bash
# One-shot setup / update for the Olist source API on an EC2 instance.
# Supports Amazon Linux 2023 and Ubuntu 22.04/24.04. Safe to re-run: it pulls the
# latest code and rebuilds, keeping your .env and the database volume.
#
#   curl -fsSL https://raw.githubusercontent.com/sanjaykumar-nb/aws-olist/main/deploy/ec2-setup.sh -o ec2-setup.sh
#   sudo DATASET_S3_URI=s3://your-bucket/olist/ bash ec2-setup.sh
#
# Without DATASET_S3_URI, copy the 9 CSVs to /opt/olist/dataset first (e.g. with scp).
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/sanjaykumar-nb/aws-olist.git}"
BRANCH="${BRANCH:-main}"
BASE_DIR="${BASE_DIR:-/opt/olist}"
APP_DIR="$BASE_DIR/app"
DATA_DIR="$BASE_DIR/dataset"
DATASET_S3_URI="${DATASET_S3_URI:-}"
COMPOSE_VERSION="v2.29.7"
REQUIRED_FILES=(
  olist_customers_dataset.csv
  olist_geolocation_dataset.csv
  olist_order_items_dataset.csv
  olist_order_payments_dataset.csv
  olist_order_reviews_dataset.csv
  olist_orders_dataset.csv
  olist_products_dataset.csv
  olist_sellers_dataset.csv
  product_category_name_translation.csv
)

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo"
. /etc/os-release

# ---------------------------------------------------------------- packages
install_pkgs() {
  case "$ID" in
    amzn) dnf install -y "$@" ;;
    ubuntu|debian) DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" ;;
    *) die "unsupported OS '$ID' (use Amazon Linux 2023 or Ubuntu)" ;;
  esac
}

log "Installing Docker and git"
case "$ID" in
  amzn) DOCKER_PKG=docker ;;
  ubuntu|debian) DOCKER_PKG=docker.io; apt-get update -y ;;
  *) die "unsupported OS '$ID' (use Amazon Linux 2023 or Ubuntu)" ;;
esac
command -v git >/dev/null || install_pkgs git
command -v docker >/dev/null || install_pkgs "$DOCKER_PKG"
command -v openssl >/dev/null || install_pkgs openssl
systemctl enable --now docker

if ! docker compose version >/dev/null 2>&1; then
  log "Installing docker compose plugin $COMPOSE_VERSION"
  mkdir -p /usr/local/lib/docker/cli-plugins
  curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-$(uname -m)" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
  chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi
docker compose version

# let the login user (ec2-user / ubuntu) run docker without sudo on next login
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
  usermod -aG docker "$SUDO_USER" || true
fi

# ---------------------------------------------------------------- code
log "Fetching code into $APP_DIR"
mkdir -p "$BASE_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  git -C "$APP_DIR" checkout --quiet "$BRANCH"
  git -C "$APP_DIR" pull --ff-only --quiet origin "$BRANCH"
else
  git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
git -C "$APP_DIR" log -1 --format='    at %h %s'

# ---------------------------------------------------------------- dataset
mkdir -p "$DATA_DIR"
if [ -n "$DATASET_S3_URI" ]; then
  log "Syncing CSVs from $DATASET_S3_URI"
  if ! command -v aws >/dev/null; then
    case "$ID" in
      amzn) install_pkgs awscli-2 ;;
      *) snap install aws-cli --classic ;;
    esac
  fi
  aws s3 sync "$DATASET_S3_URI" "$DATA_DIR" --exclude "*" --include "*.csv" --only-show-errors
fi

missing=()
for f in "${REQUIRED_FILES[@]}"; do [ -s "$DATA_DIR/$f" ] || missing+=("$f"); done
if [ ${#missing[@]} -gt 0 ]; then
  die "missing in $DATA_DIR: ${missing[*]}
Upload them, e.g. from your machine:
  scp -i key.pem /path/to/dataset/*.csv ec2-user@<public-ip>:/tmp/ && sudo mv /tmp/*.csv $DATA_DIR/
or re-run with DATASET_S3_URI=s3://bucket/prefix/ (instance role needs s3:ListBucket + s3:GetObject)."
fi
chmod 755 "$DATA_DIR"; chmod 644 "$DATA_DIR"/*.csv
echo "    all 9 CSVs present"

# ---------------------------------------------------------------- .env
cd "$APP_DIR"
set_env() { # key value: replace the line or append it
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}
if [ ! -f .env ]; then
  log "Creating .env (API key generated, stream autostart on)"
  cp .env.example .env
  set_env OLIST_DATASET_HOST_DIR "$DATA_DIR"
  set_env OLIST_API_KEY "$(openssl rand -hex 24)"
  set_env OLIST_AUTOSTART true
  chmod 600 .env
else
  log "Keeping existing .env"
fi

# ---------------------------------------------------------------- run
log "Building and starting the container"
docker compose up -d --build

port="$(grep -E '^OLIST_PORT=' .env | cut -d= -f2)"; port="${port:-8000}"
printf '    waiting for health'
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${port}/api/v1/health" >/dev/null 2>&1; then ok=1; break; fi
  printf '.'; sleep 3
done
echo
[ "${ok:-}" = 1 ] || die "API did not become healthy; check: docker compose -f $APP_DIR/docker-compose.yml logs"

token="$(curl -fsS -m 2 -X PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null || true)"
ip="$(curl -fsS -m 2 -H "X-aws-ec2-metadata-token: $token" \
  http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo '<public-ip>')"

log "Done"
cat <<EOF
    UI:        http://$ip:$port/
    Health:    http://$ip:$port/api/v1/health
    API key:   sudo grep OLIST_API_KEY $APP_DIR/.env
    Logs:      cd $APP_DIR && sudo docker compose logs -f
    Update:    sudo bash $0   (pulls main, rebuilds, keeps .env and data)

    Make sure the security group allows TCP $port only from your IP / your pipeline.
EOF
