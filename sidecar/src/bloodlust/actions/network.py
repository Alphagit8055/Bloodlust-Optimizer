"""Network stack actions.

Two rules shape everything here:

1. **Read the current value before changing it.** ``netsh`` reports its state in
   plain text, so each setter parses the existing value first and records the
   exact inverse command in the undo journal.
2. **No command strings.** Every invocation is an argv list. Interface names
   discovered at runtime are passed as discrete list elements, never
   interpolated into a shell string, so a hostile adapter name cannot inject.
"""

from __future__ import annotations

import re

from ..core import registry as reg
from ..core.result import ActionResult, Change, ChangeKind
from .base import action

_TCP_GLOBAL_RE = re.compile(r"Receive Window Auto-Tuning Level\s*:\s*(.+)", re.IGNORECASE)
_DNS_RE = re.compile(r"(?:DNS Servers|Statically Configured DNS Servers)\s*:\s*(.+)", re.IGNORECASE)

_IPV6_DISABLE_KEY = r"SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters"


def _ps(ctx, script: str, *, describe: str = ""):
    """Run a fixed PowerShell snippet. ``script`` is a literal from this file."""
    return ctx.shell.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        timeout=45,
        describe=describe or "query network state",
    )


def _active_adapters(ctx) -> list[dict]:
    """Physical adapters that are Up. Empty list if PowerShell is unavailable."""
    out = _ps(
        ctx,
        "Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | "
        "Select-Object -Property Name,InterfaceGuid,InterfaceDescription | "
        "ConvertTo-Json -Compress",
        describe="enumerate active adapters",
    )
    if not out.ok or not out.stdout:
        return []
    import json

    try:
        parsed = json.loads(out.stdout)
    except ValueError:
        return []
    return [parsed] if isinstance(parsed, dict) else list(parsed)


@action(
    args={
        "level": {
            "type": str,
            "choices": ("normal", "disabled", "highlyrestricted", "restricted"),
            "default": "normal",
        }
    },
    doc="Set the TCP receive-window autotuning level. 'normal' is the Windows default and the right choice for almost everyone.",
)
def set_tcp_autotuning(ctx, args) -> ActionResult:
    level = args.get("level", "normal")
    result = ActionResult(action_id=ctx.action_id)

    show = ctx.shell.run(
        ["netsh", "interface", "tcp", "show", "global"],
        record=False,
        describe="read TCP global settings",
    )
    current = None
    if show.ok:
        m = _TCP_GLOBAL_RE.search(show.stdout)
        if m:
            current = m.group(1).strip().lower().replace(" ", "")

    if current == level:
        result.message = f"autotuning level is already '{level}'"
        return result

    ctx.shell.record_restore(
        ["netsh", "interface", "tcp", "set", "global", f"autotuninglevel={current or 'normal'}"],
        describe=f"restore autotuning level to {current or 'normal'}",
    )

    out = ctx.shell.run(
        ["netsh", "interface", "tcp", "set", "global", f"autotuninglevel={level}"],
        describe=f"set TCP autotuning level to {level}",
    )
    if not out.ok and not out.skipped:
        raise RuntimeError(f"netsh failed: {out.stderr or out.stdout}")

    result.add(
        Change(
            kind=ChangeKind.COMMAND,
            target="netsh interface tcp",
            detail=f"autotuninglevel: {current or 'unknown'} -> {level}",
            reversible=True,
            previous=current,
        )
    )
    if level == "disabled":
        result.warn(
            "disabling autotuning caps throughput on high-latency links. "
            "'normal' is usually faster, not slower"
        )
    return result


