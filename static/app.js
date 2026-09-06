const $ = (sel) => document.querySelector(sel);

const els = {
  status: $("#device-status"),
  deviceName: $("#device-name"),
  btnDevices: $("#btn-devices"),
  devicePicker: $("#device-picker"),
  deviceList: $("#device-list"),
  btnRefreshDevices: $("#btn-refresh-devices"),
  title: $("#track-title"),
  artist: $("#track-artist"),
  posCur: $("#pos-current"),
  posDur: $("#pos-duration"),
  progress: $("#progress-fill"),
  btnPlay: $("#btn-play"),
  iconPlay: $("#icon-play"),
  iconPause: $("#icon-pause"),
  btnPrev: $("#btn-prev"),
  btnNext: $("#btn-next"),
  btnStop: $("#btn-stop"),
  volume: $("#volume-slider"),
  volumeVal: $("#volume-val"),
  form: $("#add-form"),
  urlInput: $("#url-input"),
  btnAdd: $("#btn-add"),
  searchResults: $("#search-results"),
  searchTitle: $("#search-title"),
  resultList: $("#result-list"),
  btnCloseSearch: $("#btn-close-search"),
  queueList: $("#queue-list"),
  queueCount: $("#queue-count"),
  queueStage: $("#queue-stage"),
  queueSpacer: $("#queue-spacer"),
  queuePos: $("#queue-pos"),
  queueScroll: $("#queue-scroll"),
  queueScrollThumb: $("#queue-scroll-thumb"),
};

let isPlaying = false;
let volumeTimeout = null;
let knownPosition = 0;
let knownDuration = 0;
let lastPollTime = 0;

// --- API helpers ---
async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(`/api${path}`, opts);
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch {}
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return res.json();
}

// Fire-and-forget API call: log failures instead of raising unhandled rejections
function send(method, path, body) {
  return api(method, path, body).catch((e) => console.warn(`${method} ${path}:`, e.message));
}

