"use strict";

const API = "/api/v1";
const $ = (id) => document.getElementById(id);

const TYPES = {
  customer_created: { label: "Customer created", color: "#b39cf0" },
  order_created: { label: "Order created", color: "#8ea2ff" },
  payment_received: { label: "Payment received", color: "#5cc8d6" },
  order_approved: { label: "Order approved", color: "#7fb0ff" },
  order_shipped: { label: "Order shipped", color: "#e0b469" },
  order_delivered: { label: "Order delivered", color: "#3ecf8e" },
  order_canceled: { label: "Order canceled", color: "#f2707a" },
  review_submitted: { label: "Review submitted", color: "#e58cc0" },
  customer_address_changed: { label: "Address changed", color: "#e89a6a" },
};
const typeMeta = (t) => TYPES[t] || { label: t, color: "#9ba1ad" };

const state = {
  view: "overview",
  datasets: [],
  running: null, // unknown until the first status poll
  q: { ds: "orders", cursors: [null], page: 0, next: null },
  feed: { paused: false, type: "", selected: null, events: [] },
  buckets: new Array(120).fill(0),
  tick: 0,
};

/* ---------------- utils ---------------- */
const esc = (v) =>
  String(v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (n ?? 0).toLocaleString("en-US");
// server timestamps carry microseconds; trim to ms so every browser parses them
const parseTs = (s) => new Date(String(s).replace(/(\.\d{3})\d*Z$/, "$1Z"));
const timeOf = (s) => parseTs(s).toLocaleTimeString("en-GB", { hour12: false });

function toast(msg, isErr = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "show" + (isErr ? " err" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.className = ""), 2400);
}

async function api(path, opts = {}) {
  const r = await fetch(API + path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const body = await r.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {}
    throw new Error(detail);
  }
  return r.json();
}

async function copy(text, what = "Copied") {
  try {
    await navigator.clipboard.writeText(text);
    toast(what);
  } catch {
    toast(text);
  }
}

/* ---------------- routing ---------------- */
const TITLES = { overview: "Overview", data: "Data", stream: "Event stream", api: "API reference" };
function route() {
  const view = (location.hash || "#overview").slice(1).split("?")[0];
  state.view = TITLES[view] ? view : "overview";
  document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== `view-${state.view}`));
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === state.view));
  $("view-title").textContent = TITLES[state.view];
  if (state.view === "overview") drawChart();
}
window.addEventListener("hashchange", route);

/* ---------------- connection ---------------- */
async function checkHealth() {
  const el = $("conn");
  try {
    await api("/health");
    el.className = "conn ok";
    $("conn-text").textContent = "API connected";
  } catch {
    el.className = "conn err";
    $("conn-text").textContent = "API unreachable";
  }
}

/* ---------------- simulator control ---------------- */
function renderSim(s) {
  if (s.running !== state.running) {
    state.running = s.running;
    const btn = $("sim-btn");
    btn.className = "btn " + (s.running ? "btn-danger" : "btn-primary");
    btn.innerHTML = s.running
      ? `<svg><use href="#i-stop"/></svg><span>Stop stream</span>`
      : `<svg><use href="#i-play"/></svg><span>Start stream</span>`;
    btn.setAttribute("aria-label", s.running ? "Stop stream" : "Start stream");
    $("sim-badge").className = "badge" + (s.running ? " live" : "");
    $("sim-badge-text").textContent = s.running ? "Live" : "Stopped";
    if (!state.feed.events.length) renderFeed();
  }
  if (document.activeElement !== $("rate")) $("rate").value = s.orders_per_sec;

  if (s.total_events != null) {
    $("k-events").textContent = fmt(s.total_events);
    $("k-events-sub").textContent = `last id ${s.last_event_id ?? "–"}`;
    $("k-inflight").textContent = `${fmt(s.inflight_orders)} orders in flight`;
  }
  $("run-started").textContent = s.running && s.started_at ? `since ${timeOf(s.started_at)}` : "not running";
  renderTypeBars(s.events_this_run || {});
  if (s.last_error && s.last_error !== state.lastError) toast("Simulator: " + s.last_error, true);
  state.lastError = s.last_error;
}

