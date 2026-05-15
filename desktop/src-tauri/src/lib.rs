use std::path::PathBuf;
use std::time::Duration;

use serde::Serialize;
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    AppHandle, Emitter, WebviewWindow,
};

// pm-agent installs to ~/.local/bin via `uv tool install`, which is NOT on
// the PATH inherited by macOS GUI apps. `sh -lc` alone is unreliable here
// because zsh's login profile doesn't always add ~/.local/bin. So we
// explicitly inject the canonical uv-tool install locations into PATH.
fn spawn_pm_agent(args: &[&str]) {
    let home = std::env::var("HOME").unwrap_or_default();
    let extras = [
        format!("{}/.local/bin", home),
        format!("{}/.cargo/bin", home),
        "/opt/homebrew/bin".to_string(),
        "/usr/local/bin".to_string(),
    ];
    let current = std::env::var("PATH").unwrap_or_default();
    let augmented = format!("{}:{}", extras.join(":"), current);

    let mut cmd = String::from("pm-agent");
    for a in args {
        cmd.push(' ');
        cmd.push_str(a);
    }
    let _ = std::process::Command::new("sh")
        .env("PATH", &augmented)
        .arg("-lc")
        .arg(&cmd)
        .spawn();
}

fn state_db_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_default();
    PathBuf::from(home).join(".pm-agent").join("state.db")
}

#[derive(Serialize, Clone, Debug, PartialEq)]
struct StateSnapshot {
    db_exists: bool,
    cycle_id: Option<i64>,
    cycle_status: Option<String>,
    cumulative_cost_usd: f64,
    findings_in_cycle: i64,
    open_prs: i64,
}

impl StateSnapshot {
    fn empty() -> Self {
        Self {
            db_exists: false,
            cycle_id: None,
            cycle_status: None,
            cumulative_cost_usd: 0.0,
            findings_in_cycle: 0,
            open_prs: 0,
        }
    }
}

// Reads the pm-agent state.db in read-only mode. The daemon uses WAL,
// so concurrent reads don't block its writes. Any failure (missing file,
// schema mismatch, transient lock) collapses to the empty snapshot —
// the pet falls back to idle rather than spuriously flashing red.
fn read_state() -> StateSnapshot {
    let path = state_db_path();
    if !path.exists() {
        return StateSnapshot::empty();
    }
    let conn = match rusqlite::Connection::open_with_flags(
        &path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    ) {
        Ok(c) => c,
        Err(_) => return StateSnapshot::empty(),
    };

    let latest: Option<(i64, String)> = conn
        .query_row(
            "SELECT id, status FROM cycles ORDER BY started_at DESC LIMIT 1",
            [],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)),
        )
        .ok();

    let cumulative_cost: f64 = conn
        .query_row(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM cycles",
            [],
            |row| row.get(0),
        )
        .unwrap_or(0.0);

    let findings_in_cycle: i64 = match latest.as_ref().map(|(id, _)| *id) {
        Some(id) => conn
            .query_row(
                "SELECT COUNT(*) FROM findings WHERE cycle_id = ?1",
                [id],
                |row| row.get(0),
            )
            .unwrap_or(0),
        None => 0,
    };

    let open_prs: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM prs WHERE state = 'open'",
            [],
            |row| row.get(0),
        )
        .unwrap_or(0);

    StateSnapshot {
        db_exists: true,
        cycle_id: latest.as_ref().map(|(id, _)| *id),
        cycle_status: latest.map(|(_, st)| st),
        cumulative_cost_usd: cumulative_cost,
        findings_in_cycle,
        open_prs,
    }
}

// JS calls this to grow/shrink the window when the mini panel is
// shown/hidden. We keep this in Rust so the JS doesn't have to import
// LogicalSize through the global tauri namespace (which varies by build).
#[tauri::command]
async fn set_window_size(window: WebviewWindow, width: f64, height: f64) -> Result<(), String> {
    window
        .set_size(tauri::LogicalSize::new(width, height))
        .map_err(|e| e.to_string())
}

#[tauri::command]
async fn show_menu(app: AppHandle, window: WebviewWindow) -> Result<(), String> {
    let demo = MenuItem::with_id(&app, "demo", "▶  Run demo", true, None::<&str>)
        .map_err(|e| e.to_string())?;
    let lp = MenuItem::with_id(&app, "loop", "⚙  Start autonomous loop", true, None::<&str>)
        .map_err(|e| e.to_string())?;
    let dash = MenuItem::with_id(&app, "dashboard", "📊  Open dashboard", true, None::<&str>)
        .map_err(|e| e.to_string())?;
    let sep = PredefinedMenuItem::separator(&app).map_err(|e| e.to_string())?;
    let quit = MenuItem::with_id(&app, "quit", "Quit", true, None::<&str>)
        .map_err(|e| e.to_string())?;
    let menu = Menu::with_items(&app, &[&demo, &lp, &dash, &sep, &quit])
        .map_err(|e| e.to_string())?;
    window.popup_menu(&menu).map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        // Persists window position + size across launches at
        // ~/Library/Application Support/<bundle id>/window-state.json.
        // Restores on next start with no extra code.
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .invoke_handler(tauri::generate_handler![show_menu, set_window_size])
        .setup(|app| {
            // State polling: emit only on change so the frontend doesn't
            // re-render at 0.5 Hz for nothing. 2s cadence is well below
            // the daemon's per-cycle event rate.
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                let mut last: Option<StateSnapshot> = None;
                loop {
                    let snap = read_state();
                    if last.as_ref() != Some(&snap) {
                        let _ = handle.emit("pet-state", &snap);
                        last = Some(snap);
                    }
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
            });
            Ok(())
        })
        .on_menu_event(|app, event| match event.id().as_ref() {
            "demo" => spawn_pm_agent(&["demo"]),
            "loop" => spawn_pm_agent(&["loop", "run"]),
            "dashboard" => {
                spawn_pm_agent(&["dashboard", "serve"]);
                std::thread::sleep(std::time::Duration::from_millis(500));
                let _ = std::process::Command::new("sh")
                    .args(["-lc", "open http://127.0.0.1:8000"])
                    .spawn();
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .run(tauri::generate_context!())
        .expect("error while running pm-agent pet");
}
