"""Windows shell, scheduling and input actions.

These are the ordinary, widely documented Windows settings. Every one is
written through the journaling facade, so the Undo tab can put them all back.
"""

from __future__ import annotations

from ..core import registry as reg
from ..core.result import ActionResult
from .base import action

_GAME_BAR = r"Software\Microsoft\GameBar"
_GAME_DVR = r"Software\Microsoft\Windows\CurrentVersion\GameDVR"
_GAME_CONFIG = r"System\GameConfigStore"
_POLICY_DVR = r"SOFTWARE\Policies\Microsoft\Windows\GameDVR"
_MMCS = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
_MMCS_GAMES = _MMCS + r"\Tasks\Games"
_VISUAL_FX = r"Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects"
_MOUSE = r"Control Panel\Mouse"
_POWER_THROTTLE = r"SYSTEM\CurrentControlSet\Control\Power\PowerThrottling"


@action(
    args={"enabled": {"type": bool, "default": True}},
    doc="Turn Windows Game Mode on or off.",
)
def set_game_mode(ctx, args) -> ActionResult:
    enabled = bool(args.get("enabled", True))
    value = 1 if enabled else 0
    result = ActionResult(action_id=ctx.action_id)
    for path, name in (
        (_GAME_BAR, "AutoGameModeEnabled"),
        (_GAME_BAR, "AllowAutoGameMode"),
    ):
        change = ctx.reg.set_value(
            "HKCU", path, name, reg.REG_DWORD, value,
            describe=f"Game Mode = {'on' if enabled else 'off'}",
        )
        if change:
            result.add(change)
    result.message = (
        f"Game Mode {'enabled' if enabled else 'disabled'}"
        if result.change_count
        else f"Game Mode was already {'enabled' if enabled else 'disabled'}"
    )
    return result


@action(
    args={"enabled": {"type": bool, "default": False}},
    doc="Turn the Game Bar overlay and background recording on or off. Background recording costs frames even when the overlay is not shown.",
)
def set_game_dvr(ctx, args) -> ActionResult:
    enabled = bool(args.get("enabled", False))
    value = 1 if enabled else 0
    result = ActionResult(action_id=ctx.action_id)

    for hive, path, name in (
        ("HKCU", _GAME_DVR, "AppCaptureEnabled"),
        ("HKCU", _GAME_CONFIG, "GameDVR_Enabled"),
        ("HKLM", _POLICY_DVR, "AllowGameDVR"),
    ):
        change = ctx.reg.set_value(
            hive, path, name, reg.REG_DWORD, value,
            describe=f"Game DVR / background capture = {'on' if enabled else 'off'}",
        )
        if change:
            result.add(change)

    result.message = (
        f"Game DVR {'enabled' if enabled else 'disabled'}"
        if result.change_count
        else f"Game DVR was already {'enabled' if enabled else 'disabled'}"
    )
    return result


@action(
    args={"mode": {"type": int, "min": 0, "max": 2, "default": 2}},
    doc="Fullscreen optimisation behaviour. 2 = force exclusive fullscreen, which removes a composition pass between the emulator and the display.",
)
def set_fullscreen_optimisations(ctx, args) -> ActionResult:
    mode = int(args.get("mode", 2))
    result = ActionResult(action_id=ctx.action_id)
    plan = [
        ("GameDVR_FSEBehaviorMode", reg.REG_DWORD, mode),
        ("GameDVR_HonorUserFSEBehaviorMode", reg.REG_DWORD, 1),
        ("GameDVR_DXGIHonorFSEWindowsCompatible", reg.REG_DWORD, 1),
        ("GameDVR_EFSEFeatureFlags", reg.REG_DWORD, 0),
    ]
    for name, vtype, value in plan:
        change = ctx.reg.set_value(
            "HKCU", _GAME_CONFIG, name, vtype, value,
            describe=f"fullscreen optimisations -> mode {mode}",
        )
        if change:
            result.add(change)
    result.message = (
        f"fullscreen behaviour set to mode {mode}"
        if result.change_count
        else "fullscreen behaviour already as requested"
    )
    return result


