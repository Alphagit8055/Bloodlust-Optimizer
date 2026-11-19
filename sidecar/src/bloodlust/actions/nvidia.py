"""GPU scheduling and driver actions.

Note that Hardware-Accelerated GPU Scheduling is a *Windows* setting that
affects every vendor, not an NVIDIA one — it lives here because that is where
users look for it. The vendor gate on those actions is therefore deliberately
absent.
"""

from __future__ import annotations

import re

from ..core import registry as reg
from ..core.result import ActionResult, Change, ChangeKind
from .base import action

_GRAPHICS_DRIVERS = r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers"
_GPU_PREFERENCES = r"Software\Microsoft\DirectX\UserGpuPreferences"


@action(
    args={"enabled": {"type": bool, "default": True}},
    doc="Toggle Hardware-Accelerated GPU Scheduling. Helps on most modern GPUs, but it is a per-system judgement call, so both directions are offered.",
)
def set_hags(ctx, args) -> ActionResult:
    enabled = bool(args.get("enabled", True))
    change = ctx.reg.set_value(
        "HKLM", _GRAPHICS_DRIVERS, "HwSchMode", reg.REG_DWORD, 2 if enabled else 1,
        describe=f"HAGS = {'on' if enabled else 'off'}",
    )
    result = ActionResult(action_id=ctx.action_id)
    if change:
        result.add(change)
        result.needs_reboot = True
        result.message = f"HAGS {'enabled' if enabled else 'disabled'}; reboot required"
    else:
        result.message = f"HAGS was already {'enabled' if enabled else 'disabled'}"
    return result


@action(
    args={"seconds": {"type": int, "min": 2, "max": 30, "default": 8}},
    doc="Raise the Timeout Detection and Recovery delay so a long emulator frame is not mistaken for a hung GPU.",
)
def set_tdr_delay(ctx, args) -> ActionResult:
    seconds = int(args.get("seconds", 8))
    result = ActionResult(action_id=ctx.action_id)

    change = ctx.reg.set_value(
        "HKLM", _GRAPHICS_DRIVERS, "TdrDelay", reg.REG_DWORD, seconds,
        describe=f"TDR delay = {seconds}s",
    )
    if change:
        result.add(change)
        result.needs_reboot = True
        result.message = f"TDR delay set to {seconds}s; reboot required"
    else:
        result.message = f"TDR delay was already {seconds}s"

    if seconds > 15:
        result.warn(
            "a very long TDR delay means a genuinely hung GPU takes that long "
            "to recover, which will look like a frozen display"
        )
    return result


@action(
    args={"remove": {"type": bool, "default": True}},
    doc="Remove a custom TDR delay and return to the Windows default (2s).",
)
def reset_tdr_delay(ctx, args) -> ActionResult:
    result = ActionResult(action_id=ctx.action_id)
    change = ctx.reg.delete_value(
        "HKLM", _GRAPHICS_DRIVERS, "TdrDelay", describe="TDR delay -> Windows default"
    )
    if change:
        result.add(change)
        result.needs_reboot = True
        result.message = "custom TDR delay removed; reboot required"
    else:
        result.message = "no custom TDR delay was set"
    return result


@action(
    args={
        "emulator": {
            "type": str,
            "choices": ("gameloop", "mumu", "ldplayer"),
            "default": "gameloop",
        },
        "preference": {"type": int, "min": 0, "max": 2, "default": 2},
    },
    doc="Route the emulator executable to a specific GPU through the Windows Graphics settings. 0 = let Windows decide, 1 = power saving (iGPU), 2 = high performance (dGPU).",
)
def set_emulator_gpu_preference(ctx, args) -> ActionResult:
    """Writes the same key the Settings app writes, so the choice is visible
    there and can be changed by hand. The executable path comes from *detection*,
    never from the catalog."""
    key = args.get("emulator", "gameloop")
    preference = int(args.get("preference", 2))

    info = ctx.emulators.get(key)
    result = ActionResult(action_id=ctx.action_id)
    if not info or not getattr(info, "installed", False):
        result.message = f"{key} is not installed; nothing to route"
        return result

    targets: list[str] = []
    if getattr(info, "exe", None):
        targets.append(info.exe)
    if getattr(info, "root", None):
        # GameLoop renders through a second process; routing only the launcher
        # leaves the actual renderer on the iGPU.
        from pathlib import Path

        for name in ("AndroidEmulator.exe", "MuMuVMMHeadless.exe", "dnplayer.exe"):
            for sub in ("", "MobileGamePCShared", "shell", "UI"):
                candidate = Path(info.root) / sub / name if sub else Path(info.root) / name
                if candidate.is_file():
                    targets.append(str(candidate))

    seen: set[str] = set()
    for exe in targets:
        if exe.lower() in seen:
            continue
        seen.add(exe.lower())
        value = f"GpuPreference={preference};"
        change = ctx.reg.set_value(
            "HKCU", _GPU_PREFERENCES, exe, reg.REG_SZ, value,
            describe=f"{Path(exe).name} -> GPU preference {preference}",
        )
        if change:
            result.add(change)

    if result.change_count:
        result.message = f"{result.change_count} executable(s) routed to GPU preference {preference}"
        result.warn(
            "Windows Graphics settings can override a per-app setting made in "
            "the NVIDIA Control Panel, so check both if the result looks wrong"
        )
    else:
        result.message = "GPU preference already set for every emulator executable"
    return result


@action(doc="Report the installed NVIDIA driver version and basic GPU state. Read-only.")
def report_nvidia_driver(ctx, args) -> ActionResult:
    result = ActionResult(action_id=ctx.action_id)
    out = ctx.shell.run(
        ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,power.limit",
         "--format=csv,noheader"],
        record=False,
        describe="query NVIDIA driver",
    )
    if not out.ok or not out.stdout:
        result.message = "nvidia-smi is not available (no NVIDIA GPU, or driver not installed)"
        return result

    for line in out.stdout.splitlines():
        if line.strip():
            result.add(
                Change(
                    kind=ChangeKind.NOTE,
                    target="nvidia-smi",
                    detail=line.strip(),
                    reversible=False,
                )
            )
    result.message = f"{result.change_count} GPU(s) reported"
    return result


HANDLERS = {
    "nvidia.set_hags": set_hags,
    "nvidia.set_tdr_delay": set_tdr_delay,
    "nvidia.reset_tdr_delay": reset_tdr_delay,
    "nvidia.set_emulator_gpu_preference": set_emulator_gpu_preference,
    "nvidia.report_driver": report_nvidia_driver,
}
