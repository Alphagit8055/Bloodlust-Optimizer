//! Bloodlust Optimizer — desktop shell.
//!
//! The shell is thin by design. Protocol handling, catalog verification and the
//! sidecar client live in the `bridge` crate, which is unit-tested and
//! integration-tested against the real Python engine on Linux CI (where this
//! binary cannot build, because Tauri needs webkit).
//!
//! Keeping the testable logic out of the Tauri binary is what makes that
//! possible, and it is the single most useful structural decision here.

mod commands;

use commands::AppState;

pub fn run() {
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_dialog::init());

    commands::register_handlers(builder)
        .run(tauri::generate_context!())
        .expect("error while running Bloodlust Optimizer");
}

/// Exposed so an integration test could drive the shell headlessly.
pub fn app_state() -> AppState {
    AppState::new()
}
