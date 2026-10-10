/* Garage controller — iPhone home-screen app for the garage Bose SoundTouch.
 * Round controller (rotary volume ring, 6 radio presets with preview, power)
 * + YouTube Music: For you / Genres / Search / Queue.
 * All data from the server is written with textContent / setAttribute — never
 * innerHTML — because titles come from YouTube. */
"use strict";

const TURN_PCT = 60;      // volume % per full turn of the ring
const SEND_MS = 250;      // max one /api/volume per 250 ms while turning
const GAUGE_START = -135, GAUGE_SWEEP = 270;
const NS = "http://www.w3.org/2000/svg";

const $ = (id) => document.getElementById(id);
const S = { garage: null, status: null, hover: null, drag: null, pending: null, ytm: null };

/* ------------------------------------------------------------------ api */
async function api(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await r.json(); } catch (_) { /* empty body */ }
  if (!r.ok) throw new Error((data && data.detail) || `${r.status}`);
  return data;
}
let toastTimer;
function toast(msg, err) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast show" + (err ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), 2600);
}
async function act(promise, okMsg) {
  try { const r = await promise; if (okMsg) toast(okMsg); return r; }
  catch (e) { toast(e.message || String(e), true); }
}
function el(tag, attrs = {}, text) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v; else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  if (text != null) n.textContent = text;
  return n;
}
function img(src) {
  if (!src) return el("div", { class: "noimg" });
  return el("img", { src, alt: "", loading: "lazy", referrerpolicy: "no-referrer" });
}
const fmtDur = (s) => (s ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : "");

/* ----------------------------------------------------------- controller */
const pt = (r, deg) => { const a = (deg * Math.PI) / 180; return [300 + r * Math.sin(a), 300 - r * Math.cos(a)]; };
const arc = (r, a, b) => { const [x1, y1] = pt(r, a), [x2, y2] = pt(r, b);
  return `M${x1.toFixed(1)} ${y1.toFixed(1)} A${r} ${r} 0 ${b - a > 180 ? 1 : 0} 1 ${x2.toFixed(1)} ${y2.toFixed(1)}`; };
const sector = (r1, r2, a, b) => { const [p, q] = [pt(r2, a), pt(r2, b)], [c, d] = [pt(r1, b), pt(r1, a)];
  return `M${p[0]} ${p[1]} A${r2} ${r2} 0 0 1 ${q[0]} ${q[1]} L${c[0]} ${c[1]} A${r1} ${r1} 0 0 0 ${d[0]} ${d[1]} Z`; };
function svg(tag, attrs = {}, parent) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
}

