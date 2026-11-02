/* Garage controller — iPhone home-screen app for the garage Bose SoundTouch.
 * Round controller (rotary volume ring, 6 radio presets with preview, power)
 * or the FM dial skin (car-radio screen + tuning knob over the same presets)
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
  pw.addEventListener("click", () => {
    if (!S.garage || !S.garage.connected) return toast("Garage speaker is not connected", true);
    act(api("/api/garage/power", { method: "POST" }), "Music off").then(refresh);
  });
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

/* --------------------------------------------------------------- FM dial
 * Car-radio skin over the same presets: each preset sits at a "freq" on an
 * 87.5-108 band (garage_presets.json), the knob tunes, a station plays once
 * the knob rests on it. Sound turns the knob into the volume control. */
const BAND_LO = 87.5, BAND_HI = 108;
const SNAP_MHZ = 0.3;        // a station locks within this distance of the needle
const DETENT_RAD = 0.22;     // knob turn per 0.1 MHz (or per volume step)
const TUNE_MS = 700;         // rest this long on a station before it plays
const SOUND_IDLE_MS = 8000;  // Sound mode falls back to the radio when untouched
const D = { key: null, stations: [], labels: [], freq: null, angle: 0, info: false, sound: false,
  last: null, acc: 0, tuneTimer: null, soundTimer: null, target: null, targetUntil: 0, lastPreset: undefined, gliding: false };

const onAir = () => D.stations.find((s) => S.garage && s.n === S.garage.preset) || null;
function nearestStation(f) {
  let best = null;
  for (const s of D.stations) {
    const d = Math.abs(s.freq - f);
    if (d <= SNAP_MHZ && (!best || d < Math.abs(best.freq - f))) best = s;
  }
  return best;
}

function buildDial() {
  const st = (S.garage && S.garage.stations) || [];
  const key = JSON.stringify(st);
  if (key === D.key) return;
  const first = !D.stations.length;
  D.key = key;
  D.stations = [...st].sort((a, b) => a.freq - b.freq);
  D.labels.forEach((l) => l.remove());
  D.labels = D.stations.map((s, i) => {
    const d = el("div", { class: `stn r${i % 2}` });
    d.append(el("b", {}, String(s.n)), el("i", {}, "·"), document.createTextNode(s.ps));
    $("d-panel").append(d);
    return d;
  });
  $("d-mem").replaceChildren(...[...st].sort((a, b) => a.n - b.n).map((s) => {
    const b = el("button", { type: "button", "data-n": s.n, onclick: () => { $("d-mem").hidden = true; tuneStation(s); } });
    b.append(el("b", {}, String(s.n)), document.createTextNode(s.ps), el("i", {}, s.freq.toFixed(1)));
    return b;
  }));
  if (D.freq == null || first) D.freq = (onAir() || D.stations[0] || { freq: 98 }).freq;
}

function drawScale(nx, k) {
  const c = $("d-scale"), r = c.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  c.width = r.width * dpr; c.height = r.height * dpr;
  const g = c.getContext("2d"); g.scale(dpr, dpr);
  const W = r.width, H = r.height, off = $("d-panel").offsetLeft, pw = $("d-panel").clientWidth;
  g.clearRect(0, 0, W, H);
  g.strokeStyle = "#fff"; g.fillStyle = "#fff"; g.lineWidth = 1; g.textAlign = "center";
  g.font = `${Math.max(8, H * 0.42)}px "Avenir Next Condensed", "Roboto Condensed", "Arial Narrow", sans-serif`;
  const tick = (X, major, label) => {
    X = Math.round(X) + 0.5;
    if (X < -20 || X > W + 20) return;
    g.globalAlpha = major ? 1 : 0.75;
    g.beginPath(); g.moveTo(X, 0); g.lineTo(X, major ? H * 0.45 : H * 0.22); g.stroke();
    if (label != null) { g.globalAlpha = 1; g.fillText(label, X, H - 2); }
  };
  let P;
  if (D.sound) {                       // fixed 0-100 volume scale
    const vx = (v) => off + 8 + (v / 100) * (pw - 16);
    for (let v = 0; v <= 100; v += 5) tick(vx(v), v % 25 === 0, v % 25 === 0 ? v : null);
    P = vx(S.pending ?? (S.garage && S.garage.volume) ?? 0);
  } else {                             // band scale slides behind the fixed needle
    for (let i = Math.ceil(BAND_LO * 2); i <= BAND_HI * 2; i++) {   // half-MHz ticks, a number every 2 MHz
      tick(off + nx + (i / 2 - D.freq) * k, i % 2 === 0, i % 4 === 0 ? i / 2 : null);
    }
    P = off + nx;
  }
  g.globalAlpha = 1; g.fillStyle = "#e01b1b"; g.shadowColor = "#e01b1b"; g.shadowBlur = 5;
  g.beginPath(); g.moveTo(P - 4, 0); g.lineTo(P + 4, 0); g.lineTo(P, H * 0.5); g.closePath(); g.fill();
}

