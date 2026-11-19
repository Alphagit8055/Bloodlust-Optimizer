"""JSON-RPC transport tests.

The framing here must match ``bridge/src/rpc.rs`` exactly, so the assertions are
written against the wire format rather than against Python objects.
"""

from __future__ import annotations

import io
import json

import pytest

from bloodlust.rpc import errors
from bloodlust.rpc.server import Dispatcher, Server


def _roundtrip(dispatcher, *lines) -> list[dict]:
    """Feed lines to the server, capture its stdout."""
    server = Server(dispatcher)
    stdin = io.StringIO("\n".join(lines) + "\n")
    buf = io.StringIO()

    import contextlib

    import sys

    real_stdout = sys.stdout
    sys.stdout = buf
    try:
        server.serve(stdin=stdin)
    finally:
        sys.stdout = real_stdout

    out = buf.getvalue()
    return [json.loads(ln) for ln in out.splitlines() if ln.strip()]


@pytest.fixture
def echo_dispatcher():
    d = Dispatcher()
    d.method("echo", lambda p: {"echo": p.get("value")})
    d.method("boom", lambda p: (_ for _ in ()).throw(RuntimeError("handler exploded")))
    d.method("typed", lambda p: p["required_arg"] * 2)
    d.method("notify", lambda p: None)
    return d


# ------------------------------------------------------------------- protocol


def test_valid_request_gets_result(echo_dispatcher):
    (resp,) = _roundtrip(echo_dispatcher, '{"jsonrpc":"2.0","id":1,"method":"echo","params":{"value":7}}')
    assert resp == {"jsonrpc": "2.0", "id": 1, "result": {"echo": 7}}


def test_omitted_params_is_allowed(echo_dispatcher):
    (resp,) = _roundtrip(echo_dispatcher, '{"jsonrpc":"2.0","id":1,"method":"echo"}')
    assert resp["result"] == {"echo": None}


def test_string_id_is_preserved(echo_dispatcher):
    (resp,) = _roundtrip(echo_dispatcher, '{"jsonrpc":"2.0","id":"abc","method":"echo","params":{}}')
    assert resp["id"] == "abc"


def test_null_id_is_preserved(echo_dispatcher):
    (resp,) = _roundtrip(echo_dispatcher, '{"jsonrpc":"2.0","id":null,"method":"echo","params":{}}')
    assert resp["id"] is None


def test_unknown_method_is_an_error_not_a_crash(echo_dispatcher):
    (resp,) = _roundtrip(echo_dispatcher, '{"jsonrpc":"2.0","id":1,"method":"nope","params":{}}')
    assert resp["error"]["code"] == errors.METHOD_NOT_FOUND


def test_handler_exception_becomes_internal_error(echo_dispatcher):
    (resp,) = _roundtrip(echo_dispatcher, '{"jsonrpc":"2.0","id":1,"method":"boom","params":{}}')
    assert resp["error"]["code"] == errors.INTERNAL_ERROR
    assert "handler exploded" in resp["error"]["message"]


def test_missing_argument_is_invalid_params(echo_dispatcher):
    (resp,) = _roundtrip(echo_dispatcher, '{"jsonrpc":"2.0","id":1,"method":"typed","params":{}}')
    assert resp["error"]["code"] == errors.INVALID_PARAMS


def test_non_object_params_rejected(echo_dispatcher):
    (resp,) = _roundtrip(echo_dispatcher, '{"jsonrpc":"2.0","id":1,"method":"echo","params":[1,2]}')
    assert resp["error"]["code"] == errors.INVALID_PARAMS


def test_malformed_json_returns_parse_error(echo_dispatcher):
    (resp,) = _roundtrip(echo_dispatcher, "{this is not json")
    assert resp["error"]["code"] == errors.PARSE_ERROR
    assert resp["id"] is None


def test_missing_jsonrpc_version_rejected(echo_dispatcher):
    (resp,) = _roundtrip(echo_dispatcher, '{"id":1,"method":"echo","params":{}}')
    assert resp["error"]["code"] == errors.INVALID_REQUEST


def test_notification_gets_no_reply(echo_dispatcher):
    replies = _roundtrip(echo_dispatcher, '{"jsonrpc":"2.0","method":"notify","params":{}}')
    assert replies == []