function fmt(sec) {
  if (!sec || sec < 0) return "0:00";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function esc(s) {
  if (!s) return "";
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

// --- Local ticker ---
function tickDisplay() {
  let pos = knownPosition;
  if (isPlaying && lastPollTime) {
    pos += (Date.now() - lastPollTime) / 1000;
  }
  if (knownDuration > 0) pos = Math.min(pos, knownDuration);
  els.posCur.textContent = fmt(pos);
  els.posDur.textContent = fmt(knownDuration);
  const pct = knownDuration > 0 ? (pos / knownDuration) * 100 : 0;
  els.progress.style.width = `${pct}%`;
}

// --- Poll status ---
async function poll() {
  try {
    const s = await api("GET", "/status");

    if (s.device_connected) {
      els.status.textContent = "connected";
      els.status.className = "status online";
    } else if (s.device_name) {
      els.status.textContent = "connecting";
      els.status.className = "status offline";
    } else {
      els.status.textContent = "no speaker";
      els.status.className = "status offline";
    }

    els.deviceName.textContent = s.device_name || "Select speaker";

    if (s.current_track) {
      els.title.textContent = s.current_track.title;
      els.artist.textContent = s.current_track.artist || "";
    } else {
      els.title.textContent = "No track";
      els.artist.textContent = "";
    }

    knownPosition = s.position;
    knownDuration = s.duration;
    lastPollTime = Date.now();

    isPlaying = s.transport_state === "PLAYING";
    els.iconPlay.style.display = isPlaying ? "none" : "block";
    els.iconPause.style.display = isPlaying ? "block" : "none";

    if (!volumeTimeout) {
      els.volume.value = s.volume;
      els.volumeVal.textContent = s.volume;
    }

    renderQueue(s.queue, s.current_index);
    renderDownload(s.download);
    updateModeIcon(s.play_mode || "NORMAL");
  } catch {
    els.status.textContent = "offline";
    els.status.className = "status offline";
  }
}

// --- Queue (Rolodex) ---
// #queue-list scrolls vertically over an invisible spacer; the fractional
// scroll position picks the focused card.  Only the cards near the focus
// are in the DOM (the queue can hold hundreds).  Each card is hinged on
// the rod: later cards hang beneath and bend away, earlier cards sit above
// the rod and bend back.
const CARD_H = 104;      // px, must match .queue-item height
const GAP = 6;           // px between the front card and its neighbours
const ROD_Y = 128;       // px from stage top where the rod sits
const STEP = 56;         // px of scroll per card
const NEAR = 4;          // cards rendered on each side of the focus
const TAB_H = 58;        // px of a card's top strip (index + title) left showing
const DEG = Math.PI / 180;

// Hinge angle of the k-th neighbour (k >= 1): 40deg, then 12deg steeper each
function hingeDeg(k) { return Math.min(72, 40 + 12 * (k - 1)); }
// Projected height of the title strip of neighbour k once it is tilted
function tabPx(k) { return TAB_H * Math.cos(hingeDeg(k) * DEG); }
// Stacked tab height for x neighbours (fractional x interpolates)
function stackPx(x) {
  let sum = 0, k = 1;
  for (; k <= Math.floor(x); k++) sum += tabPx(k);
  return sum + (x - Math.floor(x)) * tabPx(k);
}

let queueItems = [];
let queueSig = "";
let lastCurrentIdx = -1;
let activeIdx = -1;
const cardEls = new Map();   // queue index -> element

function queuePos() {
  const n = queueItems.length;
  if (!n) return 0;
  return Math.min(n - 1, Math.max(0, els.queueList.scrollTop / STEP));
}

function renderQueue(queue, currentIdx) {
  els.queueCount.textContent = queue.length ? `(${queue.length})` : "";

  const sig = currentIdx + "|" + queue.map((t) => t.id + (t.ready ? "+" : "-")).join(",");
  if (sig === queueSig) return;
  queueSig = sig;

  queueItems = queue;
  activeIdx = currentIdx;
  els.queueSpacer.style.height = `${Math.max(0, queue.length - 1) * STEP}px`;
  els.queueList.classList.toggle("is-empty", queue.length === 0);
  cardEls.forEach((el) => el.remove());
  cardEls.clear();
  layoutCards();

  // Bring the playing card to the front when the track changes (not on every
  // poll, so it never fights the user's own scrolling)
  if (currentIdx !== lastCurrentIdx) {
    lastCurrentIdx = currentIdx;
    scrollQueueToActive();
  }
}

function makeCard(i) {
  const track = queueItems[i];
  const el = document.createElement("div");
  el.className = "queue-item" + (i === activeIdx ? " active" : "");
  const statusBadge = track.ready ? "" : '<span class="q-dl">downloading</span>';
  el.innerHTML = `
    <div class="q-idx">${i + 1}</div>
    <div class="q-title">${esc(track.title)}</div>
    <div class="q-artist">${esc(track.artist)}</div>
    <div class="q-bottom">
      <span class="q-dur">${fmt(track.duration)}</span>
      ${statusBadge}
    </div>
    <button class="q-remove" data-id="${track.id}" title="Remove">&times;</button>
  `;
  el.addEventListener("click", () => {
    // Tap the front card to play it; tap a bent card to flip to it
    if (Math.round(queuePos()) === i) send("POST", "/play", { track_id: track.id });
    else scrollQueueTo(i);
  });
  el.querySelector(".q-remove").addEventListener("click", (e) => {
    e.stopPropagation();
    send("DELETE", `/queue/${track.id}`);
  });
  return el;
}

function layoutCards() {
  const n = queueItems.length;
  if (!n) { updateQueuePos(); return; }
  const pos = queuePos();
  const focus = Math.round(pos);
  const lo = Math.max(0, Math.floor(pos) - NEAR);
  const hi = Math.min(n - 1, Math.ceil(pos) + NEAR);

  for (const [i, el] of cardEls) {
    if (i < lo || i > hi) { el.remove(); cardEls.delete(i); }
  }

  for (let i = lo; i <= hi; i++) {
    let el = cardEls.get(i);
    if (!el) { el = makeCard(i); cardEls.set(i, el); els.queueStage.appendChild(el); }

    const d = i - pos;
    const a = Math.abs(d);
    // Angle: flat at the front, 40deg for the first neighbour, steeper after
    const theta = a <= 1 ? 40 * a : hingeDeg(a);
    let top, origin, rot, z;
    if (d >= 0) {
      // Later cards: staircase down from the front card's bottom edge, each
      // showing its index + title strip; nearer cards sit on top.
      top = a <= 1 ? ROD_Y + (CARD_H + GAP) * a : ROD_Y + CARD_H + GAP + stackPx(a - 1);
      origin = "top center"; rot = -theta;
      z = a < 0.5 ? 100 : 90 + Math.round(a);
    } else {
      // Earlier cards: staircase up from the rod, tops bending back
      const bottom = a <= 1 ? ROD_Y + CARD_H - (CARD_H + GAP) * a : ROD_Y - GAP - stackPx(a - 1);
      top = bottom - CARD_H;
      origin = "bottom center"; rot = theta;
      z = a < 0.5 ? 100 : 99 - Math.round(a);
    }
    const depth = -Math.min(80, 20 * a);
    el.style.top = `${top.toFixed(1)}px`;
    el.style.transformOrigin = origin;
    el.style.transform = `translateZ(${depth.toFixed(1)}px) rotateX(${rot.toFixed(2)}deg)`;
    el.style.zIndex = String(z);
    el.style.opacity = String(Math.max(0.7, 1 - 0.08 * a));
    el.classList.toggle("focus", i === focus);
  }
  updateQueuePos();
}

function scrollQueueTo(i, smooth = true) {
  els.queueList.scrollTo({ top: i * STEP, behavior: smooth ? "smooth" : "auto" });
}

function scrollQueueToActive() {
  if (activeIdx >= 0) scrollQueueTo(activeIdx);
}

// Position indicator: "42 / 123" for the front card, plus a thin track whose
// thumb shows how far through the deck you are.
function updateQueuePos() {
  const n = queueItems.length;
  if (n < 2) {
    els.queuePos.textContent = "";
    els.queueScroll.classList.add("hidden");
    return;
  }
  const pos = queuePos();
  els.queuePos.textContent = `${Math.round(pos) + 1} / ${n}`;
  const trackW = els.queueScroll.clientWidth;
  const thumbW = Math.max(12, trackW / n);
  const x = (pos / (n - 1)) * (trackW - thumbW);
  els.queueScrollThumb.style.width = `${thumbW}px`;
  els.queueScrollThumb.style.transform = `translateX(${x}px)`;
  els.queueScroll.classList.remove("hidden");
}

// Scroll drives the deck; when scrolling stops, settle on the nearest card
let layoutRaf = 0;
let snapTimer = null;
els.queueList.addEventListener("scroll", () => {
  if (!layoutRaf) layoutRaf = requestAnimationFrame(() => { layoutRaf = 0; layoutCards(); });
  clearTimeout(snapTimer);
  snapTimer = setTimeout(() => {
    const pos = queuePos();
    const i = Math.round(pos);
    if (Math.abs(pos - i) > 0.02) scrollQueueTo(i);
  }, 120);
}, { passive: true });
window.addEventListener("resize", () => layoutCards());

// --- Download progress ---
function renderDownload(dl) {
  const wrap = $("#download-progress");
  const text = $("#dl-text");
  const count = $("#dl-count");
  const fill = $("#dl-fill");
  const trackList = $("#dl-tracks");

  if (!dl) {
    wrap.classList.add("hidden");
    return;
  }

  wrap.classList.remove("hidden");
  const current = dl.current || "";
  if (dl.total === 1) {
    text.textContent = current || "Downloading...";
  } else {
    text.textContent = current || "Playlist downloading";
  }

  // Time remaining estimate
  let eta = "";
  if (dl.elapsed > 0 && dl.done > 0 && dl.done < dl.total) {
    const perTrack = dl.elapsed / dl.done;
    const remaining = Math.round(perTrack * (dl.total - dl.done));
    eta = remaining >= 60
      ? ` \u2022 ~${Math.ceil(remaining / 60)}m left`
      : ` \u2022 ~${remaining}s left`;
  }
  const failed = dl.failed ? ` • ${dl.failed} failed` : "";
  count.textContent = (dl.total === 1 ? (eta || "") : `${dl.done}/${dl.total}${eta}`) + failed;
  const pct = dl.total > 0 ? (dl.done / dl.total) * 100 : 0;
  fill.style.width = `${pct}%`;

  // Render track list for playlists
  if (dl.tracks && dl.tracks.length > 1) {
    trackList.innerHTML = "";
    dl.tracks.forEach((t) => {
      const li = document.createElement("li");
      let iconClass, iconContent;
      if (t.ready) {
        iconClass = "done";
        iconContent = "\u2713";
        li.className = "dl-track dl-done";
      } else if (t.downloading) {
        iconClass = "active";
        iconContent = '<span class="dl-spinner"></span>';
        li.className = "dl-track dl-active";
      } else {
        iconClass = "pending";
        iconContent = "\u2022";
        li.className = "dl-track dl-pending";
      }
      li.innerHTML = `<span class="dl-icon ${iconClass}">${iconContent}</span><span class="dl-title">${esc(t.title)}</span>`;
      trackList.appendChild(li);
    });
  } else {
    trackList.innerHTML = "";
  }
}

// --- Search ---
function isUrl(s) {
  return /^https?:\/\//.test(s) || /^www\./.test(s);
}

async function doSearch(query) {
  els.searchResults.classList.remove("hidden");
  els.searchTitle.textContent = "Searching...";
  els.resultList.innerHTML = "";

  try {
    const data = await api("POST", "/search", { query, max_results: 8 });
    els.searchTitle.textContent = `Results for "${query}"`;

    if (!data.results || data.results.length === 0) {
      els.resultList.innerHTML = '<li class="search-empty">No results found</li>';
      return;
    }

    data.results.forEach((r) => {
      const li = document.createElement("li");
      li.className = "result-item";
      li.innerHTML = `
        <div class="r-info">
          <div class="r-title">${esc(r.title)}</div>
          <div class="r-artist">${esc(r.artist)} &middot; ${fmt(r.duration)}</div>
        </div>
        <button class="r-add">+</button>
      `;

      li.querySelector(".r-add").addEventListener("click", async (e) => {
        e.stopPropagation();
        const btn = e.currentTarget;
        btn.disabled = true;
        btn.innerHTML = '<span class="r-spinner"></span>';
        try {
          const res = await api("POST", "/queue/add", { url: r.url });
          btn.innerHTML = res.duplicate ? "\u2022" : "\u2713";
          if (res.duplicate) btn.title = "Already in queue";
        } catch {
          btn.innerHTML = "!";
        }
      });

      els.resultList.appendChild(li);
    });
  } catch {
    els.searchTitle.textContent = "Search failed";
  }
}

els.btnCloseSearch.addEventListener("click", () => {
  els.searchResults.classList.add("hidden");
});

// --- Device picker ---
els.btnDevices.addEventListener("click", async () => {
  const picker = els.devicePicker;
  if (!picker.classList.contains("hidden")) {
    picker.classList.add("hidden");
    return;
  }
  picker.classList.remove("hidden");
  await refreshDeviceList();
});

els.btnRefreshDevices.addEventListener("click", async () => {
  els.btnRefreshDevices.textContent = "Scanning...";
  els.btnRefreshDevices.disabled = true;
  try {
    const data = await api("POST", "/devices/refresh");
    renderDeviceList(data.devices, data.current);
  } catch {}
  els.btnRefreshDevices.textContent = "Scan";
  els.btnRefreshDevices.disabled = false;
});

async function refreshDeviceList() {
  try {
    const data = await api("GET", "/devices");
    renderDeviceList(data.devices, data.current);
  } catch {}
}

function renderDeviceList(devices, current) {
  els.deviceList.innerHTML = "";
  if (!devices.length) {
    els.deviceList.innerHTML = '<li class="device-empty">No speakers found</li>';
    return;
  }
  devices.forEach((d) => {
    const li = document.createElement("li");
    li.className = "device-item" + (d.name === current ? " active" : "");
    li.innerHTML = `
      <span class="d-name">${esc(d.name)}</span>
      <span class="d-model">${esc(d.model)}</span>
    `;
    li.addEventListener("click", async () => {
      await send("POST", "/devices/select", { name: d.name });
      els.devicePicker.classList.add("hidden");
      poll();
    });
    els.deviceList.appendChild(li);
  });
}

// --- Controls ---
els.btnPlay.addEventListener("click", () => {
  send("POST", isPlaying ? "/pause" : "/play");
});

els.btnStop.addEventListener("click", () => send("POST", "/stop"));
els.btnNext.addEventListener("click", () => send("POST", "/next"));
els.btnPrev.addEventListener("click", () => send("POST", "/prev"));

// Seek — tap on progress bar
$(".progress-bar").addEventListener("click", (e) => {
  if (knownDuration <= 0) return;
  const bar = e.currentTarget;
  const rect = bar.getBoundingClientRect();
  const pct = (e.clientX - rect.left) / rect.width;
  const pos = Math.floor(pct * knownDuration);
  knownPosition = pos;
  lastPollTime = Date.now();
  send("POST", "/seek", { position: pos });
});

// Play mode toggle
const modes = ["NORMAL", "REPEAT_ALL", "REPEAT_ONE", "SHUFFLE"];
$("#btn-mode").addEventListener("click", () => {
  const cur = modes.indexOf(currentMode);
  const next = modes[(cur + 1) % modes.length];
  currentMode = next;
  updateModeIcon(next);
  send("POST", "/play-mode", { mode: next });
});

let currentMode = "NORMAL";

function updateModeIcon(mode) {
  currentMode = mode;
  $("#icon-mode-normal").style.display = mode === "NORMAL" ? "block" : "none";
  $("#icon-mode-repeat").style.display = mode === "REPEAT_ALL" ? "block" : "none";
  $("#icon-mode-repeat1").style.display = mode === "REPEAT_ONE" ? "block" : "none";
  $("#icon-mode-shuffle").style.display = mode === "SHUFFLE" ? "block" : "none";
  const btn = $("#btn-mode");
  btn.classList.toggle("mode-active", mode !== "NORMAL");
  btn.title = {NORMAL:"Sequential", REPEAT_ALL:"Repeat all", REPEAT_ONE:"Repeat one", SHUFFLE:"Shuffle"}[mode];
}

els.volume.addEventListener("input", () => {
  els.volumeVal.textContent = els.volume.value;
  clearTimeout(volumeTimeout);
  volumeTimeout = setTimeout(() => {
    send("POST", "/volume", { level: parseInt(els.volume.value) });
    volumeTimeout = null;
  }, 200);
});

// --- Add / Search form ---
els.form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const val = els.urlInput.value.trim();
  if (!val) return;

  if (isUrl(val)) {
    // It's a URL — add directly
    els.btnAdd.disabled = true;
    try {
      const res = await api("POST", "/queue/add", { url: val });
      els.urlInput.value = "";
      if (res.duplicate) {
        els.urlInput.placeholder = "Already in queue";
        setTimeout(() => { els.urlInput.placeholder = "Search YouTube or paste URL"; }, 2000);
      } else if (res.count !== undefined) {
        const msg = res.skipped
          ? `${res.count} added, ${res.skipped} already in queue`
          : `${res.count} tracks added`;
        els.urlInput.placeholder = msg;
        setTimeout(() => { els.urlInput.placeholder = "Search YouTube or paste URL"; }, 3000);
      }
    } catch {
      alert("Failed to add");
    }
    els.btnAdd.disabled = false;
  } else {
    // It's a search query
    await doSearch(val);
  }
});