function drawKnob() {
  const c = $("d-knobc"), r = c.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  if (!r.width) return;
  c.width = r.width * dpr; c.height = r.height * dpr;
  const g = c.getContext("2d"); g.scale(dpr, dpr);
  const S2 = r.width, C2 = S2 / 2, R = S2 / 2 - 2;
  let gr = g.createLinearGradient(0, 0, S2, S2);            // chrome rim
  gr.addColorStop(0, "#e4e8ec"); gr.addColorStop(0.45, "#5a6068"); gr.addColorStop(0.55, "#2a2e33"); gr.addColorStop(1, "#c9ced4");
  g.fillStyle = gr; g.beginPath(); g.arc(C2, C2, R, 0, 7); g.fill();
  g.fillStyle = "#16191d"; g.beginPath(); g.arc(C2, C2, R - 6, 0, 7); g.fill();
  g.save(); g.translate(C2, C2); g.rotate(D.angle);           // knurled grip
  for (let i = 0; i < 72; i++) {
    const a = (i / 72) * Math.PI * 2, lit = 0.5 + 0.5 * Math.cos(a + D.angle - 2.3);
    g.strokeStyle = `rgba(${(150 + 80 * lit) | 0},${(155 + 80 * lit) | 0},${(165 + 75 * lit) | 0},${0.25 + 0.5 * lit})`;
    g.lineWidth = 1.4;
    g.beginPath(); g.moveTo(Math.cos(a) * (R - 8), Math.sin(a) * (R - 8)); g.lineTo(Math.cos(a) * (R - 22), Math.sin(a) * (R - 22)); g.stroke();
  }
  g.restore();
  gr = g.createRadialGradient(C2 - R * 0.25, C2 - R * 0.3, 4, C2, C2, R - 22);  // satin cap
  gr.addColorStop(0, "#3b4047"); gr.addColorStop(1, "#0f1114");
  g.fillStyle = gr; g.beginPath(); g.arc(C2, C2, R - 24, 0, 7); g.fill();
  g.save(); g.translate(C2, C2); g.rotate(D.angle - Math.PI / 2);
  g.fillStyle = "#e01b1b"; g.shadowColor = "#e01b1b"; g.shadowBlur = 6;
  g.beginPath(); g.arc(R - 36, 0, 3.2, 0, 7); g.fill(); g.restore();
  g.fillStyle = "#8a9096"; g.font = "600 11px -apple-system, Roboto, sans-serif"; g.textAlign = "center";
  g.fillText(D.sound ? "VOLUME" : "TUNE", C2, C2 + 4);
}

function statusText(g) {
  if (!g.connected) return "Not connected";
  if (g.standby) return "Standby";
  if (g.track) return [g.track.title, g.track.artist].filter(Boolean).join(" · ");
  return g.source === "AIRPLAY" ? "AirPlay" : g.source || "Ready";
}

