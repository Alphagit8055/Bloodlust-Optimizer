//! Application state and Tauri command surface.
//!
//! This binary is deliberately thin. Everything that can be tested lives in
//! the `bridge` crate; what remains here is spawning the engine, resolving its
//! path inside the installed bundle, and forwarding calls to the webview.
//!
//! # Why the shell owns the network
//!
//! The sidecar runs elevated and writes to the registry. It performs no network
//! I/O at all. This shell fetches the catalog and hands the bytes over. One
//! process talks to the internet, a different process talks to the machine, and
//! the boundary between them is a single validated message.

use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;

use bloodlust_bridge::{catalog, methods, protocol, rpc, Sidecar};
use serde_json::{json, Value};
use tauri::{AppHandle, Manager, State};

use thiserror::Error;

/// Repository the catalog is served from.
///
/// Change these three constants when you fork. Everything else derives from
/// them, and the host allowlist in `bridge` already permits
/// `raw.githubusercontent.com`.
pub const REPO_OWNER: &str = "Alphagit8055";
pub const REPO_NAME: &str = "Bloodlust-Optimizer";
pub const CATALOG_REF: &str = "main";
pub const CATALOG_PATH: &str = "catalog/catalog.json";

const SIDECAR_SHUTDOWN_GRACE: Duration = Duration::from_secs(5);

#[derive(Debug, Error)]
pub enum AppError {
    #[error("the tuning engine is not running")]
    EngineDown,

    #[error("the tuning engine failed: {0}")]
    Engine(String),

    #[error("could not locate the tuning engine binary: {0}")]
    MissingSidecar(String),

    #[error("catalog: {0}")]
    Catalog(String),
}

// Tauri needs errors that serialize so the webview sees a useful message
// rather than an opaque rejection.
impl serde::Serialize for AppError {
    fn serialize<S: serde::Serializer>(&self, s: S) -> std::result::Result<S::Ok, S::Error> {
        serde::Serialize::serialize(&self.to_string(), s)
    }
}

impl From<rpc::RpcError> for AppError {
    fn from(e: rpc::RpcError) -> Self {
        match &e {
            rpc::RpcError::Eof | rpc::RpcError::Pipe => AppError::EngineDown,
            rpc::RpcError::Application { message, .. } => AppError::Engine(message.clone()),
            other => AppError::Engine(other.to_string()),
        }
    }
}

type Result<T> = std::result::Result<T, AppError>;

/// Everything the commands need, behind one mutex.
pub struct AppState {
    sidecar: Mutex<Option<Sidecar>>,
    /// Last catalog provenance, for display even when the engine is down.
    pub provenance: Mutex<Option<Value>>,
}

impl AppState {
    pub fn new() -> Self {
        Self {
            sidecar: Mutex::new(None),
            provenance: Mutex::new(None),
        }
    }

    fn with<T>(&self, f: impl FnOnce(&Sidecar) -> Result<T>) -> Result<T> {
        let guard = self
            .sidecar
            .lock()
            .map_err(|_| AppError::EngineDown)?;
        let sidecar = guard.as_ref().ok_or(AppError::EngineDown)?;
        f(sidecar)
    }

    fn call(&self, method: &str, params: Value) -> Result<Value> {
        self.with(|s| Ok(s.call(method, params)?))
    }
}

// ------------------------------------------------------------------- sidecar


/// Locate the packaged engine next to the executable.
///
/// In a NSIS install the sidecar sits in the same directory as the app; in a
/// dev checkout it is whatever `BLOODLUST_SIDECAR` points at, so `npm run dev`
/// works without a PyInstaller build.
fn resolve_sidecar(app: &AppHandle) -> std::result::Result<(PathBuf, Vec<String>), AppError> {
    if let Ok(path) = std::env::var("BLOODLUST_SIDECAR") {
        return Ok((PathBuf::from(path), Vec::new()));
    }

    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .ok_or_else(|| AppError::MissingSidecar("cannot resolve the executable directory".into()))?;

    let candidates = if cfg!(windows) {
        vec![
            exe_dir.join("bloodlust-sidecar.exe"),
            exe_dir.join("sidecar").join("bloodlust-sidecar.exe"),
        ]
    } else {
        vec![
            exe_dir.join("bloodlust-sidecar"),
            exe_dir.join("sidecar").join("bloodlust-sidecar"),
        ]
    };

    for c in &candidates {
        if c.is_file() {
            return Ok((c.clone(), Vec::new()));
        }
    }

    // Also honour the resource dir, where tauri.conf.json places it.
    //
    // The bundle ships the engine at `sidecar/bloodlust-sidecar[_exe]`, so the
    // nested path is checked first. Only checking the resource root silently
    // missed the engine in a release install even though it was packaged.
    if let Ok(resource) = app.path().resource_dir() {
        let name = if cfg!(windows) {
            "bloodlust-sidecar.exe"
        } else {
            "bloodlust-sidecar"
        };
        for c in [resource.join("sidecar").join(name), resource.join(name)] {
            if c.is_file() {
                return Ok((c, Vec::new()));
            }
        }
    }

    Err(AppError::MissingSidecar(
        candidates
            .iter()
            .map(|p| p.display().to_string())
            .collect::<Vec<_>>()
            .join(", "),
    ))
}

