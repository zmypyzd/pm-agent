const { invoke } = window.__TAURI__.core;
const { getCurrentWindow } = window.__TAURI__.window;

const appWindow = getCurrentWindow();

window.addEventListener("DOMContentLoaded", () => {
  const body = document.body;

  // Left-button drag to move the frameless window.
  body.addEventListener("mousedown", async (e) => {
    if (e.button === 0) {
      try { await appWindow.startDragging(); } catch (err) { console.warn(err); }
    }
  });

  // Right-click: ask Rust to pop up the menu at the current cursor. The
  // menu's items dispatch via `on_menu_event` in lib.rs.
  body.addEventListener("contextmenu", async (e) => {
    e.preventDefault();
    try { await invoke("show_menu"); } catch (err) { console.error(err); }
  });
});