function renderTypeBars(counts) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  if (!entries.length) {
    $("type-bars").innerHTML = `<div class="empty">Start the stream to see a breakdown by event type.</div>`;
    return;
  }
  const max = entries[0][1];
  $("type-bars").innerHTML = entries
    .map(([t, n]) => {
      const m = typeMeta(t);
      return `<div class="type-row">
        <span class="type-name"><span class="tdot" style="background:${m.color}"></span>${m.label}</span>
        <span class="bar"><i style="width:${(n / max) * 100}%;background:${m.color}"></i></span>
        <span class="type-count">${fmt(n)}</span></div>`;
    })
    .join("");
}

async function pollSim() {
  try {
    renderSim(await api("/stream/status"));
  } catch {}
}

$("sim-btn").addEventListener("click", async () => {
  const btn = $("sim-btn");
  btn.disabled = true;
  try {
    const rate = clampRate();
    const s = state.running
      ? await api("/stream/stop", { method: "POST" })
      : await api("/stream/start", { method: "POST", body: JSON.stringify({ orders_per_sec: rate }) });
    renderSim(s);
    toast(s.running ? `Stream started at ${s.orders_per_sec} orders/s` : "Stream stopped");
    pollSim();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
});

function clampRate() {
  let v = parseFloat($("rate").value);
  if (!isFinite(v) || v <= 0) v = 1;
  v = Math.min(50, Math.max(0.1, v));
  $("rate").value = v;
  return v;
}

$("rate").addEventListener("change", async () => {
  const v = clampRate();
  try {
    await api("/stream/rate", { method: "POST", body: JSON.stringify({ orders_per_sec: v }) });
    toast(`Rate set to ${v} orders/s`);
  } catch (e) {
    toast(e.message, true);
  }
});

/* ---------------- datasets (overview) ---------------- */
async function loadDatasets() {
  let datasets;
  try {
    ({ datasets } = await api("/datasets"));
  } catch {
    return;
  }
  const first = !state.datasets.length;
  state.datasets = datasets;
  const total = datasets.reduce((a, d) => a + d.row_count, 0);
  const sim = datasets.reduce((a, d) => a + d.simulated_row_count, 0);
  $("k-rows").textContent = fmt(total);
  $("k-sim").textContent = fmt(sim);
  $("ds-body").innerHTML = datasets
    .map(
      (d) => `<tr>
      <td>${d.name}</td>
      <td class="num">${fmt(d.row_count)}</td>
      <td class="num ${d.simulated_row_count ? "pos" : "zero"}">${d.simulated_row_count ? "+" + fmt(d.simulated_row_count) : "0"}</td>
      <td class="mono">${d.last_updated_at ? d.last_updated_at.replace("T", " ").slice(0, 19) : "–"}</td>
      <td class="mono">${d.source_file}</td>
      <td><div class="row-actions">
        <button class="btn btn-sm" data-browse="${d.name}" type="button">Browse</button>
        <a class="btn btn-sm" href="${API}/export/${d.name}.csv" download><svg><use href="#i-download"/></svg>CSV</a>
      </div></td></tr>`
    )
    .join("");
  $("ds-refreshed").textContent = "refreshed " + new Date().toLocaleTimeString("en-GB", { hour12: false });

  if (first) {
    $("q-ds").innerHTML = datasets.map((d) => `<option value="${d.name}">${d.name}</option>`).join("");
    selectDataset(state.q.ds);
  }
}

$("ds-body").addEventListener("click", (e) => {
  const b = e.target.closest("[data-browse]");
  if (!b) return;
  selectDataset(b.dataset.browse);
  location.hash = "#data";
});

/* ---------------- data explorer ---------------- */
function selectDataset(name) {
  const d = state.datasets.find((x) => x.name === name);
  if (!d) return;
  state.q.ds = name;
  $("q-ds").value = name;
  $("q-col").innerHTML = `<option value="">None</option>` + d.columns.map((c) => `<option>${c.name}</option>`).join("");
  $("q-val").value = "";
  runQuery(true);
}

function queryParams(withPaging) {
  const p = new URLSearchParams();
  const col = $("q-col").value, val = $("q-val").value.trim(), since = $("q-since").value.trim();
  if (col && val) p.set(col, val);
  if (since) p.set("updated_since", since);
  if (withPaging) {
    p.set("limit", $("q-limit").value);
    const c = state.q.cursors[state.q.page];
    if (c != null) p.set("cursor", c);
  }
  return p;
}

function syncRequestUrls() {
  const qs = queryParams(true).toString();
  $("q-url").innerHTML = `<b>GET</b> ${esc(`${API}/data/${state.q.ds}?${qs}`)}`;
  const csv = queryParams(false).toString();
  $("q-csv").href = `${API}/export/${state.q.ds}.csv${csv ? "?" + csv : ""}`;
}

async function runQuery(reset) {
  if (reset) {
    state.q.cursors = [null];
    state.q.page = 0;
  }
  syncRequestUrls();
  const url = `/data/${state.q.ds}?${queryParams(true)}`;
  $("q-run").disabled = true;
  try {
    const res = await api(url);
    state.q.next = res.next_cursor;
    renderTable(res.data);
    const size = +$("q-limit").value;
    const start = state.q.page * size;
    $("q-info").textContent = res.count
      ? `Rows ${fmt(start + 1)}–${fmt(start + res.count)}${res.next_cursor == null ? " · end of results" : ""}`
      : "No rows match this query";
  } catch (e) {
    state.q.next = null;
    $("q-table").innerHTML = "";
    $("q-info").textContent = "";
    toast(e.message, true);
  } finally {
    $("q-run").disabled = false;
    $("q-page").textContent = `Page ${state.q.page + 1}`;
    $("q-prev").disabled = state.q.page === 0;
    $("q-next").disabled = state.q.next == null;
  }
}

function renderTable(rows) {
  if (!rows.length) {
    $("q-table").innerHTML = `<tbody><tr><td class="null">No rows</td></tr></tbody>`;
    return;
  }
  const cols = Object.keys(rows[0]);
  $("q-table").innerHTML =
    `<thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>` +
    rows
      .map(
        (r) =>
          `<tr>${cols
            .map((c) => (r[c] == null ? `<td class="null">null</td>` : `<td title="${esc(r[c])}">${esc(r[c])}</td>`))
            .join("")}</tr>`
      )
      .join("") +
    `</tbody>`;
}

$("q-ds").addEventListener("change", (e) => selectDataset(e.target.value));
$("q-run").addEventListener("click", () => runQuery(true));
$("q-limit").addEventListener("change", () => runQuery(true));
$("q-col").addEventListener("change", syncRequestUrls);
["q-val", "q-since"].forEach((id) => {
  $(id).addEventListener("input", syncRequestUrls);
  $(id).addEventListener("keydown", (e) => e.key === "Enter" && runQuery(true));
});
$("q-next").addEventListener("click", () => {
  if (state.q.next == null) return;
  state.q.cursors[state.q.page + 1] = state.q.next;
  state.q.page++;
  runQuery(false);
});
$("q-prev").addEventListener("click", () => {
  if (state.q.page === 0) return;
  state.q.page--;
  runQuery(false);
});
$("q-copy").addEventListener("click", () =>
  copy(`${location.origin}${API}/data/${state.q.ds}?${queryParams(true)}`, "Request URL copied")
);

/* ---------------- event stream ---------------- */
$("f-type").innerHTML += Object.entries(TYPES).map(([k, m]) => `<option value="${k}">${m.label}</option>`).join("");
$("f-type").addEventListener("change", (e) => {
  state.feed.type = e.target.value;
  renderFeed();
});
$("f-pause").addEventListener("click", () => {
  state.feed.paused = !state.feed.paused;
  $("f-pause").innerHTML = state.feed.paused
    ? `<svg><use href="#i-play"/></svg><span>Resume</span>`
    : `<svg><use href="#i-pause"/></svg><span>Pause</span>`;
  if (!state.feed.paused) renderFeed();
});
$("f-clear").addEventListener("click", () => {
  state.feed.events = [];
  renderFeed();
});

function summary(ev) {
  const p = ev.payload;
  const brl = (v) => "R$ " + Number(v).toFixed(2);
  switch (ev.event_type) {
    case "order_created":
      return `${p.items.length} item${p.items.length > 1 ? "s" : ""} · ${brl(p.items.reduce((a, i) => a + i.price + i.freight_value, 0))}`;
    case "payment_received":
      return `${p.payment_type.replace("_", " ")} · ${p.payment_installments}x · ${brl(p.payment_value)}`;
    case "review_submitted":
      return `${p.review_score}/5${p.review_comment_title ? " · " + p.review_comment_title : ""}`;
    case "customer_address_changed":
      return `${p.before.customer_city}, ${p.before.customer_state} → ${p.after.customer_city}, ${p.after.customer_state}`;
    case "customer_created":
      return `${p.customer_city}, ${p.customer_state}`;
    default:
      return `status → ${p.order_status}`;
  }
}

function feedRow(ev, isNew) {
  const m = typeMeta(ev.event_type);
  const sel = state.feed.selected === ev.event_id ? " selected" : "";
  return `<li class="${isNew ? "new" : ""}${sel}" data-id="${ev.event_id}">
    <span class="eid">#${ev.event_id}</span>
    <span class="etype"><span class="tdot" style="background:${m.color}"></span>${m.label}</span>
    <span class="eent">${esc(ev.entity)} ${esc(ev.entity_id.slice(0, 6))}</span>
    <span class="esum">${esc(summary(ev))}</span>
    <span class="etime">${timeOf(ev.event_time)}</span></li>`;
}

const visible = (ev) => !state.feed.type || ev.event_type === state.feed.type;

function renderFeed() {
  const rows = state.feed.events.filter(visible);
  $("feed").innerHTML = rows.length
    ? rows.map((e) => feedRow(e, false)).join("")
    : `<li class="empty">${state.running ? "Waiting for events…" : "No events yet. Start the stream to see live activity."}</li>`;
}

function onEvent(ev) {
  state.buckets[state.buckets.length - 1]++;
  state.feed.events.unshift(ev);
  if (state.feed.events.length > 400) state.feed.events.length = 400;
  if (state.feed.paused || !visible(ev)) return;
  const list = $("feed");
  list.querySelector(".empty")?.remove();
  const readingOlder = list.scrollTop > 4; // user scrolled down: keep their rows in place
  list.insertAdjacentHTML("afterbegin", feedRow(ev, true));
  if (readingOlder) list.scrollTop += list.firstElementChild.offsetHeight;
  while (list.children.length > 400) list.lastElementChild.remove();
}

function highlight(obj) {
  return esc(JSON.stringify(obj, null, 2)).replace(
    /(&quot;(?:[^&]|&(?!quot;))*?&quot;)(\s*:)?|\b(-?\d+(?:\.\d+)?)\b|\b(null|true|false)\b/g,
    (m, str, colon, num, lit) =>
      str ? `<span class="${colon ? "k" : "s"}">${str}</span>${colon || ""}` : num ? `<span class="n">${num}</span>` : `<span class="z">${lit}</span>`
  );
}

$("feed").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-id]");
  if (!li) return;
  const ev = state.feed.events.find((x) => x.event_id === +li.dataset.id);
  if (!ev) return;
  state.feed.selected = ev.event_id;
  $("feed").querySelectorAll("li.selected").forEach((x) => x.classList.remove("selected"));
  li.classList.add("selected");
  $("detail").innerHTML = highlight(ev);
  $("d-copy").disabled = false;
});
$("d-copy").addEventListener("click", () => {
  const ev = state.feed.events.find((x) => x.event_id === state.feed.selected);
  if (ev) copy(JSON.stringify(ev, null, 2), "Event JSON copied");
});