// ------------------------------------------------------------------ commands


/// Start the engine, then try the remote catalog, then report.
///
/// Never fails because of the catalog: a GitHub outage must not stop the app
/// working from its bundled copy.
#[tauri::command]
pub async fn bootstrap(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<Value> {
    if state.sidecar.lock().unwrap().is_some() {
        return Ok(json!({"already_running": true}));
    }

    let (program, args) = resolve_sidecar(&app)?;
    let program_str = program.to_string_lossy().into_owned();

    let sidecar = Sidecar::spawn(&program_str, &args).map_err(AppError::from)?;

    // Adopt into shared state before any catalog work, so a catalog failure
    // still leaves a usable engine.
    *state.sidecar.lock().unwrap() = Some(sidecar.clone());

    let catalog_result = refresh_catalog_inner(&state).await;

    let info = state.call(methods::SYSTEM_INFO, json!({}))?;
    let catalog_state = state.call(methods::CATALOG_STATE, json!({})).unwrap_or(json!({}));

    Ok(json!({
        "system": info,
        "catalog": catalog_state,
        "catalog_refresh": match catalog_result {
            Ok(v) => v,
            Err(e) => json!({"ok": false, "error": e.to_string()}),
        },
        "repo": {"owner": REPO_OWNER, "name": REPO_NAME, "ref": CATALOG_REF},
    }))
}

async fn refresh_catalog_inner(state: &AppState) -> Result<Value> {
    let url = bloodlust_bridge::catalog_url(REPO_OWNER, REPO_NAME, CATALOG_REF, CATALOG_PATH);

    let payload = catalog::fetch(&url, bloodlust_bridge::CATALOG_HOSTS)
        .map_err(|e| AppError::Catalog(e.to_string()))?;

    let params = rpc::catalog_load_params(&payload.bytes, &payload.sha256, &url);
    let result = state.call(methods::CATALOG_LOAD, params)?;

    *state.provenance.lock().unwrap() = Some(json!({
        "url": url,
        "sha256": payload.sha256,
        "bytes": payload.bytes.len(),
        "accepted": result["ok"],
    }));

    Ok(json!({
        "ok": true,
        "url": url,
        "sha256": payload.sha256,
        "actions": result["actions"],
    }))
}

/// Pull the latest catalog from GitHub. Safe to call repeatedly.
#[tauri::command]
pub async fn refresh_catalog(state: State<'_, AppState>) -> Result<Value> {
    refresh_catalog_inner(&state).await
}

/// The family list, with the labels and ordering the catalog declares.
#[tauri::command]
pub async fn catalog_families(state: State<'_, AppState>) -> Result<Value> {
    state.call(methods::CATALOG_FAMILIES, json!({}))
}

/// Show where the active catalog came from, whether or not GitHub is reachable.
#[tauri::command]
pub async fn catalog_provenance(state: State<'_, AppState>) -> Result<Value> {
    let remote = state.provenance.lock().unwrap().clone();
    let live = state.call(methods::CATALOG_STATE, json!({}))?;
    Ok(json!({
        "remote": remote,
        "active": live["provenance"],
        "counts": live["counts"],
        "notice": live["notice"],
    }))
}

// Thin forwards. Each is one line so there is nowhere for logic to hide.

#[tauri::command]
pub async fn system_info(state: State<'_, AppState>) -> Result<Value> {
    state.call(methods::SYSTEM_INFO, json!({}))
}

#[tauri::command]
pub async fn system_emulators(state: State<'_, AppState>) -> Result<Value> {
    state.call(methods::SYSTEM_EMULATORS, json!({}))
}

#[tauri::command]
pub async fn system_refresh(state: State<'_, AppState>) -> Result<Value> {
    state.call(methods::SYSTEM_REFRESH, json!({}))
}

#[tauri::command]
pub async fn action_list(state: State<'_, AppState>, family: Option<String>) -> Result<Value> {
    let params = match family {
        Some(f) => json!({"family": f}),
        None => json!({}),
    };
    state.call(methods::ACTION_LIST, params)
}

#[tauri::command]
pub async fn action_describe(state: State<'_, AppState>, id: String) -> Result<Value> {
    state.call(methods::ACTION_DESCRIBE, json!({"id": id}))
}

#[tauri::command]
pub async fn action_preview(
    state: State<'_, AppState>,
    id: String,
    args: Option<Value>,
) -> Result<Value> {
    state.call(methods::ACTION_PREVIEW, json!({"id": id, "args": args}))
}

#[tauri::command]
pub async fn action_run(
    state: State<'_, AppState>,
    id: String,
    args: Option<Value>,
) -> Result<Value> {
    state.call(methods::ACTION_RUN, json!({"id": id, "args": args}))
}

#[tauri::command]
pub async fn batch_run(
    state: State<'_, AppState>,
    mode: String,
    ids: Option<Vec<String>>,
    preview: Option<bool>,
) -> Result<Value> {
    state.call(
        methods::BATCH_RUN,
        json!({"mode": mode, "ids": ids, "preview": preview.unwrap_or(false)}),
    )
}

#[tauri::command]
pub async fn undo_list(state: State<'_, AppState>) -> Result<Value> {
    state.call(methods::UNDO_LIST, json!({}))
}

#[tauri::command]
pub async fn undo_action(state: State<'_, AppState>, id: String) -> Result<Value> {
    state.call(methods::UNDO_ACTION, json!({"id": id}))
}

#[tauri::command]
pub async fn undo_all(state: State<'_, AppState>) -> Result<Value> {
    state.call(methods::UNDO_ALL, json!({}))
}

#[tauri::command]
pub async fn risk_set_ceiling(state: State<'_, AppState>, ceiling: String) -> Result<Value> {
    state.call(methods::RISK_SET_CEILING, json!({"ceiling": ceiling}))
}

#[tauri::command]
pub async fn state_get(state: State<'_, AppState>) -> Result<Value> {
    state.call(methods::STATE_GET, json!({}))
}

#[tauri::command]
pub async fn log_tail(state: State<'_, AppState>, limit: Option<u32>) -> Result<Value> {
    state.call(methods::LOG_TAIL, json!({"limit": limit.unwrap_or(100)}))
}

#[tauri::command]
pub async fn about() -> Value {
    json!({
        "name": protocol::APP_NAME,
        "version": protocol::APP_VERSION,
        "developer": protocol::DEVELOPER,
        "protocol": protocol::PROTOCOL_VERSION,
        "catalog_schema": protocol::CATALOG_SCHEMA_VERSION,
        "repo": format!("{REPO_OWNER}/{REPO_NAME}"),
    })
}

/// Report whether the engine is alive, so the UI can show a clear error instead
/// of hanging on a dead pipe.
#[tauri::command]
pub async fn engine_status(state: State<'_, AppState>) -> Result<Value> {
    let alive = state
        .sidecar
        .lock()
        .ok()
        .and_then(|g| g.as_ref().map(|s| s.is_alive()))
        .unwrap_or(false);
    Ok(json!({"alive": alive}))
}

/// Build the plugin and command list. Kept separate so `main.rs` stays trivial.
///
/// Concrete `Wry` runtime throughout: `AppHandle` in the commands defaults to
/// `Wry`, and a generic `R: Runtime` here would let the handler demand
/// `CommandArg` for an arbitrary runtime, which the commands never satisfy.
pub fn register_handlers(
    builder: tauri::Builder<tauri::Wry>,
) -> tauri::Builder<tauri::Wry> {
    builder
        .manage(AppState::new())
        .invoke_handler(tauri::generate_handler![
            bootstrap,
            refresh_catalog,
            catalog_families,
            catalog_provenance,
            system_info,
            system_emulators,
            system_refresh,
            action_list,
            action_describe,
            action_preview,
            action_run,
            batch_run,
            undo_list,
            undo_action,
            undo_all,
            risk_set_ceiling,
            state_get,
            log_tail,
            about,
            engine_status,
        ])
        .on_window_event(|window, event| {
            // Closing the window must take the engine down with it, or an
            // orphaned elevated process is left behind.
            if let tauri::WindowEvent::Destroyed = event {
                if let Some(state) = window.app_handle().try_state::<AppState>() {
                    if let Ok(mut guard) = state.sidecar.lock() {
                        if let Some(sidecar) = guard.take() {
                            let _ = sidecar.shutdown(SIDECAR_SHUTDOWN_GRACE);
                        }
                    }
                }
            }
        })
}
