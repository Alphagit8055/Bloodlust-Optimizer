"""PyInstaller entry point.

This exists because ``bloodlust/__main__.py`` cannot be the entry point itself.
PyInstaller executes the named script as ``__main__`` with no parent package,
so every relative import inside it — ``from .core.logging import setup`` —
raises ``ImportError: attempted relative import with no known parent package``.

The failure is invisible until the bundle runs: the build succeeds, the exe
launches, and then dies immediately. That is why the spec is always built
together with ``--selftest``.

This shim adds ``src/`` to the path and imports the package *absolutely*, which
works identically frozen and unfrozen.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Frozen: PyInstaller extracts to sys._MEIPASS. Unfrozen: this file's directory.
if getattr(sys, "frozen", False):
    _BASE = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
else:
    _BASE = Path(__file__).resolve().parent

_SRC = _BASE / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _run() -> int:
    # Imported here, after the path is set up.
    from bloodlust.__main__ import main

    return main()


if __name__ == "__main__":
    raise SystemExit(_run())