const C = {};
function buildController() {
  const s = $("ctl");
  s.innerHTML = `<defs>
    <radialGradient id="shell" cx="42%" cy="32%" r="75%"><stop offset="0" stop-color="#3a3d42"/><stop offset=".45" stop-color="#16181b"/><stop offset="1" stop-color="#050506"/></radialGradient>
    <radialGradient id="bezel" cx="45%" cy="35%" r="70%"><stop offset="0" stop-color="#2b2e33"/><stop offset="1" stop-color="#0b0c0e"/></radialGradient>
    <linearGradient id="chrome" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f4f6f8"/><stop offset=".35" stop-color="#9aa1a8"/><stop offset=".55" stop-color="#e6e9ec"/><stop offset=".8" stop-color="#6d737a"/><stop offset="1" stop-color="#cfd3d7"/></linearGradient>
    <radialGradient id="ringfill" cx="50%" cy="40%" r="60%"><stop offset="0" stop-color="#26292d"/><stop offset="1" stop-color="#0e0f11"/></radialGradient>
    <radialGradient id="disp" cx="50%" cy="35%" r="70%"><stop offset="0" stop-color="#15171a"/><stop offset="1" stop-color="#000"/></radialGradient>
    <linearGradient id="gloss" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".16"/><stop offset=".5" stop-color="#fff" stop-opacity="0"/></linearGradient>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="0" dy="10" stdDeviation="12" flood-color="#000" flood-opacity=".45"/></filter>
    <filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
  </defs>`;
  svg("circle", { cx: 300, cy: 300, r: 290, fill: "url(#shell)", filter: "url(#shadow)" }, s);
  C.ring = svg("circle", { class: "ring", cx: 300, cy: 300, r: 270, fill: "url(#bezel)", stroke: "#000", "stroke-width": 2 }, s);
  svg("path", { class: "gauge-bg", d: arc(242, GAUGE_START, GAUGE_START + GAUGE_SWEEP), "pointer-events": "none" }, s);
  C.gauge = svg("path", { class: "gauge", "pointer-events": "none" }, s);
  C.knob = svg("circle", { class: "knob", r: 7, "pointer-events": "none" }, s);
  const [dx, dy] = pt(242, GAUGE_START - 17), [ux, uy] = pt(242, GAUGE_START + GAUGE_SWEEP + 17);
  svg("text", { class: "key", x: dx, y: dy }, s).textContent = "−";
  svg("text", { class: "key", x: ux, y: uy }, s).textContent = "+";
  s.lastChild.previousSibling.addEventListener("click", () => step(-5));
  s.lastChild.addEventListener("click", () => step(5));
  const lbl = svg("text", { x: 300, y: 551, "text-anchor": "middle", "pointer-events": "none",
    style: "font:700 22px -apple-system,Helvetica,sans-serif;letter-spacing:7px;fill:#d9dcdf;opacity:.85" }, s);
  lbl.textContent = "GARAGE";
  svg("circle", { cx: 300, cy: 300, r: 213, fill: "url(#chrome)", "pointer-events": "none" }, s);
  svg("circle", { cx: 300, cy: 300, r: 206, fill: "url(#ringfill)", "pointer-events": "none" }, s);
  C.nums = []; C.segs = [];
  for (let i = 0; i < 6; i++) {
    const c = i * 60;
    const seg = svg("path", { class: "seg", d: sector(152, 204, c - 28, c + 28) }, s);
    seg.addEventListener("click", () => playPreset(i + 1));
    seg.addEventListener("pointerenter", () => { S.hover = i; renderDisplay(); });
    seg.addEventListener("pointerleave", () => { if (S.hover === i) { S.hover = null; renderDisplay(); } });
    const [a1, b1] = pt(150, c + 30), [a2, b2] = pt(206, c + 30);
    svg("line", { x1: a1, y1: b1, x2: a2, y2: b2, stroke: "#000", "stroke-width": 5, "stroke-linecap": "round", "pointer-events": "none" }, s);
    const [nx, ny] = pt(177, c);
    const num = svg("text", { class: "num", x: nx, y: ny }, s);
    num.textContent = String(i + 1);
    C.nums.push(num); C.segs.push(seg);
  }
  svg("circle", { cx: 300, cy: 300, r: 150, fill: "#000", "pointer-events": "none" }, s);
  svg("circle", { cx: 300, cy: 300, r: 146, fill: "url(#disp)", stroke: "#2a2d31", "stroke-width": 1.5 }, s);
  const pw = svg("g", { transform: "translate(300 214)", style: "cursor:pointer" }, s);
  svg("circle", { r: 22, fill: "transparent" }, pw);
  svg("path", { d: "M-7.1 -7.1 A10 10 0 1 0 7.1 -7.1", fill: "none", stroke: "#9aa0a6", "stroke-width": 2.6, "stroke-linecap": "round" }, pw);
  svg("line", { x1: 0, y1: -13, x2: 0, y2: -2, stroke: "#9aa0a6", "stroke-width": 2.6, "stroke-linecap": "round" }, pw);
  pw.addEventListener("click", () => act(api("/api/garage/power", { method: "POST" }), "Music off").then(refresh));
  C.l1 = svg("text", { class: "l1", x: 300, y: 292, "pointer-events": "none" }, s);
  C.l2 = svg("text", { class: "l2", x: 300, y: 322, "pointer-events": "none" }, s);
  C.l3 = svg("text", { class: "l3", x: 300, y: 362, "pointer-events": "none" }, s);
  svg("ellipse", { cx: 300, cy: 190, rx: 235, ry: 140, fill: "url(#gloss)", "pointer-events": "none" }, s);

  C.ring.addEventListener("pointerdown", dragStart);
  C.ring.addEventListener("pointermove", dragMove);
  C.ring.addEventListener("pointerup", dragEnd);
  C.ring.addEventListener("pointercancel", dragEnd);
  C.ring.addEventListener("wheel", (e) => { e.preventDefault(); step(e.deltaY < 0 ? 2 : -2); }, { passive: false });
}

const fit = (t, n) => (t && t.length > n ? t.slice(0, n - 1) + "…" : t || "");
function isOff() { const g = S.garage; return !g || !g.connected || g.standby; }

