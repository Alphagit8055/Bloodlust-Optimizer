# Building the .exe and publishing a release

Two routes. **Route A (GitHub Actions) is the recommended one** — you get a
reproducible build on a clean machine, and the release page, installer and
updater manifest are produced for you. Route B is for testing on your own PC.

---

## One-time setup (do this first, either route)

### 1. Copy the new files into your Windows working copy

Since your last sync I added:

| File | Why |
|---|---|
| `.gitattributes` | Kills 68 CRLF warnings; normalises line endings |
| `scripts/make_icons.py` | Regenerates the icons reproducibly |
| `src-tauri/icons/*` | 5 icon files — **the build fails without these** |

If your copy is missing `src-tauri/icons/`, run `python scripts/make_icons.py`
(needs `pip install pillow`).

### 2. Generate the update-signing key

This is what lets an installed copy verify that an update really came from you.

```cmd
npx @tauri-apps/cli@^2 signer generate -w %USERPROFILE%\.tauri\bloodlust.key
```

> This project deliberately has **no `package.json`** — the frontend is plain
> HTML/CSS/JS with no bundler, so there is nothing for npm to manage. Always
> invoke the CLI as `npx @tauri-apps/cli@^2 <command>`. Running bare `npx tauri`
> or `npm install` will fail, because those expect a `package.json`.

- It prints a **public key** and writes the **private key** to
  `%USERPROFILE%\.tauri\bloodlust.key`.
- Pick a password when prompted. **Save it somewhere safe** — without it you
  cannot sign future updates, and users cannot receive them.
- Paste the public key into `src-tauri/tauri.conf.json`:

```json
"plugins": {
  "updater": {
    "pubkey": "PASTE_THE_LONG_PUBLIC_KEY_HERE",
    "endpoints": [ "..." ]
  }
}
```

The key is a single long line beginning `dW50cnVzdGVk...`. Keep it on one line.

> The build deliberately **fails** while `pubkey` still says
> `REPLACE_WITH_OUTPUT_OF_tauri_signer_generate`. That guard exists so you can
> never publish an unsigned release by accident.

### 3. Commit and push

```cmd
git add .
git commit -m "Add icons, gitattributes, release setup"
git push
```

---

## Route A — GitHub Actions (recommended)

### A1. Add the signing secrets

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:

