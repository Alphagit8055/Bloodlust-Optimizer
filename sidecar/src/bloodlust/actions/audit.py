"""Read-only audit actions.

These never mutate anything. They exist so a user can answer "why is this
locked?" and "what is my machine actually doing?" without leaving the app, and
so a bug report can be produced with one click.
"""

from __future__ import annotations

from ..core.result import ActionResult, Change, ChangeKind
from .base import action

_PENDING_REBOOT = r"SYSTEM\CurrentControlSet\Control\Session Manager"


@action(doc="Full hardware, OS and emulator report. Read-only.")
def report(ctx, args) -> ActionResult:
    result = ActionResult(action_id=ctx.action_id)
    hw = ctx.hw

    rows = [
        ("operating system", f"{hw.os_name} {hw.os_version} (build {hw.os_build})"),
        ("architecture", hw.arch),
        ("cpu", hw.cpu_name),
        ("cpu vendor", hw.cpu_vendor or "undetermined"),
        ("cpu generation", hw.cpu_generation if hw.cpu_generation else "undetermined"),
        ("cores / threads", f"{hw.cpu_cores or '?'} / {hw.cpu_threads or '?'}"),
        ("ram", f"{hw.ram_mb} MB" if hw.ram_mb else "unknown"),
        ("discrete gpu", (hw.discrete_gpu.name if hw.discrete_gpu else "none detected")),
        ("gpu vendor", hw.gpu_vendor or "undetermined"),
        ("hypervisor active", _tri(hw.hypervisor_present)),
        ("vbs status", hw.vbs_status if hw.vbs_status is not None else "unknown"),
        ("elevated", "yes" if hw.is_elevated else "NO - HKLM writes will fail"),
        ("registry backend", hw.registry_backend),
    ]
    for label, value in rows:
        result.add(
            Change(kind=ChangeKind.NOTE, target=label, detail=str(value), reversible=False)
        )

    for key, info in ctx.emulators.items():
        state = "installed" if getattr(info, "installed", False) else "not found"
        result.add(
            Change(
                kind=ChangeKind.NOTE,
                target=f"emulator: {info.name}",
                detail=f"{state}" + (f" at {info.root}" if info.root else ""),
                reversible=False,
            )
        )

    for warning in hw.warnings:
        result.warn(warning)

    result.message = "system report generated"
    return result


@action(doc="Check whether a reboot is pending, which silently blocks several of these tweaks from taking effect. Read-only.")
def check_pending_reboot(ctx, args) -> ActionResult:
    result = ActionResult(action_id=ctx.action_id)
    pending: list[str] = []

    hit = ctx.reg.get_value("HKLM", _PENDING_REBOOT, "PendingFileRenameOperations")
    if hit:
        pending.append("PendingFileRenameOperations is set")

    for path, name in (
        (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing", "RebootPending"),
        (r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update", "RebootRequired"),
    ):
        if ctx.reg.key_exists("HKLM", path) and ctx.reg.get_value("HKLM", path, name) is not None:
            pending.append(f"{name} is set")

    result.add(
        Change(
            kind=ChangeKind.NOTE,
            target="pending reboot",
            detail=", ".join(pending) if pending else "none detected",
            reversible=False,
        )
    )
    result.message = (
        "a reboot is pending - some applied tweaks will not take effect until then"
        if pending
        else "no pending reboot detected"
    )
    return result


@action(doc="List which loaded actions are blocked on this machine and why. Read-only.")
def explain_blocks(ctx, args) -> ActionResult:
    """``ctx.state`` carries the precomputed gate results from the app layer."""
    result = ActionResult(action_id=ctx.action_id)
    blocks = (ctx.state or {}).get("blocks", [])
    if not blocks:
        result.message = "every loaded action is available on this machine"
        return result

    for entry in blocks:
        result.add(
            Change(
                kind=ChangeKind.NOTE,
                target=entry.get("id", "?"),
                detail=entry.get("reason", "unavailable"),
                reversible=False,
            )
        )
    result.message = f"{len(blocks)} action(s) unavailable"
    return result


def _tri(value) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


HANDLERS = {
    "audit.report": report,
    "audit.check_pending_reboot": check_pending_reboot,
    "audit.explain_blocks": explain_blocks,
}
