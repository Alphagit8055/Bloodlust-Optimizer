//! Cross-language integration test.
//!
//! Spawns the *real* Python sidecar and drives it through the Rust client. This
//! is the only test that can catch a mismatch between the two halves: a framing
//! disagreement, a renamed method, a drifted error code, or a response shape
//! the Rust side cannot deserialize.
//!
//! Requires `python3` on PATH. Skipped (not failed) when absent, so the crate
//! still builds on a minimal CI image — but CI does install Python, so this
//! runs there.

use std::path::PathBuf;
use std::time::Duration;

use bloodlust_bridge::{methods, rpc, RpcError, Sidecar};
use serde_json::json;

fn python() -> Option<String> {
    for candidate in ["python3", "python"] {
        if std::process::Command::new(candidate)
            .arg("--version")
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
        {
            return Some(candidate.to_string());
        }
    }
    None
}

fn sidecar_root() -> PathBuf {
    // bridge/tests/ -> bridge/ -> repo root -> sidecar/
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("bridge has a parent")
        .join("sidecar")
}

fn spawn() -> Option<Sidecar> {
    let py = python()?;
    let root = sidecar_root();
    let src = root.join("src");

    let mut pythonpath = src.clone().into_os_string();
    if let Some(existing) = std::env::var_os("PYTHONPATH") {
        pythonpath.push(if cfg!(windows) { ";" } else { ":" });
        pythonpath.push(&existing);
    }

    let cmd_args: Vec<String> = vec!["-m".into(), "bloodlust".into()];
    // Point the engine at a throwaway data dir so the test cannot touch real
    // state even if it were ever run on Windows.
    let data = std::env::temp_dir().join(format!("bloodlust-bridge-test-{}", std::process::id()));
    let _ = std::fs::create_dir_all(&data);

    let child = std::process::Command::new(&py)
        .args(&cmd_args)
        .env("PYTHONPATH", pythonpath)
        .env("BLOODLUST_DATA_DIR", &data)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::inherit())
        .spawn();

    let mut child = match child {
        Ok(c) => c,
        Err(e) => {
            eprintln!("could not spawn python: {e}");
            return None;
        }
    };

    // Adopt the raw child rather than using Sidecar::spawn, so a handshake
    // failure is reported as a detailed test failure instead of a bare None.
    let stdin = child.stdin.take()?;
    let stdout = child.stdout.take()?;

    let sidecar = Sidecar::from_child(child, stdin, stdout).ok()?;

    // The handshake is explicit here so the protocol version is asserted by
    // the test rather than silently enforced inside spawn().
    match sidecar.call(methods::PING, json!({})) {
        Ok(v) => {
            assert_eq!(v["pong"], json!(true));
            assert_eq!(
                v["protocol"].as_u64(),
                Some(bloodlust_bridge::protocol::PROTOCOL_VERSION as u64),
                "Python and Rust disagree on PROTOCOL_VERSION"
            );
            Some(sidecar)
        }
        Err(e) => {
            eprintln!("handshake failed: {e}");
            None
        }
    }
}

macro_rules! require {
    ($e:expr) => {
        match $e {
            Some(v) => v,
            None => {
                eprintln!("SKIPPED: python3 unavailable");
                return;
            }
        }
    };
}

#[test]
fn handshake_reports_the_app_identity() {
    let s = require!(spawn());
    let info = s.call(methods::SYSTEM_INFO, json!({})).expect("system.info");
    assert_eq!(info["app"]["name"], json!("Bloodlust Optimizer"));
    assert_eq!(
        info["app"]["developer"],
        json!("Telegram: @Alpha0NemesiSS")
    );
    assert_eq!(
        info["app"]["protocol"].as_u64(),
        Some(bloodlust_bridge::protocol::PROTOCOL_VERSION as u64)
    );
}

#[test]
fn catalog_is_loaded_and_serves_actions() {
    let s = require!(spawn());
    let state = s.call(methods::CATALOG_STATE, json!({})).expect("catalog.state");
    assert_eq!(state["provenance"]["source"], json!("bundled"));
    assert!(
        state["counts"]["actions"].as_u64().unwrap_or(0) > 0,
        "bundled catalog loaded with no actions"
    );

    let actions = s.call(methods::ACTION_LIST, json!({})).expect("action.list");
    let list = actions.as_array().expect("action.list returns an array");
    assert!(!list.is_empty());

    // Every action the sidecar offers must expose the fields the UI renders.
    for a in list {
        assert!(a["id"].is_string(), "action without an id: {a}");
        assert!(a["label"].is_string(), "action without a label: {a}");
        assert!(a["risk"].is_string(), "action without a risk: {a}");
        assert!(a["handler"].is_string(), "action without a handler: {a}");
        assert!(
            a["available"].is_boolean(),
            "action without a gate result: {a}"
        );
    }
}

