"""Power and CPU frequency actions.

These are the highest-value, lowest-risk tweaks in the set: an emulator's game
thread is single-threaded, so anything that lets Windows downclock or park the
core it is running on shows up directly as a frame-time spike.
"""

from __future__ import annotations

from ..core.result import ActionResult
from .base import (
    SET_CPMINCORES,
    SET_PERFBOOSTMODE,
    SET_PROCTHROTTLEMIN,
    SET_USB_SUSPEND,
    SET_WIRELESS_POWER,
    SUB_PROCESSOR,
    SUB_USB,
    SUB_WIRELESS,
    SCHEME_HIGH_PERF,
    SCHEME_ULTIMATE,
    PowerCfg,
    action,
)


@action(
    args={
        "scheme": {
            "type": str,
            "choices": ("high_performance", "ultimate"),
            "default": "ultimate",
        }
    },
    doc="Activate a performance power scheme, creating Ultimate Performance if absent.",
)
def set_performance_scheme(ctx, args) -> ActionResult:
    pc = PowerCfg(ctx.shell, ctx.journal)
    result = ActionResult(action_id=ctx.action_id)

    want = args.get("scheme", "ultimate")
    if want == "ultimate":
        guid = pc.scheme_by_name("Ultimate Performance")
        if guid is None:
            # Not present until it is duplicated from the hidden template.
            created = pc.duplicate_scheme(SCHEME_ULTIMATE)
            if created:
                guid = created
                result.warn("created the Ultimate Performance scheme from the Windows template")
        # Fall through to High Performance if the template is unavailable.
        target = guid or SCHEME_HIGH_PERF
        label = "Ultimate Performance" if guid else "High performance (fallback)"
    else:
        target = pc.scheme_by_name("High performance") or SCHEME_HIGH_PERF
        label = "High performance"

    change = pc.set_active(target, describe=f"activate {label}")
    if change:
        result.add(change)
        result.message = f"{label} is now the active power scheme"
    else:
        result.message = f"{label} was already active"
    return result


@action(
    args={
        "value": {"type": int, "min": 0, "max": 100, "default": 100},
        "on_battery": {"type": bool, "default": True},
    },
    doc="Raise the minimum processor state so cores do not downclock between frames.",
)
def set_min_processor_state(ctx, args) -> ActionResult:
    pc = PowerCfg(ctx.shell, ctx.journal)
    value = int(args.get("value", 100))
    on_battery = bool(args.get("on_battery", True))

    changes = pc.set_index(
        SUB_PROCESSOR,
        SET_PROCTHROTTLEMIN,
        ac=value,
        dc=value if on_battery else None,
        describe=f"minimum processor state = {value}%",
    )
    result = ActionResult(action_id=ctx.action_id)
    for c in changes:
        result.add(c)
    result.message = (
        f"minimum processor state set to {value}%"
        if changes
        else f"minimum processor state was already {value}%"
    )
    return result


@action(
    args={"value": {"type": int, "min": 1, "max": 100, "default": 100}},
    doc="Stop Windows parking cores, which otherwise forces thread migrations.",
)
def disable_core_parking(ctx, args) -> ActionResult:
    pc = PowerCfg(ctx.shell, ctx.journal)
    value = int(args.get("value", 100))
    changes = pc.set_index(
        SUB_PROCESSOR,
        SET_CPMINCORES,
        ac=value,
        dc=value,
        describe=f"minimum active cores = {value}%",
    )
    result = ActionResult(action_id=ctx.action_id)
    for c in changes:
        result.add(c)
    result.message = (
        f"core parking minimum set to {value}% (all cores stay available)"
        if changes
        else "core parking minimum was already correct"
    )
    return result


@action(
    args={"mode": {"type": int, "min": 0, "max": 5, "default": 2}},
    doc="Set turbo boost policy. 2 = Aggressive, 0 = Disabled, 5 = Efficient Enabled.",
)
def set_boost_policy(ctx, args) -> ActionResult:
    pc = PowerCfg(ctx.shell, ctx.journal)
    mode = int(args.get("mode", 2))
    changes = pc.set_index(
        SUB_PROCESSOR,
        SET_PERFBOOSTMODE,
        ac=mode,
        dc=None,
        describe=f"processor performance boost mode = {mode}",
    )
    result = ActionResult(action_id=ctx.action_id)
    for c in changes:
        result.add(c)
    result.message = (
        f"boost policy set to {mode} (plugged in)" if changes else "boost policy unchanged"
    )
    return result


@action(
    args={"index": {"type": int, "min": 0, "max": 3, "default": 0}},
    doc="Force the wireless adapter out of its power-saving states. Index 0 = Maximum Performance.",
)
def wireless_max_performance(ctx, args) -> ActionResult:
    """The on-battery default here is *Medium Power Saving*, which is a real and
    frequently missed source of latency spikes on laptops."""
    pc = PowerCfg(ctx.shell, ctx.journal)
    index = int(args.get("index", 0))
    changes = pc.set_index(
        SUB_WIRELESS,
        SET_WIRELESS_POWER,
        ac=index,
        dc=index,
        describe=f"wireless adapter power saving = {index} (0 = maximum performance)",
    )
    result = ActionResult(action_id=ctx.action_id)
    for c in changes:
        result.add(c)
    if changes:
        result.message = "wireless adapter will no longer enter a power-saving state"
    else:
        result.message = "wireless adapter power saving was already at maximum performance"
    return result


@action(
    args={"enabled": {"type": bool, "default": False}},
    doc="Disable USB selective suspend so a plugged-in controller cannot be dropped.",
)
def set_usb_selective_suspend(ctx, args) -> ActionResult:
    pc = PowerCfg(ctx.shell, ctx.journal)
    enabled = bool(args.get("enabled", False))
    value = 1 if enabled else 0
    changes = pc.set_index(
        SUB_USB,
        SET_USB_SUSPEND,
        ac=value,
        dc=value,
        describe=f"USB selective suspend = {'enabled' if enabled else 'disabled'}",
    )
    result = ActionResult(action_id=ctx.action_id)
    for c in changes:
        result.add(c)
    result.message = (
        f"USB selective suspend {'enabled' if enabled else 'disabled'}"
        if changes
        else "USB selective suspend already in the requested state"
    )
    if not enabled and changes:
        result.warn(
            "disabling USB suspend slightly raises idle power draw on battery"
        )
    return result


HANDLERS = {
    "power.set_performance_scheme": set_performance_scheme,
    "power.set_min_processor_state": set_min_processor_state,
    "power.disable_core_parking": disable_core_parking,
    "power.set_boost_policy": set_boost_policy,
    "power.wireless_max_performance": wireless_max_performance,
    "power.set_usb_selective_suspend": set_usb_selective_suspend,
}