function drawDial() {
  const g = S.garage || {}, panel = $("d-panel"), lcd = $("lcd");
  const s = nearestStation(D.freq), air = onAir();
  const W = panel.clientWidth, nx = W * 0.4, k = W * 0.11;  // needle at 40%; px per MHz keeps ~3 MHz neighbours in view
  D.stations.forEach((st, i) => {
    D.labels[i].style.left = `${nx + (st.freq - D.freq) * k}px`;
    D.labels[i].classList.toggle("cur", st === s);
    D.labels[i].classList.toggle("air", !!air && st.n === air.n);
  });
  lcd.classList.toggle("seek", !s && !D.sound);
  lcd.classList.toggle("snd", D.sound);
  lcd.classList.toggle("off", isOff());
  const vol = S.pending ?? g.volume;
  let strip;
  if (D.sound) strip = `Sound · Volume ${vol ?? "–"}`;
  else if (D.info) strip = air && !g.standby ? `${air.name} · Preset ${air.n}` : statusText(g);
  else strip = `FM ${(s ? s.freq : D.freq).toFixed(1)} MHz`;
  $("d-strip").replaceChildren(el("span", {}, strip));
  $("d-dev").textContent = !g.connected ? "Not connected" : g.standby ? "Standby" : g.device || "";
  $("d-voldev").textContent = g.device || "";
  $("d-vol").hidden = !D.sound;
  $("d-vn").textContent = vol ?? "–";
  $("d-vfill").style.width = `${Math.max(0, Math.min(100, vol || 0))}%`;
  $("d-mem").querySelectorAll("button").forEach((b) => b.classList.toggle("on", +b.dataset.n === g.preset));
  $("k-mem").classList.toggle("sel", !$("d-mem").hidden);
  $("k-info").classList.toggle("sel", D.info && !D.sound);
  $("k-sound").classList.toggle("sel", D.sound);
  const kn = $("d-knob");
  kn.setAttribute("aria-label", D.sound ? "Volume wheel" : "Tuning wheel");
  kn.setAttribute("aria-valuemin", D.sound ? 0 : BAND_LO);
  kn.setAttribute("aria-valuemax", D.sound ? 100 : BAND_HI);
  kn.setAttribute("aria-valuenow", D.sound ? vol ?? 0 : (s ? s.freq : D.freq).toFixed(1));
  kn.setAttribute("aria-valuetext", D.sound ? `Volume ${vol ?? 0}` : s ? `${s.name}, ${s.freq.toFixed(1)}` : `${D.freq.toFixed(1)} MHz`);
  drawScale(nx, k);
  drawKnob();
}

function renderDial() {
  if (S.skin !== "dial") return;
  const g = S.garage || {};
  buildDial();
  if (D.target && (g.preset === D.target.n || Date.now() > D.targetUntil)) D.target = null;
  // follow a station change made elsewhere (HA, the ring, another phone) unless the user is tuning
  const changed = g.preset !== D.lastPreset;
  D.lastPreset = g.preset;
  const a = onAir();
  if (changed && a && D.last == null && !D.tuneTimer && !D.target && !D.gliding && a.freq !== D.freq) glide(a.freq);
  else drawDial();
}

function glide(f, done) {
  const f0 = D.freq, a0 = D.angle, t0 = performance.now();
  const dur = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 450;
  D.gliding = true;
  const frame = (now) => {
    if (D.last != null) { D.gliding = false; return; }   // the user grabbed the knob
    const t = dur ? Math.min(1, (now - t0) / dur) : 1, e = 1 - Math.pow(1 - t, 3);
    D.freq = f0 + (f - f0) * e; D.angle = a0 + (f - f0) * 2.2 * e;
    if (t < 1) { drawDial(); requestAnimationFrame(frame); return; }
    D.freq = f; D.gliding = false; drawDial();
    if (done) done();
  };
  requestAnimationFrame(frame);
}

function tuneStation(s) {
  clearTimeout(D.tuneTimer); D.tuneTimer = null;
  if (D.sound) setSound(false);
  glide(s.freq, () => {
    if (S.garage && S.garage.preset === s.n && !S.garage.standby) return;
    D.target = s; D.targetUntil = Date.now() + 10000;
    playPreset(s.n);
  });
}

function settleDial() {
  clearTimeout(D.tuneTimer); D.tuneTimer = null;
  if (D.sound) return;
  const s = nearestStation(D.freq);
  if (!s) return;
  D.tuneTimer = setTimeout(() => { D.tuneTimer = null; tuneStation(s); }, TUNE_MS);
}

function dialVolume(dir) {
  armSound();
  if (isOff()) return;
  const v = Math.max(0, Math.min(100, (S.pending ?? S.garage.volume ?? 25) + dir));
  if (v === S.pending) return;
  S.pending = v;
  if (Date.now() - (D.volSent || 0) > SEND_MS) { D.volSent = Date.now(); sendVolume(v).catch(() => {}); }
  clearTimeout(S.stepTimer);
  S.stepTimer = setTimeout(async () => { await act(sendVolume(v)); S.garage.volume = v; S.pending = null; renderDial(); }, 300);
}

function detent(dir) {
  if (D.sound) dialVolume(dir);
  else D.freq = Math.min(BAND_HI, Math.max(BAND_LO, Math.round((D.freq + dir * 0.1) * 10) / 10));
  if (navigator.vibrate) navigator.vibrate(3);
}

