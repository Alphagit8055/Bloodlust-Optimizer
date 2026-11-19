"""GameLoop-specific actions.

Deliberately narrow. GameLoop's engine settings — resolution, renderer, frame
cap, device profile — live in its own config UI, and a tool that guesses at
that file format will corrupt it. What is done here is limited to things that
are safe to do from outside: cache, autostart, and Defender exclusions.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..core.result import ActionResult, Change, ChangeKind
from .base import action

# Only these directory names are ever candidates for cache cleanup, and only
# when they sit inside the detected install root. Anything else is left alone.
_CACHE_NAMES = ("cache", "temp", "tmp", "logs", "crashdumps", "GPUCache", "ShaderCache")

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _root(ctx, key: str = "gameloop") -> Path | None:
    info = ctx.emulators.get(key)
    if not info or not getattr(info, "installed", False) or not getattr(info, "root", None):
        return None
    root = Path(info.root)
    return root if root.is_dir() else None


@action(
    args={"emulator": {"type": str, "choices": ("gameloop", "mumu", "ldplayer"), "default": "gameloop"}},
    doc="Report the detected install root, version, executables and ADB path. Read-only.",
)
def report_install(ctx, args) -> ActionResult:
    key = args.get("emulator", "gameloop")
    info = ctx.emulators.get(key)
    result = ActionResult(action_id=ctx.action_id)

    if not info or not getattr(info, "installed", False):
        result.message = f"{key} was not found on this machine"
        return result

    for label, value in (
        ("install root", info.root),
        ("launcher", info.exe or "not found"),
        ("adb", info.adb or "not found"),
        ("found via", info.source or "unknown"),
    ):
        result.add(
            Change(kind=ChangeKind.NOTE, target=label, detail=str(value), reversible=False)
        )

    root = Path(info.root)
    if root.is_dir():
        total = 0
        files = 0
        for p in root.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
                    files += 1
            except OSError:
                continue
        result.add(
            Change(
                kind=ChangeKind.NOTE,
                target="install size",
                detail=f"{total / (1024 ** 3):.2f} GB across {files} files",
                reversible=False,
            )
        )

    result.message = f"{info.name} found at {info.root}"
    return result


@action(
    args={
        "emulator": {"type": str, "choices": ("gameloop", "mumu", "ldplayer"), "default": "gameloop"},
        "dry_run": {"type": bool, "default": False},
    },
    doc="Delete the emulator's cache, temp and log folders. Reclaims disk and avoids stale shader caches, which are a common cause of stutter after an update.",
)
def clear_cache(ctx, args) -> ActionResult:
    key = args.get("emulator", "gameloop")
    result = ActionResult(action_id=ctx.action_id)

    root = _root(ctx, key)
    if root is None:
        result.message = f"{key} is not installed; nothing to clean"
        return result

    freed = 0
    removed_dirs = 0
    # Bounded walk: never follow outside the detected root.
    for candidate in root.rglob("*"):
        try:
            if not candidate.is_dir():
                continue
            if candidate.name not in _CACHE_NAMES:
                continue
            # Defence in depth: refuse anything that escaped the root via a
            # junction or a symlink.
            resolved = candidate.resolve()
            if not str(resolved).lower().startswith(str(root.resolve()).lower()):
                result.warn(f"skipped {candidate}: resolves outside the install root")
                continue

            size = sum(
                (f.stat().st_size for f in candidate.rglob("*") if f.is_file()),
                start=0,
            )
            if args.get("dry_run"):
                result.add(
                    Change(
                        kind=ChangeKind.NOTE,
                        target=str(candidate.relative_to(root)),
                        detail=f"would free {size / (1024 ** 2):.1f} MB",
                        reversible=False,
                    )
                )
                freed += size
                removed_dirs += 1
                continue

            shutil.rmtree(candidate, ignore_errors=True)
            freed += size
            removed_dirs += 1
            result.add(
                Change(
                    kind=ChangeKind.FILE,
                    target=str(candidate.relative_to(root)),
                    detail=f"freed {size / (1024 ** 2):.1f} MB",
                    reversible=False,
                )
            )
        except OSError as exc:
            result.warn(f"could not clean {candidate.name}: {exc}")

    if removed_dirs == 0:
        result.message = "no cache folders found to clean"
    else:
        verb = "would free" if args.get("dry_run") else "freed"
        result.message = f"{verb} {freed / (1024 ** 2):.1f} MB across {removed_dirs} folder(s)"
    return result


@action(
    args={
        "disabled": {"type": bool, "default": True},
        "emulator": {"type": str, "choices": ("gameloop", "mumu", "ldplayer"), "default": "gameloop"},
    },
    doc="Stop the emulator launching at logon. A background emulator holding GPU and disk resources is a common cause of a slow first match.",
)
def set_autostart(ctx, args) -> ActionResult:
    disabled = bool(args.get("disabled", True))
    key = args.get("emulator", "gameloop")
    result = ActionResult(action_id=ctx.action_id)

    info = ctx.emulators.get(key)
    if not info or not getattr(info, "exe", None):
        result.message = "no launcher path detected; cannot identify the Run entry"
        return result

    exe = str(info.exe)
    # The Run entry name varies, so scan the small fixed set of names these
    # installers actually use rather than guessing one.
    candidates = ("GameLoop", "TxGameAssistant", "MuMu Player", "LDPlayer", "NemuPlayer")
    touched = 0
    for name in candidates:
        existing = ctx.reg.get_value("HKCU", _RUN_KEY, name)
        if not existing:
            continue
        value = str(existing[1])
        if Path(exe).name.lower() not in value.lower():
            continue  # a different program's entry that happens to share a name

        if disabled:
            change = ctx.reg.delete_value(
                "HKCU", _RUN_KEY, name, describe=f"{name} will no longer start at logon"
            )
        else:
            change = ctx.reg.set_value(
                "HKCU", _RUN_KEY, name, existing[0], value,
                describe=f"{name} will start at logon",
            )
        if change:
            result.add(change)
            touched += 1

    result.message = (
        f"{touched} autostart entry(ies) {'removed' if disabled else 'restored'}"
        if touched
        else "no matching autostart entry was present"
    )
    return result


@action(
    args={
        "add": {"type": bool, "default": True},
        "emulator": {"type": str, "choices": ("gameloop", "mumu", "ldplayer"), "default": "gameloop"},
    },
    doc="Exclude the emulator install folder from Windows Defender real-time scanning. Emulator disk I/O is constant and heavy, and scanning it on every read is a real, measurable cost. The trade-off is that malware inside that folder would go unscanned.",
)
def set_defender_exclusion(ctx, args) -> ActionResult:
    add = bool(args.get("add", True))
    key = args.get("emulator", "gameloop")
    result = ActionResult(action_id=ctx.action_id)

    root = _root(ctx, key)
    if root is None:
        result.message = f"{key} is not installed; nothing to exclude"
        return result

    verb = "Add-MpPreference" if add else "Remove-MpPreference"
    out = ctx.shell.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-Command",
            f"{verb} -ExclusionPath '{str(root)}'",
        ],
        timeout=90,
        describe=f"{verb} for {root}",
    )
    if not out.ok and not out.skipped:
        raise RuntimeError(
            f"{verb} failed (is Defender managed by a third-party AV?): "
            f"{out.stderr or out.stdout}"
        )

    result.add(
        Change(
            kind=ChangeKind.COMMAND,
            target=verb,
            detail=f"exclusion path {root} {'added' if add else 'removed'}",
            reversible=True,
        )
    )
    result.message = (
        f"Defender exclusion {'added' if add else 'removed'} for {root}"
    )
    if add:
        result.warn(
            "files inside the emulator folder are no longer scanned in real time. "
            "Only do this for an install folder you trust"
        )
    return result


HANDLERS = {
    "gameloop.report_install": report_install,
    "gameloop.clear_cache": clear_cache,
    "gameloop.set_autostart": set_autostart,
    "gameloop.set_defender_exclusion": set_defender_exclusion,
}