def test_stream_survives_a_bad_line(echo_dispatcher):
    """One corrupt line must not desynchronise the rest of the session."""
    replies = _roundtrip(
        echo_dispatcher,
        "{garbage",
        '{"jsonrpc":"2.0","id":2,"method":"echo","params":{"value":"after"}}',
    )
    assert len(replies) == 2
    assert replies[0]["error"]["code"] == errors.PARSE_ERROR
    assert replies[1]["result"] == {"echo": "after"}


def test_ids_stay_aligned_across_a_batch(echo_dispatcher):
    replies = _roundtrip(
        echo_dispatcher,
        '{"jsonrpc":"2.0","id":1,"method":"echo","params":{"value":"a"}}',
        '{"jsonrpc":"2.0","id":2,"method":"unknown","params":{}}',
        '{"jsonrpc":"2.0","id":3,"method":"echo","params":{"value":"c"}}',
    )
    assert [r["id"] for r in replies] == [1, 2, 3]


def test_utf8_survives_the_roundtrip(echo_dispatcher):
    (resp,) = _roundtrip(
        echo_dispatcher,
        '{"jsonrpc":"2.0","id":1,"method":"echo","params":{"value":"भारत — 中文"}}',
    )
    assert resp["result"]["echo"] == "भारत — 中文"


def test_blank_lines_are_ignored(echo_dispatcher):
    replies = _roundtrip(
        echo_dispatcher,
        "",
        '{"jsonrpc":"2.0","id":1,"method":"echo","params":{"value":1}}',
        "   ",
    )
    assert len(replies) == 1


def test_bom_is_stripped(echo_dispatcher):
    server = Server(echo_dispatcher)
    stdin = io.StringIO('\ufeff{"jsonrpc":"2.0","id":1,"method":"echo","params":{"value":"bom"}}\n')
    buf = io.StringIO()
    import sys

    real = sys.stdout
    sys.stdout = buf
    try:
        server.serve(stdin=stdin)
    finally:
        sys.stdout = real
    (resp,) = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]
    assert resp["result"] == {"echo": "bom"}


# ------------------------------------------------------- THE critical invariant


def test_stdout_stays_clean_during_a_real_session(isolated_data_dir):
    """Nothing but JSON-RPC may reach stdout.

    A stray log line or traceback on stdout desynchronises every subsequent
    message the Rust shell reads, and the failure shows up as a mysterious
    hang rather than an error. So: build the real app, drive it over the real
    transport, and assert every emitted line parses as JSON.
    """
    from bloodlust.app import App

    app = App()
    dispatcher = app.register(Dispatcher())

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "system.info", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "action.list", "params": {}},
        {"jsonrpc": "2.0", "id": 4, "method": "catalog.state", "params": {}},
        {"jsonrpc": "2.0", "id": 5, "method": "debug.handlers", "params": {}},
        {"jsonrpc": "2.0", "id": 6, "method": "does.not.exist", "params": {}},
    ]
    stdin = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
    buf = io.StringIO()

    import sys

    real = sys.stdout
    sys.stdout = buf
    try:
        Server(dispatcher).serve(stdin=stdin)
    finally:
        sys.stdout = real

    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert len(lines) == len(requests), f"expected {len(requests)} replies, got {len(lines)}"
    for line in lines:
        parsed = json.loads(line)  # raises if anything non-JSON leaked
        assert parsed["jsonrpc"] == "2.0"

    ids = [json.loads(ln)["id"] for ln in lines]
    assert ids == [r["id"] for r in requests]


def test_app_logging_goes_to_stderr_not_stdout(isolated_data_dir, capsys):
    """The logger must never be wired to stdout."""
    from bloodlust.core.logging import get, setup

    log = setup()
    log.info("this must not appear on stdout")
    log.error("nor must this")

    captured = capsys.readouterr()
    assert captured.out == "", "the logger wrote to stdout, which is the RPC channel"
    assert "must not appear" in captured.err


def test_dispatcher_lists_registered_methods(echo_dispatcher):
    assert echo_dispatcher.names() == ["boom", "echo", "notify", "typed"]
