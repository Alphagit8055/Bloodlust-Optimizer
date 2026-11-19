"""JSON-RPC 2.0 server over stdio.

Wire format: newline-delimited JSON (one object per line, UTF-8). Chosen over
content-length framing because it is trivially debuggable — you can pipe
hand-written lines into the binary and watch it answer.

Framing rules the Rust side must match exactly (see ``bridge/src/rpc.rs``):
  * one JSON object per ``\\n``-terminated line
  * no trailing commas, no pretty printing
  * the sidecar writes ONLY response objects to stdout
  * a request without ``id`` is a notification and gets no reply
  * unknown methods get -32601, never a crash

The loop never propagates an exception: anything unexpected becomes an
INTERNAL_ERROR response so one bad request cannot kill a gaming session.
"""

from __future__ import annotations

import io
import json
import sys
from typing import Any, Callable, Optional

from ..core.logging import get, setup
from ..version import APP_NAME, APP_VERSION, PROTOCOL_VERSION
from . import errors

Handler = Callable[[dict], Any]


class Dispatcher:
    """Method registry with positional-free, keyword-only params."""

    def __init__(self) -> None:
        self._methods: dict[str, Handler] = {}

    def method(self, name: str, fn: Handler) -> None:
        self._methods[name] = fn

    def has(self, name: str) -> bool:
        return name in self._methods

    def names(self) -> list[str]:
        return sorted(self._methods)

    def call(self, name: str, params: Optional[dict]) -> Any:
        try:
            fn = self._methods[name]
        except KeyError:
            raise errors.RpcError(errors.METHOD_NOT_FOUND, f"unknown method {name!r}")

        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise errors.RpcError(
                errors.INVALID_PARAMS, "params must be an object, got " + type(params).__name__
            )

        try:
            return fn(params)
        except errors.RpcError:
            raise
        except (TypeError, KeyError) as exc:
            # Both mean the caller's params did not match what the handler
            # expects: TypeError from a signature mismatch, KeyError from a
            # handler reading a key the caller omitted. From the shell's point
            # of view these are bad params, not an internal fault, so report
            # them that way and let the UI say "missing argument".
            label = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
            raise errors.RpcError(
                errors.INVALID_PARAMS, f"bad params for {name}: {label}"
            )
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            get().exception("handler %s raised", name)
            raise errors.RpcError(errors.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")


def _reconfigure_stdio() -> None:
    """Force UTF-8 + LF on stdio.

    On Windows the console codepage is often cp1252 and a non-ASCII tweak label
    in a response would raise UnicodeEncodeError mid-message, desynchronising
    the stream. Reconfiguring removes that failure mode.
    """
    for stream, buffering in ((sys.stdout, 1), (sys.stderr, 1)):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")
            stream.line_buffering = True  # type: ignore[attr-defined]
        except (AttributeError, ValueError, io.UnsupportedOperation):
            # Detached or already-wrapped stream (e.g. under a test harness).
            pass
        del buffering


class Server:
    def __init__(self, dispatcher: Dispatcher) -> None:
        self.d = dispatcher
        self._running = False

    # ------------------------------------------------------------- transport

    def _write(self, obj: dict) -> None:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def _reply(self, req_id: Any, result: Any) -> None:
        self._write({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _error(self, req_id: Any, err: errors.RpcError) -> None:
        self._write({"jsonrpc": "2.0", "id": req_id, "error": err.to_dict()})

    # ------------------------------------------------------------------ loop

    def serve(self, stdin: Any = None, stdout_probe: bool = False) -> int:
        """Read requests until EOF. Returns a process exit code."""
        _reconfigure_stdio()
        setup()
        self._running = True
        log = get()
        log.info("%s sidecar %s (protocol v%s) ready", APP_NAME, APP_VERSION, PROTOCOL_VERSION)

        source = stdin or sys.stdin
        # Prefer the raw byte stream so a BOM or a stray CR from a Windows pipe
        # cannot survive into json.loads.
        reader: Any = getattr(source, "buffer", source)

        while self._running:
            try:
                chunk = reader.readline()
            except (ValueError, OSError) as exc:
                log.error("stdin read failed: %s", exc)
                break

            if not chunk:  # b"" or "" -> EOF, the shell hung up
                break

            if isinstance(chunk, bytes):
                line = chunk.decode("utf-8", errors="replace")
            else:
                line = chunk

            line = line.lstrip("\ufeff").strip()
            if not line:
                continue

            self._handle_line(line)

        log.info("stdin closed, shutting down")
        return 0

    def _handle_line(self, line: str) -> None:
        log = get()
        try:
            msg = json.loads(line)
        except ValueError as exc:
            self._error(
                None, errors.RpcError(errors.PARSE_ERROR, f"invalid JSON: {exc}")
            )
            return

        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            self._error(
                msg.get("id") if isinstance(msg, dict) else None,
                errors.RpcError(errors.INVALID_REQUEST, 'missing "jsonrpc":"2.0"'),
            )
            return

        req_id = msg.get("id")
        is_notification = "id" not in msg

        try:
            result = self.d.call(msg.get("method", ""), msg.get("params"))
        except errors.RpcError as exc:
            if not is_notification:
                self._error(req_id, exc)
            else:
                log.warning("notification failed: %s", exc.message)
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("unhandled error in dispatcher")
            if not is_notification:
                self._error(
                    req_id, errors.RpcError(errors.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
                )
            return

        if not is_notification:
            self._reply(req_id, result)

    def stop(self) -> None:
        self._running = False


__all__ = ["Server", "Dispatcher"]