function renderDisplay() {
  const g = S.garage || {};
  const names = g.presets || [];
  C.nums.forEach((n, i) => { const on = g.preset === i + 1; n.classList.toggle("on", on); C.segs[i].classList.toggle("on", on); });
  $("ctl").classList.toggle("off", isOff());
  [C.l1, C.l2, C.l3].forEach((t) => t.classList.remove("preview", "live"));
  if (S.hover != null && S.drag == null) {           // preview what a preset plays
    C.l1.textContent = fit(names[S.hover] || `Preset ${S.hover + 1}`, 20);
    C.l2.textContent = g.preset === S.hover + 1 ? "Now playing" : `Preset ${S.hover + 1} · tap to play`;
    C.l3.textContent = "";
    C.l1.classList.add("preview"); C.l2.classList.add("preview");
    return;
  }
  if (!g.connected) {
    C.l1.textContent = "Not connected"; C.l2.textContent = g.active_device ? `bulb_voice is on ${fit(g.active_device, 18)}` : "";
    C.l3.textContent = "Tap a preset to switch";
  } else if (g.standby) {
    C.l1.textContent = "Standby"; C.l2.textContent = "Tap a preset or a song"; C.l3.textContent = "";
  } else if (g.preset) {
    C.l1.textContent = fit(names[g.preset - 1], 20); C.l2.textContent = `Preset ${g.preset}`;
  } else if (g.track) {
    C.l1.textContent = fit(g.track.title, 22); C.l2.textContent = fit(g.track.artist, 26);
  } else {
    C.l1.textContent = g.source === "AIRPLAY" ? "AirPlay" : g.source ? g.source[0] + g.source.slice(1).toLowerCase() : "Ready";
    C.l2.textContent = "";
  }
  if (S.drag == null && S.pending == null) showVolume(isOff() ? null : g.volume, false);
}

function showVolume(v, live) {
  C.l3.classList.toggle("live", !!live);
  if (S.hover == null || live) C.l3.textContent = v == null ? "" : live ? `Volume ${v}%` : `${v}%`;
  const pct = v == null ? 0 : Math.max(0, Math.min(100, v)) / 100;
  const end = GAUGE_START + GAUGE_SWEEP * pct;
  C.gauge.setAttribute("d", pct > 0.004 ? arc(242, GAUGE_START, end) : "");
  const [kx, ky] = pt(242, end);
  C.knob.setAttribute("cx", kx); C.knob.setAttribute("cy", ky);
}

function sendVolume(v) { return api("/api/volume", { method: "POST", body: { level: Math.round(v) } }); }
function step(d) {
  if (isOff()) return;
  const v = Math.max(0, Math.min(100, (S.pending ?? S.garage.volume ?? 25) + d));
  S.pending = v; showVolume(v, true);
  clearTimeout(S.stepTimer);
  S.stepTimer = setTimeout(async () => { await act(sendVolume(v)); S.garage.volume = v; S.pending = null; renderDisplay(); }, 300);
}
function angle(e) {
  const r = $("ctl").getBoundingClientRect();
  const x = ((e.clientX - r.left) / r.width) * 600 - 300, y = ((e.clientY - r.top) / r.height) * 600 - 300;
  return { deg: (Math.atan2(x, -y) * 180) / Math.PI, rad: Math.hypot(x, y) };
}
function dragStart(e) {
  if (isOff()) return;
  const a = angle(e);
  if (a.rad < 213) return;
  try { C.ring.setPointerCapture(e.pointerId); } catch (_) { /* optional */ }
  S.drag = { last: a.deg, value: S.garage.volume ?? 25, sent: 0, lastStep: null };
  showVolume(Math.round(S.drag.value), true);
}
function dragMove(e) {
  const d = S.drag; if (!d) return;
  const a = angle(e);
  let delta = a.deg - d.last;
  if (delta > 180) delta -= 360; if (delta < -180) delta += 360;
  d.last = a.deg;
  d.value = Math.max(0, Math.min(100, d.value + (delta / 360) * TURN_PCT));
  const v = Math.round(d.value);
  showVolume(v, true);
  if (d.lastStep !== v && navigator.vibrate && v % 2 === 0) navigator.vibrate(4);
  d.lastStep = v;
  if (Date.now() - d.sent > SEND_MS) { d.sent = Date.now(); sendVolume(v).catch(() => {}); }
}
async function dragEnd(e) {
  const d = S.drag; if (!d) return;
  S.drag = null;
  try { C.ring.releasePointerCapture(e.pointerId); } catch (_) { /* already released */ }
  const v = Math.round(d.value);
  await act(sendVolume(v));
  if (S.garage) S.garage.volume = v;
  renderDisplay();
}

async function playPreset(n) {
  S.hover = null;
  const names = (S.garage && S.garage.presets) || [];
  toast(`Starting ${names[n - 1] || "preset " + n}…`);
  await act(api(`/api/garage/preset/${n}`, { method: "POST" }));
  setTimeout(refresh, 1500);
}

