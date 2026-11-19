"""ADB actions against the emulator guest.

Two constraints make this safe:

1. **A fixed allowlist of settings keys.** The guest namespace is wide and some
   of it is genuinely risky, so only the keys listed in ``ALLOWED_SETTINGS``
   can be touched. The catalog may choose which of them to use; it cannot add
   one.
2. **Read before write.** The previous value is fetched with
   ``settings get`` and recorded, so undo restores the guest rather than
   blindly writing a default back over a user's own choice.

These are the guest's own developer options, not host settings, and they are all
reverted by a factory reset of the emulator.
"""

from __future__ import annotations

from ..core.result import ActionResult, Change, ChangeKind
from .base import action, adb_shell

# namespace -> {key: (allowed values)}
ALLOWED_SETTINGS: dict[str, dict[str, tuple]] = {
    "global": {
        "window_animation_scale": ("0", "0.5", "1", "1.5", "2", "5", "10"),
        "transition_animation_scale": ("0", "0.5", "1", "1.5", "2", "5", "10"),
        "animator_duration_scale": ("0", "0.5", "1", "1.5", "2", "5", "10"),
        "gpu_debug_app": ("",),
    },
    "system": {
        "show_touches": ("0", "1"),
        "pointer_speed": tuple(str(i) for i in range(-7, 8)),
    },
    "secure": {
        "location_mode": ("0", "1", "2", "3"),
    },
}


@action(
    args={"serial": {"type": str, "default": ""}},
    doc="List connected emulator ADB devices and their state. Read-only.",
)
def report_devices(ctx, args) -> ActionResult:
    result = ActionResult(action_id=ctx.action_id)

    adb = None
    for key in ("gameloop", "mumu", "ldplayer"):
        info = ctx.emulators.get(key)
        if info and getattr(info, "adb", None):
            adb = info.adb
            break
    if adb is None:
        result.message = "no adb executable was found for any detected emulator"
        return result

    out = ctx.shell.run([adb, "devices", "-l"], record=False, describe="adb devices")
    if not out.ok:
        result.message = f"adb failed: {out.stderr or out.stdout}"
        return result

    lines = [ln for ln in out.stdout.splitlines() if ln.strip()][1:]
    if not lines:
        result.message = "adb is available but no emulator is currently running"
        return result

    for line in lines:
        result.add(
            Change(kind=ChangeKind.ADB, target="device", detail=line.strip(), reversible=False)
        )
    result.message = f"{len(lines)} device(s) connected"
    return result


@action(
    args={
        "namespace": {"type": str, "choices": tuple(ALLOWED_SETTINGS), "default": "global"},
        "key": {"type": str, "default": "window_animation_scale"},
        "value": {"type": str, "default": "0.5"},
        "serial": {"type": str, "default": ""},
    },
    doc="Set an allowlisted guest setting. Removing animations is the visible one; it makes the UI feel instant because there is nothing to wait for.",
)
def set_guest_setting(ctx, args) -> ActionResult:
    namespace = args.get("namespace", "global")
    key = args.get("key", "window_animation_scale")
    value = str(args.get("value", "0.5"))
    serial = args.get("serial") or None
    device_args = ["-s", serial] if serial else []

    allowed = ALLOWED_SETTINGS.get(namespace, {})
    if key not in allowed:
        raise ValueError(
            f"setting {namespace}/{key} is not in the allowlist "
            f"(allowed: {', '.join(sorted(allowed))})"
        )
    if value not in allowed[key]:
        raise ValueError(f"value {value!r} is not permitted for {namespace}/{key}")

    result = ActionResult(action_id=ctx.action_id)

    get = adb_shell(ctx, device_args, ["settings", "get", namespace, key], describe=f"read {key}")
    previous = (get.stdout or "").strip() if get.ok else None
    if previous in ("null", ""):
        previous = None

    if previous == value:
        result.message = f"{namespace}/{key} is already {value}"
        return result

    put = adb_shell(
        ctx, device_args, ["settings", "put", namespace, key, value],
        describe=f"set {key}={value}",
    )
    if not put.ok and not put.skipped:
        raise RuntimeError(f"settings put failed: {put.stderr or put.stdout}")

    # Undo is a command restore, so it survives across restarts via the journal.
    if previous is not None:
        adb = _find_adb(ctx)
        if adb:
            ctx.shell.record_restore(
                [adb] + device_args + ["shell", "settings", "put", namespace, key, previous],
                describe=f"restore {namespace}/{key} to {previous}",
            )
    result.add(
        Change(
            kind=ChangeKind.ADB,
            target=f"settings {namespace}/{key}",
            detail=f"{previous or 'unset'} -> {value}",
            reversible=previous is not None,
            previous=previous,
        )
    )
    result.message = f"{namespace}/{key} set to {value}"
    return result


@action(
    args={"serial": {"type": str, "default": ""}},
    doc="Turn off all three animation scale settings in one go.",
)
def disable_guest_animations(ctx, args) -> ActionResult:
    result = ActionResult(action_id=ctx.action_id)
    serial = args.get("serial") or None
    device_args = ["-s", serial] if serial else []

    for key in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale"):
        get = adb_shell(ctx, device_args, ["settings", "get", "global", key], describe=f"read {key}")
        previous = (get.stdout or "").strip() if get.ok else None
        if previous in ("null", ""):
            previous = None

        put = adb_shell(
            ctx, device_args, ["settings", "put", "global", key, "0"], describe=f"{key}=0"
        )
        if not put.ok and not put.skipped:
            result.warn(f"could not set {key}: {put.stderr or put.stdout}")
            continue

        adb = _find_adb(ctx)
        if adb and previous is not None:
            ctx.shell.record_restore(
                [adb] + device_args + ["settings", "put", "global", key, previous],
                describe=f"restore {key} to {previous}",
            )
        result.add(
            Change(
                kind=ChangeKind.ADB,
                target=f"settings global/{key}",
                detail=f"{previous or 'unset'} -> 0",
                reversible=previous is not None,
                previous=previous,
            )
        )

    result.message = (
        f"{result.change_count} animation scale(s) set to 0"
        if result.change_count
        else "guest animations were already disabled"
    )
    return result


def _find_adb(ctx) -> str | None:
    for key in ("gameloop", "mumu", "ldplayer"):
        info = ctx.emulators.get(key)
        if info and getattr(info, "adb", None):
            return info.adb
    return None


HANDLERS = {
    "adb.report_devices": report_devices,
    "adb.set_guest_setting": set_guest_setting,
    "adb.disable_guest_animations": disable_guest_animations,
}
