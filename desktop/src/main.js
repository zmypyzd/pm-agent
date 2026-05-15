const { invoke } = window.__TAURI__.core;
const { getCurrentWindow } = window.__TAURI__.window;
const { listen } = window.__TAURI__.event;

const appWindow = getCurrentWindow();

const $ = (id) => document.getElementById(id);

// ---------- Notification queue ----------
// The duck IS the notifier — so let multiple events line up instead of
// clobbering each other. Errors jump the line.
const queue = [];
let queueRunning = false;
let currentTimer = null;

function notify(text, opts = {}) {
  const item = {
    text,
    duration: opts.duration || 2400,
    kind: opts.kind || "info",
    pose: opts.pose || null,
  };
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
  renderBubble(n.text, n.kind);
  if (n.pose) triggerPose(n.pose);
  currentTimer = setTimeout(() => {
    hideBubble();
    setTimeout(runQueue, 220);
  }, n.duration);
}

function renderBubble(text, kind) {
  const b = $("bubble");
  b.textContent = text;
  b.className = "bubble " + (kind || "");
}

function hideBubble() {
  const b = $("bubble");
  b.classList.add("hidden");
}

// ---------- Pose animations ----------
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

function squish() {
  triggerPose("squish");
}

// ---------- Idle behaviours ----------
function scheduleBlink() {
  const wait = 2200 + Math.random() * 3200;
  setTimeout(() => {
    if (!$("pet").classList.contains("sleeping")) {
      const lid = $("eyelid");
      lid.classList.add("blink");
      setTimeout(() => lid.classList.remove("blink"), 110);
    }
    scheduleBlink();
  }, wait);
}

function welcomeHint() {
  setTimeout(() => notify("右键看菜单 →", { duration: 3000 }), 800);
}

// ---------- State → presentation ----------
let lastState = null;
let sleepTimer = null;

function applyState(s) {
  const pet = $("pet");

  if (!s || !s.db_exists) {
    setBadge("idle", "");
    // Drift to "sleeping" after 30s with no state at all.
    if (!sleepTimer) {
      sleepTimer = setTimeout(() => pet.classList.add("sleeping"), 30000);
    }
    lastState = s;
    return;
  }

  // Any real state wakes the duck.
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
      notify(d === 1 ? "+1 PR opened" : `+${d} PRs opened`, { kind: "success" });
    } else if (s.findings_in_cycle > prev.findings_in_cycle && s.findings_in_cycle >= 5) {
      notify(`${s.findings_in_cycle} findings this cycle`, { kind: "warn" });
    }
  }

  if (s.cycle_status === "running") {
    pet.classList.add("flapping");
  } else {
    pet.classList.remove("flapping");
  }

  switch (s.cycle_status) {
    case "running":
      setBadge(`▶ ${s.findings_in_cycle}f / ${cost}`, "running");
      break;
    case "errored":
    case "aborted":
      setBadge(`✗ ${cost}`, "error");
      break;
    case "done":
      setBadge(`✓ ${cost}`, "success");
      break;
    case "scan-empty":
      setBadge(`empty · ${cost}`, "");
      break;
    default:
      setBadge(`idle · ${cost}`, "");
  }

  lastState = s;
}

window.addEventListener("DOMContentLoaded", async () => {
  scheduleBlink();
  welcomeHint();

  const body = document.body;

  body.addEventListener("mousedown", async (e) => {
    if (e.button === 0) {
      try { await appWindow.startDragging(); } catch (err) { console.warn(err); }
    }
  });

  body.addEventListener("click", (e) => {
    if (e.button === 0) squish();
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
});

window.petSay = (text, opts) => notify(text, opts || {});
window.petBadge = setBadge;
