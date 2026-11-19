# Bloodlust Optimizer

Reversible Windows, GPU, network and GameLoop tuning for people playing
Android games on an emulator.

**Developer:** Telegram: [@Alpha0NemesiSS](https://t.me/Alpha0NemesiSS)

---

## The idea

Two rules shaped the whole design.

**Every change can be undone.** Before a value is written, the previous value
is recorded in a journal. Reverting replays the journal, so applying an action
twice and reverting once still returns you to the original state. Nothing is
left behind that the Undo tab cannot show you.

**The tweak list is data, not code.** Actions are described in a catalog served
from this repository. Editing `catalog/catalog.json` retunes the app for every
user on their next launch — no rebuild, no reinstall. But a catalog can only
*configure* actions already compiled into the binary. It cannot add one. That
is the control that makes a public repository safe to drive an app that writes
to the registry.

---

## What it does

33 actions across 7 families, each gated to the hardware it actually applies to:

| Family | Examples |
|---|---|
| **System Report** | Hardware inventory, pending-reboot check, "why is this locked?" |
| **Power & CPU** | Ultimate Performance plan, no core parking, full clock, wireless adapter power saving |
| **Network** | TCP autotuning, delayed-ACK, Cloudflare DNS, DNS flush |
| **GPU & Scheduling** | HAGS, TDR delay, route the emulator to the dedicated GPU |
| **Windows Shell** | Game Mode, Game DVR, fullscreen optimisations, MMCSS Games class, pointer acceleration |
| **GameLoop** | Install report, cache clear, autostart, Defender exclusion |
| **Emulator Guest (ADB)** | Disable guest animations, hide touch indicators |

Everything is gated **fail-closed**: if the app cannot *prove* your hardware
matches what an action needs, the action is locked and it tells you why. A
mis-detected CPU costs you a tweak, never your machine — which matters most for
the generation-gated tweaks that actively harm the wrong silicon.

---

## Download

Grab `Bloodlust-Optimizer_*_x64-setup.exe` from the
[latest release](https://github.com/Alphagit8055/Bloodlust-Optimizer/releases/latest).

Run it **as administrator**. Without elevation the system-level actions stay
locked — the app will tell you rather than failing silently.

---

## An honest note on what this cannot do

An emulator runs ARM code translated to x86, inside a guest, behind at least a
frame of queueing. Those costs are structural. No setting removes them.

These tweaks remove *overhead* — Windows downclocking the core your game runs
on, background recording stealing frames, Defender scanning every emulator disk
read, the radio entering a power-saving state mid-match. That is real and worth
having. It is not headroom that does not exist.

If your frame rate is limited by your hardware, this will not change that. If
it is limited by your settings, it will.

---

## How it works

```
Tauri shell (Rust)          ← the only network-capable code
     │  JSON-RPC over stdio
     ▼
Python engine (PyInstaller) ← no network access at all
     │
     ▼  registry · powercfg · netsh · adb
   Windows
```

The shell fetches the catalog and hands it to the engine. The engine has no
HTTP client and no sockets. One process talks to the internet, a different one
talks to your machine, and the only channel between them is a validated
message.

Full detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## For developers

### Layout

```
bridge/       Rust: JSON-RPC client, catalog verification, protocol constants
              → no Tauri dependency, so it tests on Linux CI
sidecar/      Python engine: catalog, gating, executor, undo, handlers
              → 135 tests, all runnable without Windows
src-tauri/    Tauri shell: thin, deliberately
src/          Renderer: plain DOM, no bundler, no CDN
catalog/      catalog.json — the file served from this repo
scripts/      build_sidecar.py — builds and self-tests the engine
.github/      CI: test matrix, then Windows installer on tag
```

### Run it locally

```bash
# The engine, standalone
cd sidecar
pip install -r requirements.txt
python -m bloodlust --selftest

# Tests (no Windows needed)
pytest tests -q

# The bridge crate
cd ../bridge && cargo test

# The whole app
npm install -g @tauri-apps/cli
BLOODLUST_SIDECAR=./sidecar/src/run_dev.py tauri dev
```

### Adding an action

[docs/ADDING-ACTIONS.md](docs/ADDING-ACTIONS.md) — two files, about five
minutes.

### Publishing

[docs/GITHUB-SETUP.md](docs/GITHUB-SETUP.md) — repository, updater signing key,
secrets, and the tag that produces a release.

---

## Security

- **HTTPS only**, with a compile-time host allowlist. The allowlist cannot be
  edited at runtime, because a value that could be edited could be edited to
  point the app at an attacker's server.
- **SHA-256 provenance** for every catalog, shown in the UI.
- **Handler allowlist.** A catalog naming a handler the binary does not ship is
  rejected in full.
- **No shell strings, anywhere.** Every command is an argv list.
- **No telemetry, no analytics, no accounts.** The engine makes no outbound
  connections.
- **Fail-closed gating.** Unverifiable means blocked.

---

## Not in this version

Licensing and entitlements are deliberately absent — the hooks are there
(`client_ui` equivalent, state persistence, risk ceiling) but nothing is gated
behind a key.

Runtime session boost (holding a performance state for the length of a match)
is also not implemented.

---

## License

MIT. See [LICENSE](LICENSE).

Tweaks that lower a security boundary are labelled `danger` in the catalog and
locked behind the risk ceiling by default. Read the description before applying
one; they are included because some users want them, not because they are
recommended.