function armSound() { clearTimeout(D.soundTimer); D.soundTimer = setTimeout(() => setSound(false), SOUND_IDLE_MS); }
function setSound(on) {
  D.sound = on;
  clearTimeout(D.soundTimer);
  if (on) { $("d-mem").hidden = true; clearTimeout(D.tuneTimer); D.tuneTimer = null; armSound(); }
  drawDial();
}

function knobAngle(e) {
  const r = $("d-knob").getBoundingClientRect();
  return Math.atan2(e.clientY - r.top - r.height / 2, e.clientX - r.left - r.width / 2);
}
function bindDial() {
  const kn = $("d-knob");
  kn.addEventListener("pointerdown", (e) => {
    try { kn.setPointerCapture(e.pointerId); } catch (_) { /* optional */ }
    D.last = knobAngle(e); D.acc = 0;
    clearTimeout(D.tuneTimer); D.tuneTimer = null;
  });
  kn.addEventListener("pointermove", (e) => {
    if (D.last == null) return;
    const a = knobAngle(e);
    let d = a - D.last;
    if (d > Math.PI) d -= 2 * Math.PI; if (d < -Math.PI) d += 2 * Math.PI;
    D.last = a; D.acc += d; D.angle += d;
    while (Math.abs(D.acc) >= DETENT_RAD) { const s = Math.sign(D.acc); D.acc -= s * DETENT_RAD; detent(s); }
    drawDial();
  });
  const end = () => {
    if (D.last == null) return;
    D.last = null; D.acc = 0;
    if (!D.sound) { const s = nearestStation(D.freq); if (s) D.freq = s.freq; }
    drawDial(); settleDial();
  };
  kn.addEventListener("pointerup", end);
  kn.addEventListener("pointercancel", end);
  const nudge = (dir) => { detent(dir); D.angle += dir * DETENT_RAD; drawDial(); settleDial(); };
  kn.addEventListener("wheel", (e) => { e.preventDefault(); nudge(e.deltaY < 0 ? 1 : -1); }, { passive: false });
  kn.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight" || e.key === "ArrowUp") { e.preventDefault(); nudge(1); }
    if (e.key === "ArrowLeft" || e.key === "ArrowDown") { e.preventDefault(); nudge(-1); }
  });
  const seek = (dir) => {
    if (!D.stations.length) return;
    const list = dir > 0 ? D.stations.filter((s) => s.freq > D.freq + 0.05) : D.stations.filter((s) => s.freq < D.freq - 0.05).reverse();
    tuneStation(list[0] || (dir > 0 ? D.stations[0] : D.stations[D.stations.length - 1]));
  };
  $("d-seekdn").addEventListener("click", () => seek(-1));
  $("d-seekup").addEventListener("click", () => seek(1));
  const radio = () => { if (D.sound) setSound(false); };
  $("k-radio").addEventListener("click", () => { $("d-mem").hidden = true; D.info = false; radio(); drawDial(); });
  $("k-mem").addEventListener("click", () => { radio(); $("d-mem").hidden = !$("d-mem").hidden; drawDial(); });
  $("k-info").addEventListener("click", () => { radio(); D.info = !D.info; drawDial(); });
  $("k-fm").addEventListener("click", () => { radio(); toast("FM only: every station is an internet stream"); });
  $("k-sound").addEventListener("click", () => setSound(!D.sound));
  const clock = () => { const d = new Date(); $("d-clock").textContent = `${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`; };
  clock(); setInterval(clock, 15000);
  window.addEventListener("resize", () => { if (S.skin === "dial") drawDial(); });
}

function setSkin(name) {
  S.skin = name === "ring" ? "ring" : "dial";
  $("ring-wrap").classList.toggle("hidden", S.skin !== "ring");
  $("dial-wrap").classList.toggle("hidden", S.skin !== "dial");
  document.querySelectorAll(".skin button").forEach((b) => b.classList.toggle("on", b.dataset.skin === S.skin));
  try { localStorage.setItem("garage.skin", S.skin); } catch (_) { /* private mode */ }
  if (S.skin === "dial") renderDial();
  else { clearTimeout(D.tuneTimer); D.tuneTimer = null; if (D.sound) setSound(false); renderDisplay(); }
}
document.querySelectorAll(".skin button").forEach((b) => b.addEventListener("click", () => setSkin(b.dataset.skin)));

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
    renderDial();
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
  bindDial();
  S.garage = { presets: [], connected: false };
  renderDisplay();
  let skin = "dial";
  try { skin = localStorage.getItem("garage.skin") || "dial"; } catch (_) { /* private mode */ }
  setSkin(skin);
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
