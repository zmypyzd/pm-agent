const { invoke } = window.__TAURI__.core;
const { getCurrentWindow } = window.__TAURI__.window;
const { listen } = window.__TAURI__.event;

const appWindow = getCurrentWindow();

const $ = (id) => document.getElementById(id);

const COLLAPSED = { w: 140, h: 160 };
const EXPANDED = { w: 220, h: 320 };

// ---------- Event history (drives the mini panel) ----------
const history = [];
function pushHistory(text, kind, url) {
  history.push({ at: new Date(), text, kind: kind || "info", url: url || null });
  if (history.length > 30) history.shift();
}

async function openUrl(url) {
  try { await invoke("open_url", { url }); } catch (err) { console.error(err); }
}

// ---------- Notification queue ----------
const queue = [];
let queueRunning = false;
let currentTimer = null;

function notify(text, opts = {}) {
  const item = {
    text,
    duration: opts.duration || 2400,
    kind: opts.kind || "info",
    pose: opts.pose || null,
    silent: !!opts.silent,
    url: opts.url || null,
  };
  if (!item.silent) pushHistory(text, item.kind, item.url);
  if (opts.priority === "high") {
    queue.unshift(item);
    if (queueRunning && currentTimer) {
      clearTimeout(currentTimer);
      currentTimer = null;
      queueRunning = false;
    }
  } else {
    queue.push(item);
  }
  if (!queueRunning) runQueue();
}

function runQueue() {
  if (queue.length === 0) {
    queueRunning = false;
    return;
  }
  queueRunning = true;
  const n = queue.shift();
  renderBubble(n.text, n.kind, n.url);
  if (n.pose) triggerPose(n.pose);
  currentTimer = setTimeout(() => {
    hideBubble();
    setTimeout(runQueue, 220);
  }, n.duration);
}

function renderBubble(text, kind, url) {
  const b = $("bubble");
  b.textContent = text;
  b.className = "bubble " + (kind || "");
  b.onclick = null;
  if (url) {
    b.classList.add("clickable");
    b.onclick = (e) => { e.stopPropagation(); openUrl(url); };
  }
  const pet = $("pet");
  pet.classList.remove("quacking");
  void pet.offsetWidth;
  pet.classList.add("quacking");
  setTimeout(() => pet.classList.remove("quacking"), 280);
}

function hideBubble() {
  $("bubble").classList.add("hidden");
}

function triggerPose(name) {
  const pet = $("pet");
  ["happy", "sad", "squish"].forEach((c) => pet.classList.remove(c));
  void pet.offsetWidth;
  pet.classList.add(name);
  const dur = name === "happy" ? 900 : name === "sad" ? 1200 : 320;
  setTimeout(() => pet.classList.remove(name), dur);
}

function setBadge(text, state) {
  const badge = $("badge");
  badge.textContent = text;
  badge.className = "badge" + (state ? " " + state : "");
}

function squish() { triggerPose("squish"); }

// ---------- Mini panel ----------
let panelOpen = false;
let panelBusy = false;

async function togglePanel() {
  if (panelBusy) return;
  panelBusy = true;
  const panel = $("panel");
  if (!panelOpen) {
    try { await invoke("set_window_size", { width: EXPANDED.w, height: EXPANDED.h }); } catch (e) { console.error(e); }
    renderPanel();
    panel.classList.remove("hidden");
    panelOpen = true;
  } else {
    panel.classList.add("hidden");
    // Wait for fade-out before shrinking the window.
    setTimeout(async () => {
      try { await invoke("set_window_size", { width: COLLAPSED.w, height: COLLAPSED.h }); } catch (e) { console.error(e); }
    }, 240);
    panelOpen = false;
  }
  panelBusy = false;
}

function renderPanel() {
  const list = $("panel-history");
  list.innerHTML = "";
  if (history.length === 0) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "no events yet";
    list.appendChild(li);
  } else {
    history.slice().reverse().forEach((h) => {
      const li = document.createElement("li");
      li.className = (h.kind || "") + (h.url ? " clickable" : "");
      const t = h.at.toTimeString().slice(0, 5);
      li.innerHTML = `<span class="t">${t}</span><span>${escapeHtml(h.text)}</span>`;
      if (h.url) li.dataset.url = h.url;
      list.appendChild(li);
    });
  }
  $("panel-cost").textContent = (lastState && lastState.db_exists)
    ? `$${lastState.cumulative_cost_usd.toFixed(2)}`
    : "$0.00";
  $("panel-cycle").textContent = (lastState && lastState.cycle_id)
    ? `cycle ${lastState.cycle_id} · ${lastState.cycle_status}${lastState.findings_in_cycle ? " · " + lastState.findings_in_cycle + "f" : ""}`
    : "no cycle yet";
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

// ---------- Idle behaviours ----------
function scheduleBlink() {
  const wait = 2200 + Math.random() * 3200;
  setTimeout(() => {
    const pet = $("pet");
    if (!pet.classList.contains("sleeping")) {
      pet.classList.add("blinking");
      setTimeout(() => pet.classList.remove("blinking"), 140);
    }
    scheduleBlink();
  }, wait);
}