@action(
    args={
        "system_responsiveness": {"type": int, "min": 0, "max": 100, "default": 10},
        "gpu_priority": {"type": int, "min": 0, "max": 8, "default": 8},
        "priority": {"type": int, "min": 0, "max": 6, "default": 6},
    },
    doc="Raise the MMCSS 'Games' scheduling class so the emulator's audio and video threads are not preempted by background work.",
)
def set_mmcss_games(ctx, args) -> ActionResult:
    responsiveness = int(args.get("system_responsiveness", 10))
    gpu_priority = int(args.get("gpu_priority", 8))
    priority = int(args.get("priority", 6))
    result = ActionResult(action_id=ctx.action_id)

    change = ctx.reg.set_value(
        "HKLM", _MMCS, "SystemResponsiveness", reg.REG_DWORD, responsiveness,
        describe=f"multimedia system responsiveness = {responsiveness}",
    )
    if change:
        result.add(change)

    # 0xFFFFFFFF removes the network throughput cap MMCSS applies while
    # multimedia is active. It is a DWORD, not a signed value.
    change = ctx.reg.set_value(
        "HKLM", _MMCS, "NetworkThrottlingIndex", reg.REG_DWORD, 0xFFFFFFFF,
        describe="network throttling index = unlimited",
    )
    if change:
        result.add(change)

    for name, vtype, value, detail in (
        ("GPU Priority", reg.REG_DWORD, gpu_priority, f"GPU priority = {gpu_priority}"),
        ("Priority", reg.REG_DWORD, priority, f"scheduling priority = {priority}"),
        ("Scheduling Category", reg.REG_SZ, "High", "scheduling category = High"),
        ("SFIO Priority", reg.REG_SZ, "High", "I/O priority = High"),
    ):
        change = ctx.reg.set_value(
            "HKLM", _MMCS_GAMES, name, vtype, value, describe=detail
        )
        if change:
            result.add(change)

    result.message = (
        f"{result.change_count} scheduling value(s) raised for the Games task"
        if result.change_count
        else "MMCSS Games task already tuned"
    )
    return result


@action(
    args={"performance": {"type": bool, "default": True}},
    doc="Switch Windows visual effects to the performance preset (disables animations and shadows).",
)
def set_visual_effects(ctx, args) -> ActionResult:
    performance = bool(args.get("performance", True))
    change = ctx.reg.set_value(
        "HKCU", _VISUAL_FX, "VisualFXSetting", reg.REG_DWORD, 2 if performance else 0,
        describe=f"visual effects = {'best performance' if performance else 'let Windows choose'}",
    )
    result = ActionResult(action_id=ctx.action_id)
    if change:
        result.add(change)
        result.message = "visual effects set to best performance"
    else:
        result.message = "visual effects already as requested"
    return result


@action(
    args={"disabled": {"type": bool, "default": True}},
    doc="Turn off pointer acceleration ('Enhance pointer precision'). This changes aim feel rather than latency, but raw 1:1 input is what most players want.",
)
def set_pointer_acceleration(ctx, args) -> ActionResult:
    disabled = bool(args.get("disabled", True))
    value = "0" if disabled else "1"
    result = ActionResult(action_id=ctx.action_id)

    # These three are REG_SZ, not DWORD - a common mistake that makes the
    # setting silently not apply.
    for name in ("MouseSpeed", "MouseThreshold1", "MouseThreshold2"):
        change = ctx.reg.set_value(
            "HKCU", _MOUSE, name, reg.REG_SZ, "0" if disabled else "6",
            describe=f"{name} = {'raw input' if disabled else 'accelerated'}",
        )
        if change:
            result.add(change)

    if result.change_count:
        result.message = (
            "pointer acceleration disabled; sign out and back in for it to take effect"
            if disabled
            else "pointer acceleration restored to default"
        )
    else:
        result.message = "pointer settings already as requested"
    return result


@action(
    args={"disabled": {"type": bool, "default": True}},
    doc="Stop Windows throttling background threads. Helps an emulator whose helper threads get parked mid-frame.",
)
def set_power_throttling(ctx, args) -> ActionResult:
    disabled = bool(args.get("disabled", True))
    change = ctx.reg.set_value(
        "HKLM", _POWER_THROTTLE, "PowerThrottlingOff", reg.REG_DWORD, 1 if disabled else 0,
        describe=f"EcoQoS power throttling = {'off' if disabled else 'on'}",
    )
    result = ActionResult(action_id=ctx.action_id)
    if change:
        result.add(change)
        result.needs_reboot = True
        result.message = f"power throttling {'disabled' if disabled else 'enabled'}; reboot required"
    else:
        result.message = f"power throttling already {'disabled' if disabled else 'enabled'}"
    return result


HANDLERS = {
    "windows.set_game_mode": set_game_mode,
    "windows.set_game_dvr": set_game_dvr,
    "windows.set_fullscreen_optimisations": set_fullscreen_optimisations,
    "windows.set_mmcss_games": set_mmcss_games,
    "windows.set_visual_effects": set_visual_effects,
    "windows.set_pointer_acceleration": set_pointer_acceleration,
    "windows.set_power_throttling": set_power_throttling,
}