/* ------------------------------------------------------------ transport */
$("t-prev").addEventListener("click", () => act(api("/api/prev", { method: "POST" })).then(refresh));
$("t-next").addEventListener("click", () => act(api("/api/next", { method: "POST" })).then(refresh));
$("t-play").addEventListener("click", async () => {
  const playing = S.garage && S.garage.transport === "PLAYING";
  await act(api(playing ? "/api/pause" : "/api/play", { method: "POST", body: playing ? undefined : {} }));
  refresh();
});

/* ------------------------------------------------------------- music UI */
async function enqueue(item, mode) {
  const body = { kind: item.kind, id: item.id, title: item.title || "", artist: item.subtitle || "",
                 duration: item.duration || 0, mode };
  const label = mode === "now" ? "Playing" : mode === "next" ? "Up next" : "Queued";
  const r = await act(api("/api/ytm/enqueue", { method: "POST", body }));
  if (!r) return;
  if (r.count) toast(`${label}: ${r.count > 1 ? r.count + " tracks" : fit(item.title, 40)}${r.count > 1 || mode === "now" ? " (downloading…)" : ""}`);
  else toast(r.jumped ? `Playing ${fit(item.title, 40)}` : "Already in the queue");
  setTimeout(refresh, 1200);
}

function itemRow(item) {
  const row = el("div", { class: "row" });
  row.append(img(item.thumbnail));
  const meta = el("div", { class: "meta" });
  meta.append(el("div", { class: "t" }, item.title));
  const kindLabel = { song: "", video: "Video", album: "Album", playlist: "Playlist", artist: "Artist" }[item.kind] || "";
  meta.append(el("div", { class: "s" }, [kindLabel, item.subtitle, fmtDur(item.duration)].filter(Boolean).join(" · ")));
  row.append(meta);
  if (item.kind === "artist") {
    row.append(el("button", { class: "act", type: "button", title: "Search this artist",
      onclick: () => { $("search-q").value = item.title; setKind("songs"); runSearch(); } }, "🔎"));
    return row;
  }
  row.append(el("button", { class: "act play", type: "button", title: "Play now", onclick: () => enqueue(item, "now") }, "▶"));
  row.append(el("button", { class: "act", type: "button", title: "Add to queue", onclick: () => enqueue(item, "end") }, "＋"));
  if (item.kind === "song" || item.kind === "video") {
    row.append(el("button", { class: "act", type: "button", title: "Radio: more like this",
      onclick: () => enqueue({ ...item, kind: "radio" }, "now") }, "📻"));
  }
  return row;
}

function card(item) {
  const b = el("button", { class: "card", type: "button", title: "Play now", onclick: () => enqueue(item, "now") });
  b.append(img(item.thumbnail), el("div", { class: "t" }, item.title), el("div", { class: "s" }, item.subtitle));
  return b;
}

function fill(container, nodes, emptyMsg) {
  container.replaceChildren(...(nodes.length ? nodes : [el("div", { class: "empty" }, emptyMsg)]));
}

async function loadForYou() {
  const p = $("tab-foryou");
  fill(p, [], "Loading…");
  const [home, lib] = await Promise.all([
    api("/api/ytm/home").catch((e) => ({ error: e.message, shelves: [] })),
    S.ytm && S.ytm.signed_in ? api("/api/ytm/library").catch(() => ({ items: [] })) : Promise.resolve({ items: [] }),
  ]);
  const nodes = [];
  for (const sh of home.shelves || []) {
    const sec = el("div", { class: "shelf" });
    sec.append(el("h3", {}, sh.title));
    const row = el("div", { class: "cards" });
    sh.items.forEach((it) => row.append(card(it)));
    sec.append(row);
    nodes.push(sec);
  }
  if ((lib.items || []).length) {
    const sec = el("div", { class: "shelf" });
    sec.append(el("h3", {}, "Your playlists"));
    const list = el("div", { class: "list" });
    lib.items.forEach((it) => list.append(itemRow(it)));
    sec.append(list);
    nodes.push(sec);
  }
  if (!S.ytm || !S.ytm.signed_in) {
    nodes.push(el("div", { class: "empty" }, "Not signed in to YouTube Music: personal mixes and your playlists appear after the one-time sign-in."));
  }
  fill(p, nodes, home.error ? `YouTube Music unavailable: ${home.error}` : "Nothing here yet.");
}

