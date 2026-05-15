const { invoke } = window.__TAURI__.core;
const { getCurrentWindow } = window.__TAURI__.window;

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

// Idle blink — natural cadence with jitter.
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

// Hint for first-time users: small bubble that fades.
function welcomeHint() {
  setTimeout(() => showBubble("右键看菜单 →", 3000), 800);
}

window.addEventListener("DOMContentLoaded", () => {
  scheduleBlink();
  welcomeHint();

  const body = document.body;

  // Left-button drag to move the frameless window.
  body.addEventListener("mousedown", async (e) => {
    if (e.button === 0) {
      try { await appWindow.startDragging(); } catch (err) { console.warn(err); }
    }
  });

  // Click reaction: squish + brief acknowledgement.
  body.addEventListener("click", (e) => {
    if (e.button === 0) {
      squish();
    }
  });

  // Right-click: ask Rust to pop up the menu at the current cursor.
  body.addEventListener("contextmenu", async (e) => {
    e.preventDefault();
    squish();
    try { await invoke("show_menu"); } catch (err) { console.error(err); }
  });
});

// Public hooks for Rust to invoke after menu actions land. Keeps the
// pet "responsive" — the rust handler can call into JS via emit/event,
// but for Phase 2 we expose simple globals as a stepping stone.
window.petSay = showBubble;
window.petBadge = setBadge;
