//! Bloodlust Optimizer — desktop shell.
//!
//! The shell is thin by design. Protocol handling, catalog verification and the
//! sidecar client live in the `bridge` crate, which is unit-tested and
//! integration-tested against the real Python engine.
//!
//! Keeping the testable logic out of the Tauri binary is what makes that
//! possible, and it is the single most useful structural decision here.

mod commands;

use commands::AppState;

/// The release build is a `windows`-subsystem binary: no console, so anything
/// written to stderr is invisible. A panic on any thread would otherwise take
/// the window down silently with nothing to tell the user (or us) what
/// happened. Mirror the panic — and any fatal exit error — into a file next
/// to the temp dir before dying.
fn install_crash_hook() {
    std::panic::set_hook(Box::new(|info| {
        let location = info
            .location()
            .map(|l| format!("{}:{}", l.file(), l.line()))
            .unwrap_or_else(|| "unknown location".into());
        let body = format!("bloodlust-optimizer panicked at {location}\n{info}\n");
        let _ = std::fs::write(crash_log_path(), body);
    }));
}

fn crash_log_path() -> std::path::PathBuf {
    std::env::temp_dir().join("bloodlust-crash.log")
}

pub fn run() {
    install_crash_hook();

    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_dialog::init());

    if let Err(e) = commands::register_handlers(builder).run(tauri::generate_context!()) {
        let body = format!("bloodlust-optimizer exited with error: {e}\n");
        let _ = std::fs::write(crash_log_path(), body);
        std::process::exit(1);
    }
}

/// Exposed so an integration test could drive the shell headlessly.
pub fn app_state() -> AppState {
    AppState::new()
}
