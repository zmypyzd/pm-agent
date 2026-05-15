const { invoke } = window.__TAURI__.core;
const { getCurrentWindow } = window.__TAURI__.window;
const { listen } = window.__TAURI__.event;

const appWindow = getCurrentWindow();

const $ = (id) => document.getElementById(id);

let bubbleTimer = null;
function showBubble(text, ms = 2400) {
  const b = $("bubble");
  b.textContent = text;
  b.classList.remove("hidden");
  if (bubbleTimer) clearTimeout(bubbleTimer);
  bubbleTimer = setTimeout(() => b.classList.add("hidden"), ms);
}

function setBadge(text, state) {
  const badge = $("badge");
  badge.textContent = text;
  badge.className = "badge" + (state ? " " + state : "");
}

function squish() {
  const pet = $("pet");
  pet.classList.remove("squish");
  void pet.offsetWidth; // restart animation
  pet.classList.add("squish");
  setTimeout(() => pet.classList.remove("squish"), 320);
}

function scheduleBlink() {
  const wait = 2200 + Math.random() * 3200;
  setTimeout(() => {
    const lid = $("eyelid");
    lid.classList.add("blink");
    setTimeout(() => {
      lid.classList.remove("blink");
      scheduleBlink();
    }, 110);
  }, wait);
}

function welcomeHint() {
  setTimeout(() => showBubble("右键看菜单 →", 3000), 800);
}

// Translate a state snapshot into badge text/style + optional bubble.
let lastState = null;
function applyState(s) {
  if (!s || !s.db_exists) {
    setBadge("idle", "");
    lastState = s;
    return;
  }

  const cost = `$${s.cumulative_cost_usd.toFixed(2)}`;
  const prev = lastState;

  // Transition bubbles — only fire when the meaningful field changed.
  if (prev && prev.db_exists) {
    if (s.cycle_id !== prev.cycle_id && s.cycle_status === "running") {
      showBubble(`Cycle ${s.cycle_id} started ▶`);
    } else if (prev.cycle_status === "running" && s.cycle_status === "done") {
      showBubble(`Cycle ${s.cycle_id} done ✓`);
    } else if (prev.cycle_status === "running" && s.cycle_status === "errored") {
      showBubble(`Cycle ${s.cycle_id} errored ✗`);
    } else if (s.open_prs > prev.open_prs) {
      const delta = s.open_prs - prev.open_prs;
      showBubble(delta === 1 ? "+1 PR opened" : `+${delta} PRs opened`);
    }
  }

  const pet = $("pet");
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

  // Subscribe to state snapshots from the Rust polling task.
  try {
    await listen("pet-state", (event) => applyState(event.payload));
  } catch (err) {
    console.warn("pet-state subscribe failed:", err);
  }
});

window.petSay = showBubble;
window.petBadge = setBadge;
