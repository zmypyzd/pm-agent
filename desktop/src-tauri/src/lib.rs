use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    AppHandle, WebviewWindow,
};

// pm-agent installs to ~/.local/bin via `uv tool install`, which is not on
// the PATH inherited by macOS GUI apps. `sh -lc` loads the user's login
// shell profile so PATH includes their install location.
fn spawn_pm_agent(args: &[&str]) {
    let mut cmd = String::from("pm-agent");
    for a in args {
        cmd.push(' ');
        cmd.push_str(a);
    }
    let _ = std::process::Command::new("sh")
        .arg("-lc")
        .arg(&cmd)
        .spawn();
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
        .invoke_handler(tauri::generate_handler![show_menu])
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