async function loadGenres() {
  const chips = $("genre-chips");
  if (chips.childElementCount) return;
  fill(chips, [], "Loading…");
  const r = await act(api("/api/ytm/moods"));
  if (!r) return;
  const nodes = [];
  for (const [section, cats] of Object.entries(r.sections)) {
    const mood = /mood/i.test(section);
    cats.forEach((c) => nodes.push(el("button", { class: "chip" + (mood ? " mood" : ""), type: "button",
      onclick: () => openGenre(c) }, c.title)));
  }
  fill(chips, nodes, "No genres.");
}
async function openGenre(c) {
  $("genre-chips").classList.add("hidden");
  $("genre-head").classList.remove("hidden");
  $("genre-title").textContent = c.title;
  fill($("genre-items"), [], "Loading…");
  const r = await act(api(`/api/ytm/mood?params=${encodeURIComponent(c.params)}`));
  fill($("genre-items"), r ? r.items.map(itemRow) : [], "Nothing found.");
}
$("genre-back").addEventListener("click", () => {
  $("genre-chips").classList.remove("hidden");
  $("genre-head").classList.add("hidden");
  $("genre-items").replaceChildren();
});

let searchKind = "songs";
function setKind(k) {
  searchKind = k;
  document.querySelectorAll("#search-kind button").forEach((b) => b.classList.toggle("on", b.dataset.kind === k));
}
document.querySelectorAll("#search-kind button").forEach((b) => b.addEventListener("click", () => { setKind(b.dataset.kind); runSearch(); }));
async function runSearch() {
  const q = $("search-q").value.trim();
  if (!q) return;
  fill($("search-results"), [], "Searching…");
  const r = await act(api(`/api/ytm/search?q=${encodeURIComponent(q)}&kind=${searchKind}`));
  fill($("search-results"), r ? r.results.map(itemRow) : [], "No results.");
}
$("search-form").addEventListener("submit", (e) => { e.preventDefault(); $("search-q").blur(); runSearch(); });

function renderQueue() {
  const st = S.status;
  if (!st) return;
  const nodes = st.queue.map((t, i) => {
    const row = el("div", { class: "row" + (i === st.current_index ? " current" : "") + (t.ready === false ? " pending" : "") });
    row.append(el("div", { class: "noimg" }));
    const meta = el("div", { class: "meta" });
    meta.append(el("div", { class: "t" }, t.title), el("div", { class: "s" },
      [t.artist, fmtDur(t.duration), t.ready === false ? "downloading…" : ""].filter(Boolean).join(" · ")));
    row.append(meta,
      el("button", { class: "act play", type: "button", title: "Play", onclick: async () => {
        await act(api("/api/play", { method: "POST", body: { track_id: t.id } })); refresh(); } }, "▶"),
      el("button", { class: "act", type: "button", title: "Remove", onclick: async () => {
        await act(api(`/api/queue/${encodeURIComponent(t.id)}`, { method: "DELETE" })); refresh(); } }, "✕"));
    return row;
  });
  fill($("queue-list"), nodes, "Queue is empty.");
}

/* ----------------------------------------------------------------- tabs */
const loaded = {};
function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  if (name === "foryou" && !loaded.foryou) { loaded.foryou = true; loadForYou(); }
  if (name === "genres") loadGenres();
  if (name === "queue") renderQueue();
  try { localStorage.setItem("garage.tab", name); } catch (_) { /* private mode */ }
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));

/* ------------------------------------------------------------- polling */
async function refresh() {
  try {
    S.garage = await api("/api/garage/state");
    const pill = $("device-pill");
    if (S.garage.connected) { pill.textContent = S.garage.device; pill.className = "pill ok"; }
    else { pill.textContent = `Connect ${S.garage.device}`; pill.className = "pill warn"; }
    if (S.drag == null) renderDisplay();
  } catch (e) { /* server restarting: keep last view */ }
  if ($("tab-queue").classList.contains("active")) {
    try { S.status = await api("/api/status"); renderQueue(); } catch (_) { /* ignore */ }
  }
}
$("device-pill").addEventListener("click", async () => {
  if (S.garage && !S.garage.connected) { await act(api("/api/garage/connect", { method: "POST" }), "Connected"); refresh(); }
});

async function init() {
  buildController();
  S.garage = { presets: [], connected: false };
  renderDisplay();
  try {
    S.ytm = await api("/api/ytm/status");
    const p = $("ytm-pill");
    p.textContent = S.ytm.signed_in ? "YT Music ✓" : "YT Music (guest)";
    p.className = "pill ghost" + (S.ytm.signed_in ? " ok" : "");
  } catch (_) { /* optional */ }
  let tab = "foryou";
  try { tab = localStorage.getItem("garage.tab") || "foryou"; } catch (_) { /* private mode */ }
  showTab(tab);
  await refresh();
  setInterval(refresh, 2500);
}
init();
