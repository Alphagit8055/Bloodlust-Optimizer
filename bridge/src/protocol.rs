//! The wire contract shared with the Python sidecar.
//!
//! Every constant here has a mirror in `sidecar/src/bloodlust`. If you change
//! one, change the other, and bump `PROTOCOL_VERSION` in both places.
//! `sidecar/tests/test_rpc.py` and `bridge/tests/sidecar_roundtrip.rs` fail
//! loudly when they disagree.

/// Bumped whenever the wire format changes incompatibly.
pub const PROTOCOL_VERSION: u32 = 1;

/// Catalog schema version this build understands.
pub const CATALOG_SCHEMA_VERSION: u32 = 1;

pub const APP_NAME: &str = "Bloodlust Optimizer";
pub const APP_VERSION: &str = "0.1.0";
pub const DEVELOPER: &str = "Telegram: @Alpha0NemesiSS";

/// JSON-RPC error codes. The application range must match
/// `sidecar/src/bloodlust/rpc/errors.py`.
pub mod codes {
    pub const PARSE_ERROR: i64 = -32700;
    pub const INVALID_REQUEST: i64 = -32600;
    pub const METHOD_NOT_FOUND: i64 = -32601;
    pub const INVALID_PARAMS: i64 = -32602;
    pub const INTERNAL_ERROR: i64 = -32603;

    pub const NOT_READY: i64 = -32001;
    pub const ACTION_NOT_FOUND: i64 = -32002;
    pub const ACTION_BLOCKED: i64 = -32003;
    pub const ACTION_FAILED: i64 = -32004;
    pub const NO_UNDO: i64 = -32005;
    pub const NEEDS_ELEVATION: i64 = -32006;
    pub const CATALOG_INVALID: i64 = -32007;
    pub const PROTOCOL_MISMATCH: i64 = -32008;
    pub const SESSION_ACTIVE: i64 = -32009;
}

/// Method names. Kept as constants so a typo is a compile error, not a
/// mysterious -32601 at runtime.
pub mod methods {
    pub const PING: &str = "ping";
    pub const SYSTEM_INFO: &str = "system.info";
    pub const SYSTEM_EMULATORS: &str = "system.emulators";
    pub const SYSTEM_REFRESH: &str = "system.refresh";

    pub const CATALOG_LOAD: &str = "catalog.load";
    pub const CATALOG_STATE: &str = "catalog.state";
    pub const CATALOG_FAMILIES: &str = "catalog.families";

    pub const ACTION_LIST: &str = "action.list";
    pub const ACTION_DESCRIBE: &str = "action.describe";
    pub const ACTION_PREVIEW: &str = "action.preview";
    pub const ACTION_RUN: &str = "action.run";
    pub const BATCH_RUN: &str = "batch.run";

    pub const UNDO_LIST: &str = "undo.list";
    pub const UNDO_ACTION: &str = "undo.action";
    pub const UNDO_ALL: &str = "undo.all";

    pub const STATE_GET: &str = "state.get";
    pub const STATE_SET: &str = "state.set";
    pub const RISK_SET_CEILING: &str = "risk.setCeiling";

    pub const LOG_TAIL: &str = "log.tail";
    pub const DEBUG_HANDLERS: &str = "debug.handlers";
    pub const DEBUG_BLOCKS: &str = "debug.blocks";
}

/// The line terminator. Both sides must agree; the sidecar writes `\n` even on
/// Windows (it reconfigures stdout with `newline="\n"`).
pub const FRAME_TERMINATOR: u8 = b'\n';

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn error_codes_match_the_python_side() {
        // Mirrors sidecar/src/bloodlust/rpc/errors.py exactly.
        assert_eq!(codes::PARSE_ERROR, -32700);
        assert_eq!(codes::INVALID_REQUEST, -32600);
        assert_eq!(codes::METHOD_NOT_FOUND, -32601);
        assert_eq!(codes::INVALID_PARAMS, -32602);
        assert_eq!(codes::INTERNAL_ERROR, -32603);
        assert_eq!(codes::NOT_READY, -32001);
        assert_eq!(codes::CATALOG_INVALID, -32007);
    }

    #[test]
    fn method_names_use_the_dotted_convention() {
        for m in [
            methods::PING,
            methods::SYSTEM_INFO,
            methods::CATALOG_LOAD,
            methods::ACTION_RUN,
            methods::UNDO_ALL,
            methods::DEBUG_BLOCKS,
        ] {
            assert!(!m.is_empty());
            assert!(
                m.chars().all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '_'),
                "{m} contains a character the Python dispatcher will not accept"
            );
        }
    }
}