@action(
    args={
        "primary": {"type": str, "default": "1.1.1.1"},
        "secondary": {"type": str, "default": "1.0.0.1"},
    },
    doc="Set static DNS on every active physical adapter.",
)
def set_static_dns(ctx, args) -> ActionResult:
    primary = args.get("primary", "1.1.1.1")
    secondary = args.get("secondary", "1.0.0.1")
    for server in (primary, secondary):
        if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", server):
            raise ValueError(f"{server!r} is not a valid IPv4 address")

    result = ActionResult(action_id=ctx.action_id)
    adapters = _active_adapters(ctx)
    if not adapters:
        result.message = "no active physical adapter found; nothing changed"
        return result

    for adapter in adapters:
        name = str(adapter.get("Name") or "")
        if not name:
            continue

        show = ctx.shell.run(
            ["netsh", "interface", "ip", "show", "dns", f"name={name}"],
            record=False,
            describe=f"read DNS for {name}",
        )
        previous = None
        if show.ok:
            m = _DNS_RE.search(show.stdout)
            if m:
                previous = m.group(1).strip()

        if previous:
            ctx.shell.record_restore(
                ["netsh", "interface", "ip", "set", "dns", f"name={name}", "dhcp"],
                describe=f"restore {name} to DHCP-assigned DNS",
            )

        out = ctx.shell.run(
            ["netsh", "interface", "ip", "set", "dns", f"name={name}", "static", primary, "primary"],
            describe=f"set primary DNS on {name}",
        )
        ctx.shell.run(
            ["netsh", "interface", "ip", "add", "dns", f"name={name}", secondary, "index=2"],
            describe=f"set secondary DNS on {name}",
        )
        if not out.ok and not out.skipped:
            result.warn(f"could not set DNS on {name}: {out.stderr or out.stdout}")
            continue

        result.add(
            Change(
                kind=ChangeKind.COMMAND,
                target=f"netsh dns {name}",
                detail=f"DNS -> {primary}, {secondary} (was {previous or 'DHCP'})",
                reversible=True,
                previous=previous,
            )
        )

    result.message = (
        f"DNS set to {primary}/{secondary} on {result.change_count} adapter(s)"
        if result.change_count
        else "no adapter was changed"
    )
    return result


@action(
    args={"ack_frequency": {"type": int, "min": 1, "max": 2, "default": 1}},
    doc="Reduce delayed-ACK latency on every active interface. A small but real win on high-frequency small packets.",
)
def reduce_delayed_ack(ctx, args) -> ActionResult:
    ack = int(args.get("ack_frequency", 1))
    result = ActionResult(action_id=ctx.action_id)

    adapters = _active_adapters(ctx)
    if not adapters:
        result.message = "no active adapter to configure"
        return result

    base = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
    for adapter in adapters:
        guid = adapter.get("InterfaceGuid")
        name = adapter.get("Name", guid)
        if not guid:
            continue
        path = f"{base}\\{guid}"

        c1 = ctx.reg.set_value(
            "HKLM", path, "TcpAckFrequency", reg.REG_DWORD, ack,
            describe=f"{name}: acknowledge every {ack} packet(s)",
        )
        c2 = ctx.reg.set_value(
            "HKLM", path, "TCPNoDelay", reg.REG_DWORD, 1,
            describe=f"{name}: disable Nagle's algorithm",
        )
        for c in (c1, c2):
            if c:
                result.add(c)

    result.message = (
        f"tuned {result.change_count} value(s) across {len(adapters)} adapter(s)"
        if result.change_count
        else "all interfaces already tuned"
    )
    return result


@action(doc="Flush the DNS resolver cache. Harmless and always available.")
def flush_dns(ctx, args) -> ActionResult:
    result = ActionResult(action_id=ctx.action_id)
    out = ctx.shell.run(["ipconfig", "/flushdns"], describe="flush DNS cache", reversible=False)
    if not out.ok and not out.skipped:
        raise RuntimeError(f"ipconfig /flushdns failed: {out.stderr or out.stdout}")
    result.add(
        Change(
            kind=ChangeKind.COMMAND,
            target="ipconfig /flushdns",
            detail="resolver cache cleared",
            reversible=False,
        )
    )
    result.message = "DNS cache flushed"
    return result


@action(
    args={"disabled": {"type": bool, "default": False}},
    doc="Disable IPv6. Only worth doing if the network is genuinely misconfigured; on a healthy network this can make things worse.",
)
def set_ipv6_disabled(ctx, args) -> ActionResult:
    disabled = bool(args.get("disabled", False))
    # 0x20 = prefer IPv4 over IPv6. Full disable (0xFF) is discouraged by
    # Microsoft and breaks parts of Windows, so we do not offer it.
    value = 0x20 if disabled else 0x0
    change = ctx.reg.set_value(
        "HKLM", _IPV6_DISABLE_KEY, "DisabledComponents", reg.REG_DWORD, value,
        describe=f"IPv6 component preference = {hex(value)}",
    )
    result = ActionResult(action_id=ctx.action_id)
    if change:
        result.add(change)
        result.needs_reboot = True
        result.message = "IPv6 preference updated; reboot required"
    else:
        result.message = "IPv6 preference already as requested"
    return result


HANDLERS = {
    "network.set_tcp_autotuning": set_tcp_autotuning,
    "network.set_static_dns": set_static_dns,
    "network.reduce_delayed_ack": reduce_delayed_ack,
    "network.flush_dns": flush_dns,
    "network.set_ipv6_disabled": set_ipv6_disabled,
}
