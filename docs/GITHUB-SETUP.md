# GitHub setup

Getting from this folder to a public release people can download. Roughly 30
minutes, and almost all of it is one-time.

The names below assume:

- GitHub account: `Alphagit8055`
- Repository: `Bloodlust-Optimizer`

If you use anything different, the four constants in
`src-tauri/src/commands.rs` and the URL in `src-tauri/tauri.conf.json` are the
only places that need to change.

---

## 1. Create the repository

On GitHub, **New repository**:

| Field | Value |
|---|---|
| Name | `Bloodlust-Optimizer` |
| Visibility | **Public** — required, see the note below |
| Initialise with README | **No** (this folder already has one) |

> **Why public matters.** The app fetches its catalog from
> `raw.githubusercontent.com`, which serves private repositories only with a
> token. A token would have to ship inside the installer, and anything inside
> an installer is extractable — so a private repo would mean embedding a
> credential in a binary you hand to strangers. Keep the repo public; it
> contains no secrets by design.

Then from this folder:

```bash
git init -b main
git add .
git commit -m "Bloodlust Optimizer initial import"
git remote add origin https://github.com/Alphagit8055/Bloodlust-Optimizer.git
git push -u origin main
```

---

## 2. Point the app at your repository

Four constants, one file — `src-tauri/src/commands.rs`:

```rust
pub const REPO_OWNER: &str = "Alphagit8055";
pub const REPO_NAME: &str  = "Bloodlust-Optimizer";
pub const CATALOG_REF: &str = "main";
pub const CATALOG_PATH: &str = "catalog/catalog.json";
```

And one URL in `src-tauri/tauri.conf.json`, under `plugins.updater.endpoints`:

```
https://github.com/Alphagit8055/Bloodlust-Optimizer/releases/latest/download/latest.json
```

That is all. The host allowlist in `bridge/src/lib.rs` already permits
`githubusercontent.com` and `github.com`, and it is baked in at compile time
deliberately — a value that could be edited at runtime could be edited to point
the app at an attacker's server.

---

## 3. Generate the updater signing key

The auto-updater verifies a signature before installing anything. Without this,
a build will not publish.

```bash
npx @tauri-apps/cli@^2 signer generate -w ~/.tauri/bloodlust.key
```

You get two things:

**A public key**, printed at the end. Paste it into `src-tauri/tauri.conf.json`,
replacing the placeholder:

```json
"plugins": {
  "updater": {
    "pubkey": "dW50cnVzdGVkIGNvbW1lbnQ6...THE_LONG_STRING"
  }
}
```

**A private key file** and the password you chose. These go into GitHub
secrets, never into the repository.

---

## 4. Add the repository secrets

GitHub → your repo → **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**. Add these three:

| Name | Value |
|---|---|
| `TAURI_SIGNING_PRIVATE_KEY` | The **contents** of `~/.tauri/bloodlust.key`, pasted in full |
| `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | The password you chose in step 3 |
| `GITHUB_TOKEN` | *Do not create this one* — GitHub provides it automatically |

Keep a copy of the private key somewhere safe. If you lose it, you cannot sign
updates for the versions already installed, and those users must reinstall
manually.

---

## 5. Turn on Actions

**Settings → Actions → General**:

- **Actions permissions** → *Allow all actions and reusable workflows*
- **Workflow permissions** → *Read and write permissions*

The second one is what lets the workflow create the release. Without it the
build succeeds and the upload fails with a 403.

---

## 6. Verify before your first release

Push to `main` (not a tag). This runs the test matrix only and publishes
nothing:

```bash
git push origin main
```

On the **Actions** tab you should see `Test (ubuntu-latest)` and
`Test (windows-latest)` both pass. Fix anything red before continuing — the
release job will not start until they are green.

---

## 7. Publish a release

```bash
git tag v0.1.0
git push origin v0.1.0
```

The workflow runs for about 15–25 minutes. It:

1. runs the tests on Linux and Windows
2. builds the Python engine and **runs its self-test** — a bundle that cannot
   find its embedded catalog fails here rather than shipping a blank UI
3. builds the Tauri installer
4. uploads a **draft** release with
   `Bloodlust-Optimizer_0.1.0_x64-setup.exe`, the `.sig` file, and `latest.json`

Open the draft, read it, and press **Publish**. Users download the `.exe` from
the release page.

---

## 8. How updates reach users

`latest.json` in the release tells the app a newer version exists; the app
downloads the new installer, verifies the signature against the public key in
`tauri.conf.json`, and installs it.

Because the release is marked *latest*, that URL always resolves to your newest
publish. Nothing to update in the app.

---

## 9. Changing tweaks without a release

This is the part worth understanding.

**Editing `catalog/catalog.json` on `main` takes effect immediately** for every
user, on their next launch. No rebuild, no reinstall. That is what makes the
GitHub-hosted catalog worth having: you can retune a value, relabel an action,
or gate one to a specific CPU generation, and it is live in minutes.

What that **cannot** do is add a new action. Every catalog entry names a
*handler*, and the handler must already be compiled into the binary the user is
running. Validation rejects a catalog that names anything else — this is the
control that lets a public repository drive an app that writes to the registry.

So:

| Change | Needs a new release? |
|---|---|
| Retune an argument, relabel, reorder, gate by hardware | No — edit the JSON |
| Disable an action temporarily | No — remove it from the JSON |
| Add a brand-new tweak with new logic | **Yes** — write a handler, then a release |

After editing the JSON, confirm CI is green. The `Verify the catalog against the
shipped allowlist` step runs the exact validation the app runs, so a broken
catalog fails the build rather than reaching users.

---

## 10. Troubleshooting

**The build fails with "plugins.updater.pubkey is still the placeholder."**
Step 3 is not done. The workflow refuses to publish an unsigned installer.

**The release is created but has no installer attached.**
Workflow permissions are read-only. See step 5.

**Users report an empty action list.**
The catalog failed validation and the bundled copy also failed to load. Ask for
the engine log — the app's **Engine log** link shows the exact validation
problems. The bundled catalog is embedded at build time, so this normally means
a stale `src-tauri/sidecar/` directory; run `python scripts/build_sidecar.py`
locally to check.

**"The tuning engine failed to start."**
The sidecar is missing from the install. It should be beside the executable.
Check that `scripts/build_sidecar.py` ran in the workflow log before
`tauri build`.

**You renamed the repository.**
Update the four constants and the updater URL, then publish a new version.
Existing installs keep working — they just stop receiving catalog updates until
they update.