// --- Logs ---
const logToggle = $("#btn-toggle-logs");
const logPanel = $("#log-panel");
const logList = $("#log-list");
let logOpen = false;
let logInterval = null;

logToggle.addEventListener("click", () => {
  logOpen = !logOpen;
  logPanel.classList.toggle("hidden", !logOpen);
  logToggle.textContent = logOpen ? "Logs (hide)" : "Logs";
  if (logOpen) {
    fetchLogs();
    logInterval = setInterval(fetchLogs, 3000);
  } else {
    clearInterval(logInterval);
    logInterval = null;
  }
});

async function fetchLogs() {
  try {
    const data = await api("GET", "/logs");
    logList.innerHTML = "";
    // Show newest first
    const logs = (data.logs || []).slice().reverse();
    logs.forEach((entry) => {
      const li = document.createElement("li");
      li.className = "log-entry log-" + entry.level.toLowerCase();
      li.innerHTML = `<span class="log-ts">${esc(entry.ts)}</span> <span class="log-lvl">${entry.level}</span> ${esc(entry.msg)}`;
      logList.appendChild(li);
    });
    if (logPanel.scrollTop < 50) {
      logPanel.scrollTop = 0;
    }
  } catch {}
}

// --- Start ---
poll();
setInterval(poll, 2000);
setInterval(tickDisplay, 500);
