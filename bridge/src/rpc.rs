//! JSON-RPC 2.0 client over a child process's stdio.
//!
//! Framing must match `sidecar/src/bloodlust/rpc/server.py` exactly:
//! newline-delimited UTF-8 JSON, one object per `\n`-terminated line.
//!
//! Two rules this module exists to enforce:
//!
//! 1. **stdout is sacred.** The sidecar writes only JSON-RPC there. If a line
//!    does not parse, that is a protocol fault, and we surface it as one
//!    rather than silently desynchronising every later message.
//! 2. **Requests are serialised.** A `Mutex` around the whole write-then-read
//!    exchange keeps request and response aligned. Pipelining would need a
//!    reader task and an id-keyed response map; the sidecar is fast enough
//!    that this is not worth the complexity.

use std::io::{BufRead, BufReader, BufWriter, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::Deserialize;
use serde_json::{json, Map, Value};

use thiserror::Error;

#[derive(Debug, Error)]
pub enum RpcError {
    #[error("failed to start the engine: {0}")]
    Spawn(String),

    #[error("engine stdin/stdout unavailable")]
    Pipe,

    #[error("the engine closed its output")]
    Eof,

    #[error("engine sent a line that is not JSON: {0}")]
    MalformedFrame(String),

    #[error("engine replied to the wrong request id (expected {expected}, got {got:?})")]
    IdMismatch { expected: u64, got: Option<u64> },

    #[error("{message} (code {code})")]
    Application { code: i64, message: String, data: Option<Value> },

    #[error("failed to send the request: {0}")]
    Send(String),
}

/// The `error` object from a JSON-RPC response.
#[derive(Debug, Clone, Deserialize)]
pub struct RpcErrorObject {
    pub code: i64,
    pub message: String,
    #[serde(default)]
    pub data: Option<Value>,
}

/// A decoded response envelope.
#[derive(Debug, Clone, Deserialize)]
struct Envelope {
    #[serde(rename = "jsonrpc")]
    version: String,
    #[serde(default)]
    id: Option<u64>,
    #[serde(default)]
    result: Option<Value>,
    #[serde(default)]
    error: Option<RpcErrorObject>,
}

/// A live connection to the sidecar.
///
/// Cheap to clone: the transport is behind an `Arc<Mutex<..>>`, and every clone
/// shares the same child process. That lets several Tauri commands hold a
/// handle without the caller having to thread one through.
#[derive(Clone)]
pub struct Sidecar {
    inner: Arc<Mutex<Transport>>,
    next_id: Arc<AtomicU64>,
}

struct Transport {
    child: Child,
    stdin: Option<BufWriter<ChildStdin>>,
    stdout: BufReader<ChildStdout>,
}

impl Sidecar {
    /// Spawn the engine and confirm it answers.
    pub fn spawn(program: &str, args: &[String]) -> Result<Self, RpcError> {
        let mut cmd = Command::new(program);
        cmd.args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            // stderr stays inherited so the engine's logs reach the console in
            // dev and the packaged log collector in production.
            .stderr(Stdio::inherit());

        let mut child = cmd.spawn().map_err(|e| RpcError::Spawn(e.to_string()))?;

        let stdin = child.stdin.take().ok_or(RpcError::Pipe)?;
        let stdout = child.stdout.take().ok_or(RpcError::Pipe)?;

        let this = Self {
            inner: Arc::new(Mutex::new(Transport {
                child,
                stdin: Some(BufWriter::new(stdin)),
                stdout: BufReader::new(stdout),
            })),
            next_id: Arc::new(AtomicU64::new(1)),
        };

        // Handshake: proves the engine is alive and speaking our protocol
        // before any UI depends on it.
        let pong = this.call(crate::protocol::methods::PING, json!({}))?;
        let peer_protocol = pong
            .get("protocol")
            .and_then(Value::as_u64)
            .unwrap_or(u64::MAX);
        if peer_protocol != crate::protocol::PROTOCOL_VERSION as u64 {
            return Err(RpcError::Application {
                code: crate::protocol::codes::PROTOCOL_MISMATCH,
                message: format!(
                    "engine speaks protocol v{peer_protocol}, this build needs v{}",
                    crate::protocol::PROTOCOL_VERSION
                ),
                data: None,
            });
        }

        Ok(this)
    }

    /// Adopt an already-spawned child process.
    ///
    /// Needed because the Tauri shell launches sidecars through its own shell
    /// plugin (so they resolve relative to the app bundle), and because tests
    /// need to control the environment the engine starts in.
    ///
    /// Unlike [`Sidecar::spawn`] this performs no handshake; call
    /// [`Sidecar::call`] with [`crate::protocol::methods::PING`] yourself if
    /// you want the protocol version verified.
    pub fn from_child(
        mut child: Child,
        stdin: ChildStdin,
        stdout: ChildStdout,
    ) -> Result<Self, RpcError> {
        // Guard against a child that died before we attached.
        match child.try_wait() {
            Ok(None) => {}
            Ok(Some(status)) => {
                return Err(RpcError::Spawn(format!(
                    "engine exited immediately with {status}"
                )))
            }
            Err(e) => return Err(RpcError::Spawn(e.to_string())),
        }

        Ok(Self {
            inner: Arc::new(Mutex::new(Transport {
                child,
                stdin: Some(BufWriter::new(stdin)),
                stdout: BufReader::new(stdout),
            })),
            next_id: Arc::new(AtomicU64::new(1)),
        })
    }

    /// Send a request and wait for its response.
    pub fn call(&self, method: &str, params: Value) -> Result<Value, RpcError> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let request = json!({
            "jsonrpc": "2.0",
            "id": id,
            "method": method,
            "params": params,
        });

        // Serialise the whole exchange so ids cannot interleave.
        let mut guard = self
            .inner
            .lock()
            .map_err(|_| RpcError::Send("engine connection is wedged".into()))?;
        let t = &mut *guard;

        let mut line = serde_json::to_vec(&request).map_err(|e| RpcError::Send(e.to_string()))?;
        line.push(crate::protocol::FRAME_TERMINATOR);

        {
            let stdin = t.stdin.as_mut().ok_or(RpcError::Eof)?;
            stdin
                .write_all(&line)
                .map_err(|e| RpcError::Send(e.to_string()))?;
            stdin
                .flush()
                .map_err(|e| RpcError::Send(e.to_string()))?;
        }

        let raw = loop {
            let mut buf = String::new();
            let n = t
                .stdout
                .read_line(&mut buf)
                .map_err(|e| RpcError::Send(e.to_string()))?;
            if n == 0 {
                return Err(RpcError::Eof);
            }
            let trimmed = buf.trim();
            // Tolerate blank lines: a stray newline must not desync the stream.
            if !trimmed.is_empty() {
                break trimmed.to_string();
            }
        };

        let envelope: Envelope =
            serde_json::from_str(&raw).map_err(|_| RpcError::MalformedFrame(raw.clone()))?;

        if envelope.version != "2.0" {
            return Err(RpcError::MalformedFrame(format!(
                "jsonrpc field was {version:?}, expected \"2.0\"",
                version = envelope.version
            )));
        }

        if envelope.id != Some(id) {
            return Err(RpcError::IdMismatch {
                expected: id,
                got: envelope.id,
            });
        }

        if let Some(err) = envelope.error {
            return Err(RpcError::Application {
                code: err.code,
                message: err.message,
                data: err.data,
            });
        }

        Ok(envelope.result.unwrap_or(Value::Null))
    }

    /// Send a notification. No response is expected or read.
    pub fn notify(&self, method: &str, params: Value) -> Result<(), RpcError> {
        let request = json!({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        });
        let mut guard = self
            .inner
            .lock()
            .map_err(|_| RpcError::Send("engine connection is wedged".into()))?;
        let t = &mut *guard;

        let mut line = serde_json::to_vec(&request).map_err(|e| RpcError::Send(e.to_string()))?;
        line.push(crate::protocol::FRAME_TERMINATOR);
        let stdin = t.stdin.as_mut().ok_or(RpcError::Eof)?;
        stdin
            .write_all(&line)
            .map_err(|e| RpcError::Send(e.to_string()))?;
        stdin
            .flush()
            .map_err(|e| RpcError::Send(e.to_string()))?;
        Ok(())
    }

    pub fn is_alive(&self) -> bool {
        match self.inner.lock() {
            Ok(mut t) => matches!(t.child.try_wait(), Ok(None)),
            Err(_) => false,
        }
    }

    /// Close stdin, which is the sidecar's shutdown signal, then reap.
    pub fn shutdown(&self, grace: Duration) -> Option<i32> {
        let mut guard = self.inner.lock().ok()?;
        let t = &mut *guard;
        if let Some(mut stdin) = t.stdin.take() {
            let _ = stdin.flush();
            // `stdin` drops here, which closes the pipe; the engine's read loop
            // then sees EOF and exits cleanly.
        }

        let deadline = std::time::Instant::now() + grace;
        while std::time::Instant::now() < deadline {
            if let Ok(Some(status)) = t.child.try_wait() {
                return status.code();
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        let _ = t.child.kill();
        t.child.wait().ok().and_then(|s| s.code())
    }
}

impl Drop for Sidecar {
    fn drop(&mut self) {
        // Only the last handle should tear the child down.
        if let Some(inner) = Arc::get_mut(&mut self.inner) {
            if let Ok(t) = inner.get_mut() {
                let _ = t.child.kill();
                let _ = t.child.wait();
            }
        }
    }
}

/// Convenience: build the params object for `catalog.load`.
pub fn catalog_load_params(payload: &[u8], sha256: &str, url: &str) -> Value {
    let mut params = Map::new();
    // Send the catalog as a string so it survives JSON transport unchanged.
    params.insert(
        "payload".into(),
        Value::String(String::from_utf8_lossy(payload).into_owned()),
    );
    params.insert("sha256".into(), json!(sha256));
    params.insert("url".into(), json!(url));
    Value::Object(params)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_frame_is_exactly_one_line_of_json() {
        let request = json!({"jsonrpc":"2.0","id":1,"method":"ping","params":{}});
        let mut bytes = serde_json::to_vec(&request).unwrap();
        bytes.push(crate::protocol::FRAME_TERMINATOR);

        let text = String::from_utf8(bytes).unwrap();
        assert_eq!(text.matches('\n').count(), 1, "frame must contain exactly one newline");
        assert!(text.ends_with('\n'));
        // No pretty printing: that would put newlines inside the frame.
        assert!(!text.contains("\n  "));
    }

    #[test]
    fn envelope_decodes_a_result() {
        let raw = r#"{"jsonrpc":"2.0","id":7,"result":{"pong":true}}"#;
        let env: Envelope = serde_json::from_str(raw).unwrap();
        assert_eq!(env.id, Some(7));
        assert!(env.error.is_none());
        assert_eq!(env.result.unwrap()["pong"], json!(true));
    }

    #[test]
    fn envelope_decodes_an_error() {
        let raw = r#"{"jsonrpc":"2.0","id":7,"error":{"code":-32003,"message":"blocked","data":{"x":1}}}"#;
        let env: Envelope = serde_json::from_str(raw).unwrap();
        let err = env.error.unwrap();
        assert_eq!(err.code, -32003);
        assert_eq!(err.message, "blocked");
        assert_eq!(err.data.unwrap()["x"], json!(1));
    }

    #[test]
    fn envelope_without_id_decodes_to_none() {
        let raw = r#"{"jsonrpc":"2.0","result":null}"#;
        let env: Envelope = serde_json::from_str(raw).unwrap();
        assert_eq!(env.id, None);
    }

    #[test]
    fn catalog_params_carry_provenance() {
        let params = catalog_load_params(b"{\"schema\":1}", "deadbeef", "https://example.test/c.json");
        assert_eq!(params["sha256"], json!("deadbeef"));
        assert_eq!(params["url"], json!("https://example.test/c.json"));
        assert_eq!(params["payload"], json! {"{\"schema\":1}"});
    }
}
