# PyInstaller spec for the Bloodlust Optimizer engine.
#
# onedir, not onefile: a onefile build unpacks ~60 MB into a temp directory on
# every launch, which adds a visible delay to app start for no benefit here
# since the sidecar ships inside the installer anyway.

from pathlib import Path

HERE = Path(SPECPATH).resolve()          # sidecar/
ROOT = HERE.parent                         # repo root
CATALOG = ROOT / "catalog" / "catalog.json"

if not CATALOG.is_file():
    raise SystemExit(f"bundled catalog not found at {CATALOG}")

# NOTE: the spec builds a windowed (console=False) engine. The JSON-RPC channel
# keeps working: the shell spawns the engine with its stdio handles explicitly
# piped, and a PyInstaller windowed bootloader inherits valid handles just like
# a console one — the only difference is no console window is created for the
# child. Verified locally by piping a hand-written PING into the frozen exe.
#
# The entry point is the shim, NOT bloodlust/__main__.py. Running
# __main__.py directly breaks every relative import inside the package.
a = Analysis(
    [str(HERE / "entrypoint.py")],
    pathex=[str(HERE / "src")],
    binaries=[],
    # The catalog travels inside the bundle so the app works with no network.
    datas=[(str(CATALOG), ".")],
    hiddenimports=[
        # Loaded by name from the catalog, so static analysis cannot see them.
        "bloodlust.actions.power",
        "bloodlust.actions.network",
        "bloodlust.actions.nvidia",
        "bloodlust.actions.windows",
        "bloodlust.actions.gameloop",
        "bloodlust.actions.adb",
        "bloodlust.actions.audit",
        "winreg",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Nothing here needs a GUI toolkit, a test runner, or numpy. Excluding
        # them cuts the bundle size substantially.
        "tkinter",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "matplotlib",
        "numpy",
        "pandas",
        "scipy",
        "PIL",
        "pytest",
        "IPython",
        "jupyter",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="bloodlust-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,         # no console window for the engine; stdout still works — see note below
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=None,
    # The shell elevates itself; the sidecar inherits that token. Marking it
    # here too means it still works if launched directly during development.
    uac_admin=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="bloodlust-sidecar",
)
