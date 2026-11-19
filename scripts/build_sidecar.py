#!/usr/bin/env python3
"""Build the engine and verify it before anything is shipped.

Run this before `tauri build`. It is idempotent: if an engine is already staged
in src-tauri/sidecar/ it skips the rebuild unless you pass --force. If you
forget it, src-tauri/build.rs blocks the release build with a message telling
you to run this.

It does three things in order, and stops at the first failure:

1. build with PyInstaller
2. run the sidecar's own `--selftest`, which proves the frozen binary can find
   its catalog and enumerate its handlers
3. copy the result to where tauri.conf.json expects it

Step 2 is the important one. A bundle that cannot locate its embedded catalog
starts fine and then shows an empty UI, which is a much worse failure to
discover in production than a failed build.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "sidecar"
DIST = SIDECAR / "dist"
SPEC = SIDECAR / "bloodlust.spec"

IS_WINDOWS = platform.system() == "Windows"
BIN_NAME = "bloodlust-sidecar.exe" if IS_WINDOWS else "bloodlust-sidecar"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def check_python() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit(f"Python 3.10+ required, found {sys.version.split()[0]}")


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed; installing into the current environment")
        run([sys.executable, "-m", "pip", "install", "--upgrade", "pyinstaller"])


def already_staged() -> bool:
    """True when a previously staged engine is present and looks complete.

    Lets `tauri build` and CI run this repeatedly without paying for a full
    PyInstaller pass each time.
    """
    staged = ROOT / "src-tauri" / "sidecar" / BIN_NAME
    return staged.is_file()


def build(force: bool = False) -> Path:
    if not force and already_staged():
        staged = ROOT / "src-tauri" / "sidecar" / BIN_NAME
        print(f"engine already staged at {staged} — skipping the rebuild")
        print("(pass --force to rebuild anyway)")
        return staged

    if DIST.exists():
        shutil.rmtree(DIST)
    build_dir = SIDECAR / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir)

    run(
        [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm",
            "--distpath", DIST,
            "--workpath", build_dir,
            SPEC,
        ],
        cwd=SIDECAR,
    )

    target = DIST / "bloodlust-sidecar" / BIN_NAME
    if not target.is_file():
        raise SystemExit(f"expected the engine at {target}, but it is not there")
    return target


def selftest(binary: Path) -> None:
    """Run the frozen engine's own startup check."""
    print("\n--- engine selftest ---")
    env = dict(os.environ)
    # Point state at a temp dir so the selftest cannot touch a real install.
    env["BLOODLUST_DATA_DIR"] = str(DIST / "_selftest")
    proc = subprocess.run(
        [str(binary), "--selftest"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        raise SystemExit(
            "\nengine selftest FAILED — do not ship this build.\n"
            "The most common cause is that catalog.json was not bundled."
        )
    print("--- selftest passed ---\n")


def stage(binary: Path) -> Path:
    """Place the bundle where tauri.conf.json declares it."""
    src_dir = binary.parent
    dest_dir = ROOT / "src-tauri" / "sidecar"
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True)
    shutil.copytree(src_dir, dest_dir, dirs_exist_ok=True)
    staged = dest_dir / BIN_NAME
    if not staged.is_file():
        raise SystemExit(f"staging failed: {staged} missing")
    return staged


def report(binary: Path) -> None:
    total = sum(f.stat().st_size for f in binary.parent.rglob("*") if f.is_file())
    print(f"engine bundle : {binary.parent}")
    print(f"bundle size   : {total / (1024 * 1024):.1f} MB")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-selftest", action="store_true",
                        help="skip the startup verification (not recommended)")
    parser.add_argument("--no-stage", action="store_true",
                        help="build only; do not copy into src-tauri/")
    parser.add_argument("--force", action="store_true",
                        help="rebuild even when a staged engine already exists")
    args = parser.parse_args()

    check_python()
    ensure_pyinstaller()

    binary = build(force=args.force)
    if not args.no_selftest:
        selftest(binary)
    if not args.no_stage:
        binary = stage(binary)
    report(binary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
