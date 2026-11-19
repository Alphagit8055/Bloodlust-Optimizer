"""Junk cleanup actions.

Everything here deletes files that Windows or an emulator will happily
regenerate: temp folders, crash dumps, prefetch history, GPU shader caches.

Rules that keep this honest:

1. **Never touches the running engine.** A PyInstaller build extracts itself
   into ``%TEMP%`` at launch; the sweep skips anything whose path contains a
   ``_MEI`` directory component so cleanup can never pull the ground out from
   under this process.
2. **Locked files are skipped, not forced.** Anything that raises ``OSError``
   is counted and reported, never retried with elevated rights, never
   ``taskkill``'d.
3. **Not reversible.** Deleting caches is the point; the Undo journal gets a
   ``FILE`` change marked ``reversible=False`` so the UI shows the deletion
   without pretending it can be undone.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..core.result import ActionResult, Change, ChangeKind
from .base import action

_SELF_GUARD = ("_MEI", "bloodlust", "pyinstaller")


def _sweep(result: ActionResult, candidates: list[Path], label: str) -> None:
    total_files = 0
    total_bytes = 0
    skipped = 0

    for root in candidates:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root, topdown=True):
            # Prune our own runtime extraction tree before entering it.
            dirnames[:] = [d for d in dirnames if not _is_self_owned(Path(dirpath) / d)]

            for name in list(filenames):
                path = Path(dirpath) / name
                if _is_self_owned(path):
                    skipped += 1
                    continue
                try:
                    size = path.stat().st_size
                    path.unlink()
                    total_files += 1
                    total_bytes += size
                except OSError:
                    skipped += 1

            # Remove empty directories left behind, deepest first.
            for name in list(dirnames):
                path = Path(dirpath) / name
                try:
                    if path.exists() and not any(path.iterdir()):
                        path.rmdir()
                except OSError:
                    pass

    mb = total_bytes / (1024 * 1024)
    result.add(
        Change(
            kind=ChangeKind.FILE,
            target=label,
            detail=f"deleted {total_files} file(s), freed {mb:.1f} MB ({skipped} in use, skipped)",
            reversible=False,
        )
    )
    result.message = f"deleted {total_files} file(s), freed {mb:.1f} MB"
    if skipped:
        result.warn(f"{skipped} item(s) were in use and left alone")


def _is_self_owned(path: Path) -> bool:
    text = str(path).lower()
    return any(marker.lower() in text for marker in _SELF_GUARD)


def _user_temp() -> Path:
    return Path(os.environ.get("TEMP") or os.environ.get("TMP") or Path.home() / "AppData" / "Local" / "Temp")


@action(doc="Delete everything deletable in your own TEMP folder. Junk files, logs and partial downloads; anything in use is skipped.")
def clean_user_temp(ctx, args) -> ActionResult:
    result = ActionResult(action_id=ctx.action_id)
    _sweep(result, [_user_temp()], "%TEMP%")
    return result


@action(doc="Delete everything deletable in C:\\Windows\\Temp. Needs elevation; anything in use is skipped.")
def clean_windows_temp(ctx, args) -> ActionResult:
    result = ActionResult(action_id=ctx.action_id)
    _sweep(result, [Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Temp"], "C:\\Windows\\Temp")
    return result


@action(doc="Delete Windows Error Reporting crash dumps (user and machine) and Minidump files.")
def clean_crash_dumps(ctx, args) -> ActionResult:
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    program_data = Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData")
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    candidates = [
        local / "CrashDumps",
        local / "Microsoft" / "Windows" / "WER" / "ReportArchive",
        local / "Microsoft" / "Windows" / "WER" / "ReportQueue",
        program_data / "Microsoft" / "Windows" / "WER" / "ReportArchive",
        program_data / "Microsoft" / "Windows" / "WER" / "ReportQueue",
        system_root / "Minidump",
    ]
    result = ActionResult(action_id=ctx.action_id)
    _sweep(result, candidates, "crash dumps and WER reports")
    return result


@action(doc="Delete prefetch (*.pf) files. Windows rebuilds them; the first launch of each app after this run is slightly slower.")
def clean_prefetch(ctx, args) -> ActionResult:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    prefetch = system_root / "Prefetch"
    result = ActionResult(action_id=ctx.action_id)
    # Only .pf files, not the directory itself.
    pf_files: list[Path] = []
    try:
        pf_files = list(prefetch.glob("*.pf")) if prefetch.is_dir() else []
    except OSError:
        pf_files = []

    total_bytes = 0
    skipped = 0
    for path in pf_files:
        try:
            total_bytes += path.stat().st_size
            path.unlink()
        except OSError:
            skipped += 1

    mb = total_bytes / (1024 * 1024)
    result.add(
        Change(
            kind=ChangeKind.FILE,
            target=str(prefetch),
            detail=f"deleted {len(pf_files) - skipped} prefetch file(s), freed {mb:.1f} MB",
            reversible=False,
        )
    )
    result.message = f"prefetch cleared ({len(pf_files) - skipped} files, {mb:.1f} MB)"
    if skipped:
        result.warn(f"{skipped} prefetch file(s) were in use and left alone")
    return result


@action(doc="Delete GPU shader caches (NVIDIA and AMD). The drivers rebuild them; the first launch after this run stutters a little while they do.")
def clean_shader_caches(ctx, args) -> ActionResult:
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    candidates = [
        local / "NVIDIA" / "GLCache",
        local / "NVIDIA" / "DXCache",
        local / "NVIDIA" / "NV_Cache",
        local / "AMD" / "DxCache",
        local / "AMD" / "DxcCache",
        local / "AMD" / "GLCache",
        local / "D3DSCache",
    ]
    result = ActionResult(action_id=ctx.action_id)
    _sweep(result, candidates, "GPU shader caches")
    return result


HANDLERS = {
    "cleanup.clean_user_temp": clean_user_temp,
    "cleanup.clean_windows_temp": clean_windows_temp,
    "cleanup.clean_crash_dumps": clean_crash_dumps,
    "cleanup.clean_prefetch": clean_prefetch,
    "cleanup.clean_shader_caches": clean_shader_caches,
}