function connectStream() {
  const es = new EventSource(`${API}/stream/events`);
  es.onmessage = (m) => onEvent(JSON.parse(m.data));
  // EventSource reconnects on its own and resumes via Last-Event-ID
}

/* ---------------- throughput chart ---------------- */
function drawChart() {
  const c = $("chart");
  if (!c.offsetParent) return; // hidden view
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  c.width = w * dpr;
  c.height = h * dpr;
  const ctx = c.getContext("2d");
  ctx.scale(dpr, dpr);
  const data = state.buckets;
  const peak = Math.max(...data);
  const step = peak <= 5 ? 1 : peak <= 20 ? 5 : peak <= 50 ? 10 : peak <= 200 ? 50 : 100;
  const top = Math.max(step * 4, Math.ceil(peak / step) * step);
  const padL = 34, padB = 20, padT = 6;
  const pw = w - padL, ph = h - padB - padT;
  const x = (i) => padL + (i / (data.length - 1)) * pw;
  const y = (v) => padT + ph - (v / top) * ph;

  ctx.font = "11px Inter, sans-serif";
  ctx.fillStyle = "#646b78";
  ctx.strokeStyle = "rgba(255,255,255,0.06)";
  ctx.lineWidth = 1;
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let v = 0; v <= top; v += top / 4) {
    const yy = Math.round(y(v)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(padL, yy);
    ctx.lineTo(w, yy);
    ctx.stroke();
    ctx.fillText(String(Math.round(v)), padL - 8, yy);
  }
  ctx.textBaseline = "top";
  [["−2m", 0], ["−1m", 60], ["now", data.length - 1]].forEach(([t, i], k) => {
    ctx.textAlign = k === 0 ? "left" : k === 2 ? "right" : "center";
    ctx.fillText(t, x(i), h - padB + 6);
  });

  const grad = ctx.createLinearGradient(0, padT, 0, padT + ph);
  grad.addColorStop(0, "rgba(142,162,255,0.25)");
  grad.addColorStop(1, "rgba(142,162,255,0)");
  ctx.beginPath();
  data.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
  ctx.lineTo(x(data.length - 1), y(0));
  ctx.lineTo(x(0), y(0));
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();
  ctx.beginPath();
  data.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
  ctx.strokeStyle = "#8ea2ff";
  ctx.lineWidth = 1.5;
  ctx.lineJoin = "round";
  ctx.stroke();
}
window.addEventListener("resize", drawChart);

