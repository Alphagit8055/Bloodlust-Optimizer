#!/usr/bin/env python3
"""Development launcher: runs the engine from source with no PyInstaller build.

Point BLOODLUST_SIDECAR at this file to use `tauri dev` without rebuilding the
engine on every change. It is not used in production.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bloodlust.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