function welcomeHint() {
  setTimeout(() => notify("点鸭子看记录 · 右键看菜单", { duration: 3200, silent: true }), 800);
}

// ---------- State → presentation ----------
let lastState = null;
let sleepTimer = null;

function applyState(s) {
  const pet = $("pet");

  if (!s || !s.db_exists) {
    setBadge("idle", "");
    if (!sleepTimer) {
      sleepTimer = setTimeout(() => pet.classList.add("sleeping"), 30000);
    }
    lastState = s;
    if (panelOpen) renderPanel();
    return;
  }

  pet.classList.remove("sleeping");
  if (sleepTimer) { clearTimeout(sleepTimer); sleepTimer = null; }

  const cost = `$${s.cumulative_cost_usd.toFixed(2)}`;
  const prev = lastState;

  if (prev && prev.db_exists) {
    if (s.cycle_id !== prev.cycle_id && s.cycle_status === "running") {
      notify(`Cycle ${s.cycle_id} started ▶`, { kind: "info" });
    } else if (prev.cycle_status === "running" && s.cycle_status === "done") {
      notify(`Cycle ${s.cycle_id} done ✓ ${cost}`, { kind: "success", pose: "happy" });
    } else if (
      prev.cycle_status === "running" &&
      (s.cycle_status === "errored" || s.cycle_status === "aborted")
    ) {
      notify(`Cycle ${s.cycle_id} ${s.cycle_status} ✗`, {
        kind: "error",
        pose: "sad",
        priority: "high",
        duration: 3200,
      });
    } else if (s.open_prs > prev.open_prs) {
      const d = s.open_prs - prev.open_prs;
      const label = s.latest_pr_number
        ? (d === 1 ? `PR #${s.latest_pr_number} opened` : `+${d} PRs, newest #${s.latest_pr_number}`)
        : (d === 1 ? "+1 PR opened" : `+${d} PRs opened`);
      notify(label, { kind: "success", url: s.latest_pr_url || null });
    } else if (s.findings_in_cycle > prev.findings_in_cycle && s.findings_in_cycle >= 5) {
      notify(`${s.findings_in_cycle} findings this cycle`, { kind: "warn" });
    }
  }

  if (s.cycle_status === "running") pet.classList.add("flapping");
  else pet.classList.remove("flapping");

  switch (s.cycle_status) {
    case "running":
      setBadge(`▶ ${s.findings_in_cycle}f / ${cost}`, "running"); break;
    case "errored":
    case "aborted":
      setBadge(`✗ ${cost}`, "error"); break;
    case "done":
      setBadge(`✓ ${cost}`, "success"); break;
    case "scan-empty":
      setBadge(`empty · ${cost}`, ""); break;
    default:
      setBadge(`idle · ${cost}`, "");
  }

  lastState = s;
  if (panelOpen) renderPanel();
}

// Eye tracking is disabled with the PNG body — the painted eyes are
// fixed; trying to overlay movable pupils on top causes a double-eye
// artifact. Path B chose the PNG's polish over the live-glance effect.

window.addEventListener("DOMContentLoaded", async () => {
  scheduleBlink();
  welcomeHint();

  // Delegate panel-history clicks → open URL.
  $("panel-history").addEventListener("click", (e) => {
    const li = e.target.closest("li");
    if (li && li.dataset.url) {
      e.stopPropagation();
      openUrl(li.dataset.url);
    }
  });

  const body = document.body;

  body.addEventListener("mousedown", async (e) => {
    if (e.button === 0 && !e.target.closest(".panel")) {
      try { await appWindow.startDragging(); } catch (err) { console.warn(err); }
    }
  });

  body.addEventListener("click", (e) => {
    if (e.button !== 0) return;
    if (e.target.closest(".panel")) return;
    squish();
    togglePanel();
  });

  body.addEventListener("contextmenu", async (e) => {
    e.preventDefault();
    squish();
    try { await invoke("show_menu"); } catch (err) { console.error(err); }
  });

  try {
    await listen("pet-state", (event) => applyState(event.payload));
  } catch (err) {
    console.warn("pet-state subscribe failed:", err);
  }

  // One-shot greeting with the last-24h roll-up. Emitted by Rust ~2.5s
  // after launch when there's actually something to report.
  try {
    await listen("pet-daily-summary", (event) => {
      const s = event.payload;
      const cost = `$${s.total_cost_usd.toFixed(2)}`;
      notify(
        `24h: ${s.cycles}c · ${s.findings}f · ${s.prs_opened}p · ${cost}`,
        { kind: "info", duration: 4500, pose: "happy" }
      );
    });
  } catch (err) {
    console.warn("pet-daily-summary subscribe failed:", err);
  }
});

window.petSay = (text, opts) => notify(text, opts || {});
window.petBadge = setBadge;
