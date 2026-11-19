# Architecture

## The shape of it

```
┌────────────────────────────────────────────────────────────────┐
│  Tauri shell  (Rust, elevated)                                 │
│                                                                │
│   renderer (src/*.html|css|js)                                 │
│      │  window.__TAURI__.core.invoke                           │
│      ▼                                                         │
│   commands.rs ── the only network-capable code in the app      │
│      │                                                         │
│      │  newline-delimited JSON-RPC 2.0 over stdio              │
│      ▼                                                         │
└──────┼─────────────────────────────────────────────────────────┘
       │
┌──────▼─────────────────────────────────────────────────────────┐
│  Python engine  (PyInstaller onedir, elevated, NO network)     │
│                                                                │
│   rpc/server.py      framing, dispatch, error mapping          │
│   app.py             method table, app state                   │
│   core/catalog.py    validation + the handler allowlist        │
│   core/risk.py       fail-closed requirement gating            │
│   core/executor.py   resolve → gate → run → journal            │
│   core/backup.py     the undo journal                          │
│   actions/*          the handlers that actually change things  │
│      │                                                         │
│      ▼  registry · powercfg · netsh · adb                      │
│   Windows                                                    │
└────────────────────────────────────────────────────────────────┘
```

## Why two processes

The obvious question is why not do it all in Rust, or all in Python.

**Registry, WMI and ADB work is far less painful in Python.** Doing the same
in Rust means a `winreg` crate, hand-rolled `powercfg` output parsing, and a
lot of `#[cfg(windows)]`. In Python it is a few lines, and — more importantly —
it is testable on Linux with an in-memory registry, which is how this project's
135 engine tests run in CI on `ubuntu-latest`.

**The UI is far less painful in Tauri than in Python.** A Tauri window is a
real native window at ~10 MB. A Python GUI is either a webview you embed
yourself or Tk.

The cost is a process boundary. That cost is paid once, in `bridge/src/rpc.rs`
and `rpc/server.py`, and it is covered by an integration test that spawns the
real Python engine from Rust and drives it.

## The security model

Three decisions do most of the work.

### 1. The catalog is data, never code

The catalog is fetched from GitHub at runtime. It crosses a trust boundary, so
`core/catalog.py` treats it as hostile until proven otherwise:

- every `handler` string must already be compiled into *this build* — a remote
  catalog cannot register a new action, only configure one that ships
- `args` are JSON primitives only, and each is re-validated against the
  handler's own declared schema
- unknown keys anywhere are a **hard error**, not ignored — this is what stops
  a `command:` or `script:` field being added today in the hope a future
  version honours it
- one bad action rejects the entire file; there is no partial application

The consequence: a fully compromised repository cannot make an installed app do
anything new. It can retune existing actions, which is the intent.

### 2. The elevated process has no network

The Python engine writes to the registry and runs `powercfg`. It performs zero
network I/O — no HTTP client, no sockets. The Rust shell fetches the catalog and
hands over the bytes.

One process talks to the internet, a different one talks to the machine, and the
only channel between them is a validated message. If the engine were somehow
tricked into running something, it has no way to call home.

### 3. The shell's command surface is argv-only

`core/mutate.Shell.run` takes a `list[str]`, never a string, and never uses
`shell=True`. A handler physically cannot construct a command line. Interface
names and paths discovered at runtime are passed as discrete list elements, so a
hostile adapter name cannot inject anything.

## Gating is fail-closed

`core/risk.py` evaluates `requires` against detected hardware. An action is
available only when every requirement is *positively satisfied*. If detection
could not determine a fact, the action is `blocked`, and the reason is shown to
the user.

The asymmetry is deliberate. A mis-detected CPU must cost the user a tweak,
never their machine. This matters most for generation-gated actions — the
"13th/14th-gen Intel" class of tweak that actively harms the wrong silicon.

## Undo

`core/backup.py` is the safety net.

Before a value is written, the previous value is read and recorded. Reverting
replays the journal in reverse. Two details are load-bearing:

**Re-running an action appends to the existing journal** rather than replacing
it. So apply → apply → undo returns to the *original* state, not to whatever
the first application left behind. There is a test for exactly this
(`test_double_apply_then_single_undo_reaches_the_true_original`).

**Preview journals cannot persist.** `UndoJournal.commit()` returns `None` when
the journal is in preview mode, enforced inside the journal rather than relying
on every caller to remember. A preview that appeared in the Undo tab would be a
lie about what the app had done.

Powercfg and netsh changes journal the exact inverse command, so those revert
too.

## Where the logic lives

The rule that keeps this maintainable: **anything testable lives in `bridge` or
the Python engine, never in the Tauri binary.**

`src-tauri/` cannot be compiled on Linux (Tauri needs webkit), so anything
placed there is unverifiable in CI. `bridge/` has no Tauri dependency, so it
builds and tests everywhere — which is why the JSON-RPC client, the URL
allowlist and the hashing all live there and not in `commands.rs`.

`commands.rs` is correspondingly boring: resolve the sidecar path, forward
calls, tear down on window close.

## Failure modes worth knowing

| Situation | Behaviour |
|---|---|
| GitHub unreachable | Bundled catalog is used; the UI says so |
| Hostile or broken catalog | Rejected in full; bundled copy stays active; validation problems are logged |
| Engine dies mid-session | `engine_status` reports it; commands fail fast rather than hanging |
| Handler raises | Becomes a clean `ACTION_FAILED` with the message; the session survives |
| One corrupt line on the pipe | Reported as `PARSE_ERROR`; the stream stays synchronised (tested) |
| Not running as admin | HKLM actions are gated off with the reason shown, not attempted and failed |

## Versioning

Two independent version numbers, both checked at handshake:

- `PROTOCOL_VERSION` — the wire format. The shell refuses to talk to an engine
  that disagrees.
- `CATALOG_SCHEMA_VERSION` — the catalog shape. A catalog from a newer schema is
  rejected rather than partially understood.

Bump both sides together, in `sidecar/src/bloodlust/version.py` and
`bridge/src/protocol.rs`.
