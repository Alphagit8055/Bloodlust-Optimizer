"""Filesystem locations the engine owns.

Everything lives under one per-user directory. The engine never writes outside
it, which makes "delete this folder" a genuine uninstall for all state.

``BLOODLUST_DATA_DIR`` overrides the location; the test suite uses this to run
the whole engine against a throwaway temporary directory.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_DIR_NAME = "Bloodlust Optimizer"


def data_dir() -> Path:
    override = os.environ.get("BLOODLUST_DATA_DIR")
    if override:
        return Path(override)

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")

    return Path(base) / _DIR_NAME


def undo_dir() -> Path:
    p = data_dir() / "undo"
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_file() -> Path:
    return data_dir() / "state.json"


def session_file() -> Path:
    return data_dir() / "session.json"


def ensure_dirs() -> None:
    for fn in (data_dir, undo_dir, log_dir):
        fn()


__all__ = ["data_dir", "undo_dir", "log_dir", "state_file", "session_file", "ensure_dirs"]
