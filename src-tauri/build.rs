//! Build-time guard.
//!
//! The installer declares `sidecar/bloodlust-sidecar.exe` as a bundled
//! resource. If that file is missing, Tauri may still produce an installer —
//! one that starts, spawns nothing, and shows an empty UI. That is the worst
//! possible place to discover the problem: on a user's machine.
//!
//! So we check here instead, at compile time, with a message that says exactly
//! what to run. This only runs on Windows, because that is the only platform
//! that builds the release bundle.

fn main() {
    #[cfg(windows)]
    check_sidecar();

    tauri_build::build()
}

#[cfg(windows)]
fn check_sidecar() {
    let manifest = std::path::PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let sidecar = manifest.join("sidecar").join("bloodlust-sidecar.exe");

    // Skip the check for a plain `cargo check`/`cargo test`, where no bundle is
    // produced and the engine legitimately is not built yet.
    let profile = std::env::var("PROFILE").unwrap_or_default();
    if sidecar.exists() || profile != "release" {
        return;
    }

    panic!(
        "\n\
        \n  ─────────────────────────────────────────────────────────────\n  \
         RELEASE BUILD BLOCKED: the tuning engine is missing\n\n  \
         Expected: {}\n\n  \
         The installer bundles this file as a resource. Building without it\n  \
         produces an app that starts, spawns nothing, and shows an empty UI.\n\n  \
         Fix — from the repository root:\n\n    \
         python scripts/build_sidecar.py\n\n  \
         That builds the engine with PyInstaller, runs its self-test, and\n  \
         stages it into src-tauri/sidecar/. Then re-run the build.\n  \
         ─────────────────────────────────────────────────────────────\n",
        sidecar.display()
    );
}