| Name | Value |
|---|---|
| `TAURI_SIGNING_PRIVATE_KEY` | the **entire contents** of `bloodlust.key` (a long base64 blob) |
| `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | the password you chose |

To print the key file contents on Windows:

```cmd
type %USERPROFILE%\.tauri\bloodlust.key
```

Copy everything it prints, including the trailing newline. Do **not** add this
file to the repo — `.gitignore` already blocks `*.key`, but secrets are the
right place regardless.

### A2. Allow Actions to create releases

**Settings** → **Actions** → **General** → **Workflow permissions** →
select **Read and write permissions** → **Save**.

Without this the build succeeds and then fails at the release step with a 403.

### A3. Tag a version

```cmd
git tag v0.1.0
git push origin v0.1.0
```

The tag is what triggers the build. Watch **Actions** → *Build* → the
`Build Windows installer` job. It takes roughly 15–25 minutes: the first run has
to compile every Rust dependency.

### A4. Publish the draft

When it finishes, go to **Releases** — there is a **draft** named
*Bloodlust Optimizer 0.1.0*. It contains:

- `Bloodlust-Optimizer_0.1.0_x64-setup.exe` — the installer users download
- `..._x64-setup.exe.sig` — the updater signature
- `latest.json` — the manifest the built-in updater reads

Check the .exe filename looks right, then press **Publish release**. Drafts are
invisible to users and the updater returns 404 until you publish.

### A5. Later releases

```cmd
git tag v0.1.1
git push origin v0.1.1
```

Bump `version` in **all three** places first, or the updater will loop trying to
install a version it already has:

- `src-tauri/Cargo.toml` → `version`
- `src-tauri/tauri.conf.json` → `version`
- `sidecar/src/bloodlust/version.py` → `APP_VERSION`

---

## Route B — build on your own PC

Useful for testing before you spend 20 minutes on CI. **Requires Windows
x64**; Tauri cannot cross-compile from Linux for this setup.

### B1. Install the toolchain

```cmd
winget install Rustlang.Rustup
winget install Python.Python.3.12
winget install Microsoft.VisualStudio.2022.BuildTools
```

For the Build Tools, select **Desktop development with C++** — the MSVC linker
is mandatory, `cargo` will not link without it.

Then, in a **new** terminal:

```cmd
rustup default stable-msvc
pip install pyinstaller pillow
```

### B2. Build the engine

```cmd
cd /d "D:\Learning and Dev\Optimizer Emulator Tool - Bloodlust optimizer\Bloodlust Optimizer\bloodlust-optimizer"
python scripts\build_sidecar.py
```

This does three things, and each one matters:

1. runs PyInstaller to freeze the Python engine
2. **runs the frozen binary's `--selftest`** — catches a broken bundle now
   rather than after install
3. copies it to `src-tauri\sidecar\bloodlust-sidecar.exe`

Expected finish: `SELFTEST OK`, 33 actions, 33 handlers, ~50 MB.

If you skip this step, the release build **stops at compile time** with a
message telling you to run it. That guard exists because a missing engine
otherwise produces an installer that installs fine and does nothing.

### B3. Build the installer

```cmd
npx @tauri-apps/cli@^2 build
```

Output lands in `src-tauri\target\release\bundle\nsis\`.
The un-optimised app binary is `src-tauri\target\release\bloodlust-optimizer.exe`,
but **ship the installer**, not that binary — it has no engine next to it.

### B4. Run it

Install with the NSIS `.exe`, then launch from the Start menu. On first launch
Windows will show a UAC prompt — the app needs administrator rights because
registry and `powercfg` changes require elevation.

---

## Testing without building

```cmd
cd sidecar
set BLOODLUST_SIDECAR=%CD%\src\run_dev.py
cd ..
npx @tauri-apps/cli@^2 dev
```

`BLOODLUST_SIDECAR` makes the app run the engine from source, so you can edit
Python and restart without rebuilding the bundle.

Verify the engine alone at any time:

```cmd
python sidecar\src\run_dev.py --selftest
cd sidecar && python -m pytest tests -q
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `icon not found` during `tauri build` | `src-tauri/icons/` missing | `python scripts\make_icons.py` |
| `build.rs` panics: engine missing | sidecar not staged | `python scripts\build_sidecar.py` |
| Release job fails 403 | Actions lacks write permission | Step **A2** |
| `pubkey is still the placeholder` | Step 1 not done | Paste the real key |
| `link.exe not found` | MSVC C++ workload absent | Step **B1** |
| App opens, then "engine is not running" | sidecar not next to the exe | Re-run B2, check `src-tauri\sidecar\` |
| Updater says "no update available" but a release exists | release still a **draft** | Publish it |
| `cargo build` takes forever | Windows Defender scanning `target/` | Add an exclusion for the project folder |
| `src refspec main does not match any` | no commits yet | Commit before pushing |

---

## What users experience

They download the `-setup.exe` from your Releases page, install, and run it as
administrator. Every change the app makes is journalled and revertible from the
**Undo** tab. Updates arrive through the built-in updater once you publish a new
tagged release.

The catalog — which actions appear, their labels, defaults and hardware gates —
is fetched from `raw.githubusercontent.com/Alphagit8055/Bloodlust-Optimizer/main/catalog/catalog.json`
on every launch. So you can retune or disable existing actions by editing that
file, **without cutting a release**. Adding an action that needs new logic *does*
need a release, because the handler has to be compiled into the engine.