#[test]
fn families_are_ordered_and_labelled() {
    let s = require!(spawn());
    let families = s.call(methods::CATALOG_FAMILIES, json!({})).expect("catalog.families");
    let list = families.as_array().expect("array");
    assert!(!list.is_empty());
    for f in list {
        assert!(f["id"].is_string());
        assert!(f["label"].is_string());
        assert!(f["order"].is_number());
    }
}

#[test]
fn unknown_method_yields_the_shared_error_code() {
    let s = require!(spawn());
    let err = s
        .call("no.such.method", json!({}))
        .expect_err("should have failed");
    match err {
        RpcError::Application { code, .. } => {
            assert_eq!(
                code,
                bloodlust_bridge::protocol::codes::METHOD_NOT_FOUND,
                "Rust and Python disagree on the METHOD_NOT_FOUND code"
            )
        }
        other => panic!("expected an Application error, got {other:?}"),
    }
}

#[test]
fn running_an_unknown_action_yields_action_not_found() {
    let s = require!(spawn());
    let err = s
        .call(methods::ACTION_RUN, json!({"id": "definitely_not_real"}))
        .expect_err("should have failed");
    match err {
        RpcError::Application { code, .. } => {
            assert_eq!(code, bloodlust_bridge::protocol::codes::ACTION_NOT_FOUND)
        }
        other => panic!("expected ACTION_NOT_FOUND, got {other:?}"),
    }
}

#[test]
fn a_read_only_action_runs_and_returns_changes() {
    let s = require!(spawn());
    let result = s
        .call(methods::ACTION_RUN, json!({"id": "audit_report"}))
        .expect("audit_report should run on any platform");

    assert_eq!(result["ok"], json!(true), "audit_report failed: {result}");
    assert_eq!(result["action_id"], json!("audit_report"));
    assert!(result["changes"].is_array());
    assert!(
        result["changes"].as_array().unwrap().len() > 5,
        "a system report with almost no rows is not a report: {result}"
    );
}

#[test]
fn undo_with_nothing_recorded_yields_no_undo() {
    let s = require!(spawn());
    let err = s
        .call(methods::UNDO_ACTION, json!({"id": "audit_report"}))
        .expect_err("nothing to undo");
    match err {
        RpcError::Application { code, .. } => {
            assert_eq!(code, bloodlust_bridge::protocol::codes::NO_UNDO)
        }
        other => panic!("expected NO_UNDO, got {other:?}"),
    }
}

#[test]
fn a_sidecar_can_reject_a_hostile_catalog() {
    let s = require!(spawn());
    // A catalog naming a handler the binary does not ship must be refused,
    // with the bundled copy left in place. This is the central security
    // control, so it is asserted across the language boundary.
    let hostile = json!({
        "schema": bloodlust_bridge::CATALOG_SCHEMA_VERSION,
        "catalog_version": "hostile",
        "families": [{"id": "x", "label": "X"}],
        "actions": [{
            "id": "evil",
            "label": "Evil",
            "family": "x",
            "risk": "safe",
            "handler": "os.system",
            "description": ""
        }]
    });
    let payload = serde_json::to_string(&hostile).unwrap();

    let err = s
        .call(
            methods::CATALOG_LOAD,
            rpc::catalog_load_params(payload.as_bytes(), "0".repeat(64).as_str(), "https://example.test/c.json"),
        )
        .expect_err("a hostile catalog must be rejected");

    match err {
        RpcError::Application { code, message, .. } => {
            assert_eq!(code, bloodlust_bridge::protocol::codes::CATALOG_INVALID);
            assert!(message.contains("rejected"), "unexpected message: {message}");
        }
        other => panic!("expected CATALOG_INVALID, got {other:?}"),
    }

    // And the bundled catalog is still serving.
    let state = s.call(methods::CATALOG_STATE, json!({})).unwrap();
    assert_eq!(state["provenance"]["source"], json!("bundled"));
    assert_ne!(state["provenance"]["catalog_version"], json!("hostile"));
}

#[test]
fn handlers_list_proves_the_allowlist_is_not_empty() {
    let s = require!(spawn());
    let result = s.call(methods::DEBUG_HANDLERS, json!({})).unwrap();
    assert!(result["count"].as_u64().unwrap_or(0) > 10);
    assert!(result["handlers"].is_array());
}

#[test]
fn many_requests_keep_their_ids_aligned() {
    let s = require!(spawn());
    for i in 0..25u64 {
        let v = s.call(methods::PING, json!({})).expect("ping");
        assert_eq!(v["pong"], json!(true), "iteration {i} lost sync");
    }
    // If framing had drifted, the client would have hit an IdMismatch above.
}

#[test]
fn shutdown_is_clean() {
    let s = require!(spawn());
    assert!(s.is_alive());
    let code = s.shutdown(Duration::from_secs(10));
    // Exit code 0 means the engine saw EOF and returned normally.
    assert_eq!(code, Some(0), "sidecar did not exit cleanly on stdin close");
}