setInterval(() => {
  // last complete second drives the KPI; then open a new bucket
  const lastFull = state.buckets[state.buckets.length - 1];
  $("k-eps").innerHTML = `${fmt(lastFull)}<small> ev/s</small>`;
  state.buckets.push(0);
  state.buckets.shift();
  drawChart();
}, 1000);

/* ---------------- API reference ---------------- */
const ENDPOINTS = [
  ["Meta", "GET", "/health", "Liveness check and server time"],
  ["Meta", "GET", "/datasets", "Datasets with row counts, columns and last update"],
  ["Batch", "GET", "/data/orders?limit=500", "JSON rows; follow next_cursor to page"],
  ["Batch", "GET", "/data/orders?order_status=delivered", "Any column works as an equality filter"],
  ["Batch", "GET", "/data/orders?updated_since=2026-11-13T00:00:00Z", "Incremental pull; save the returned watermark"],
  ["Batch", "GET", "/export/orders.csv", "Full table as a streamed CSV file"],
  ["Batch", "GET", "/export/orders.csv?updated_since=2026-11-13T00:00:00Z", "Incremental CSV; watermark in X-Watermark header"],
  ["Stream", "POST", "/stream/start", "Start the simulator, optional orders_per_sec"],
  ["Stream", "POST", "/stream/stop", "Stop the simulator"],
  ["Stream", "POST", "/stream/rate", "Change orders_per_sec while running"],
  ["Stream", "GET", "/stream/status", "Running state and counters"],
  ["Stream", "GET", "/events?after_id=0&limit=500", "Pull the event log from an offset"],
  ["Stream", "GET", "/stream/events", "Server-Sent Events push feed, resumable"],
];
function curlFor(m, p) {
  const url = `${location.origin}${API}${p}`;
  if (m === "POST") return `curl -X POST "${url}" -H "Content-Type: application/json" -d '{"orders_per_sec": 2}'`;
  if (p.startsWith("/export")) return `curl -o ${p.split("/")[2].split("?")[0]} "${url}"`;
  if (p === "/stream/events") return `curl -N "${url}"`;
  return `curl "${url}"`;
}
let lastGroup = "";
$("endpoints").innerHTML = ENDPOINTS.map(([g, m, p, d], i) => {
  const head = g !== lastGroup ? `<div class="ep-group">${g}</div>` : "";
  lastGroup = g;
  return `${head}<div class="ep"><span class="method ${m}">${m}</span><code>${esc(p)}</code>
    <span class="desc">${esc(d)}</span>
    <button class="icon-btn" data-ep="${i}" type="button" title="Copy curl" aria-label="Copy curl command"><svg><use href="#i-copy"/></svg></button></div>`;
}).join("");
$("endpoints").addEventListener("click", (e) => {
  const b = e.target.closest("[data-ep]");
  if (!b) return;
  const [, m, p] = ENDPOINTS[+b.dataset.ep];
  copy(curlFor(m, p), "curl command copied");
});

/* ---------------- boot ---------------- */
route();
renderFeed();
renderTypeBars({});
checkHealth();
pollSim();
loadDatasets();
connectStream();
setInterval(checkHealth, 10000);
setInterval(pollSim, 1500);
setInterval(loadDatasets, 5000);
