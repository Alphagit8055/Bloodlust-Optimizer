"""Shared helpers for action handlers.

The ``action`` decorator attaches the argument schema the executor validates
against. Declaring the schema next to the handler — rather than in the catalog —
is what stops a remote catalog from widening an argument's accepted range.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from ..core import registry as reg
from ..core.mutate import Shell
from ..core.result import Change, ChangeKind

# --- power plan GUIDs (fixed by Windows, not by us) -------------------------
SCHEME_BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"
SCHEME_HIGH_PERF = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
SCHEME_ULTIMATE = "e9a42b02-d5df-448d-aa66-8f4416fc4484"

# --- power setting GUIDs ----------------------------------------------------
SUB_PROCESSOR = "54533251-82be-4824-96c1-47b60b740d00"
SET_PROCTHROTTLEMIN = "893dee8e-2bef-41e0-89c6-b55d0929964c"
SET_PROCTHROTTLEMAX = "bc5038f7-23e0-4960-96da-33abaf5935ec"
SET_CPMINCORES = "0cc5b647-c1df-4637-891a-dec35c318583"
SET_PERFBOOSTMODE = "be337238-0d82-4146-a960-4f3749d470c7"

SUB_WIRELESS = "19cbb8fa-5279-450e-9fac-8a3d5fedd0c1"
SET_WIRELESS_POWER = "12bbebe6-58d6-4636-95bb-3217ef867c1a"

SUB_USB = "2a737441-1930-4402-8d77-b2bebba308a3"
SET_USB_SUSPEND = "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"


def action(
    *,
    args: Optional[dict[str, dict]] = None,
    doc: str = "",
) -> Callable:
    """Attach an argument schema to a handler function."""

    def decorate(fn: Callable) -> Callable:
        fn.arg_schema = args or {}
        if doc:
            fn.__doc__ = doc
        return fn

    return decorate


class PowerCfg:
    """``powercfg`` wrapper that records undoable state.

    Every write reads the current value first and stores it, so the undo journal
    can put the power scheme back exactly as it was — including the AC/DC split,
    which is where the on-battery defaults that cause latency spikes actually
    live.
    """

    def __init__(self, shell: Shell, journal) -> None:
        self.shell = shell
        self.journal = journal

    # ------------------------------------------------------------------ reads

    def active_scheme(self) -> Optional[str]:
        out = self.shell.run(
            ["powercfg", "/getactivescheme"], record=False, describe="read active power scheme"
        )
        if not out.ok:
            return None
        m = re.search(r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})", out.stdout)
        return m.group(1).lower() if m else None

    def scheme_by_name(self, needle: str) -> Optional[str]:
        out = self.shell.run(["powercfg", "/list"], record=False, describe="list power schemes")
        if not out.ok:
            return None
        for line in out.stdout.splitlines():
            if needle.lower() in line.lower():
                m = re.search(
                    r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})", line
                )
                if m:
                    return m.group(1).lower()
        return None

    def index(self, subgroup: str, setting: str) -> tuple[Optional[int], Optional[int]]:
        """Current (AC, DC) index for a power setting, parsed from ``/q``."""
        out = self.shell.run(
            ["powercfg", "/q", "SCHEME_CURRENT", subgroup, setting],
            record=False,
            describe="read power setting index",
        )
        if not out.ok:
            return None, None
        ac = _parse_index(out.stdout, "AC")
        dc = _parse_index(out.stdout, "DC")
        return ac, dc

    # ----------------------------------------------------------------- writes

    def set_active(self, guid: str, *, describe: str = "") -> Optional[Change]:
        current = self.active_scheme()
        if current and current == guid.lower():
            return None

        if current:
            self.journal.entries.append(
                {
                    "kind": "powercfg_active",
                    "prev": current,
                    "describe": describe or "restore previous power scheme",
                }
            )

        out = self.shell.run(
            ["powercfg", "/setactive", guid],
            describe=describe or f"activate power scheme {guid}",
        )
        if not out.ok and not out.skipped:
            raise RuntimeError(f"powercfg /setactive failed: {out.stderr or out.stdout}")

        return Change(
            kind=ChangeKind.COMMAND,
            target="powercfg /setactive",
            detail=(describe or "activated") + f" ({guid})",
            reversible=True,
            previous=current,
        )

    def duplicate_scheme(self, guid: str) -> Optional[str]:
        """Create a scheme from a template. Returns the new GUID."""
        out = self.shell.run(
            ["powercfg", "/duplicatescheme", guid],
            describe="duplicate power scheme template",
            reversible=False,
        )
        if not out.ok:
            return None
        m = re.search(r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})", out.stdout)
        return m.group(1).lower() if m else None

    def set_index(
        self,
        subgroup: str,
        setting: str,
        *,
        ac: Optional[int] = None,
        dc: Optional[int] = None,
        describe: str = "",
    ) -> list[Change]:
        """Set AC and/or DC index, journaling the previous pair."""
        prev_ac, prev_dc = self.index(subgroup, setting)
        changes: list[Change] = []

        want = {"AC": (ac, prev_ac), "DC": (dc, prev_dc)}
        pending = {k: v for k, v in want.items() if v[0] is not None and v[0] != v[1]}
        if not pending:
            return changes

        self.journal.entries.append(
            {
                "kind": "powercfg_index",
                "subgroup": subgroup,
                "setting": setting,
                "prev_ac": prev_ac,
                "prev_dc": prev_dc,
                "describe": describe,
            }
        )

        for scope, (value, previous) in pending.items():
            flag = "/setacvalueindex" if scope == "AC" else "/setdcvalueindex"
            out = self.shell.run(
                ["powercfg", flag, "SCHEME_CURRENT", subgroup, setting, str(value)],
                describe=f"{describe or 'set power index'} ({scope})",
            )
            if not out.ok and not out.skipped:
                raise RuntimeError(f"powercfg {flag} failed: {out.stderr or out.stdout}")

            changes.append(
                Change(
                    kind=ChangeKind.COMMAND,
                    target=f"powercfg {flag}",
                    detail=f"{describe or setting}: {scope} {previous} -> {value}",
                    reversible=True,
                    previous=previous,
                )
            )

        # Value indexes only take effect once the scheme is re-activated.
        act = self.shell.run(
            ["powercfg", "/setactive", "SCHEME_CURRENT"],
            describe="re-apply active power scheme",
        )
        if not act.ok and not act.skipped:
            raise RuntimeError(f"powercfg /setactive SCHEME_CURRENT failed: {act.stderr}")

        return changes


_HEX = re.compile(r"0x([0-9a-fA-F]+)")


def _parse_index(output: str, scope: str) -> Optional[int]:
    """Pull the hex index out of a ``powercfg /q`` block.

    The label text is localised but the ``0x...`` value is not, so the marker is
    matched loosely and the value is read as the first hex number on the line.
    Both of the forms Windows has emitted are accepted:

        Current AC Power Setting Index: 0x00000005
        Current AC Power Setting Index: (0x00000005)
    """
    needle = f"CURRENT {scope} POWER SETTING INDEX"
    for line in output.splitlines():
        collapsed = " ".join(line.upper().split())
        if needle in collapsed:
            m = _HEX.search(line)
            if m:
                return int(m.group(1), 16)
    return None


def set_dword(
    ctx,
    hive: str,
    path: str,
    name: str,
    value: int,
    *,
    describe: str = "",
) -> Optional[Change]:
    return ctx.reg.set_value(hive, path, name, reg.REG_DWORD, int(value), describe=describe)


def adb_shell(ctx, device_args: list[str], command: list[str], *, describe: str = ""):
    """Run ``adb shell`` against the emulator guest."""
    adb = None
    for key in ("gameloop", "mumu", "ldplayer"):
        info = ctx.emulators.get(key)
        if info and getattr(info, "adb", None):
            adb = info.adb
            break
    if adb is None:
        raise RuntimeError("no adb executable found for any detected emulator")

    argv = [adb] + device_args + ["shell"] + command
    return ctx.shell.run(argv, timeout=30, describe=describe or " ".join(command))


__all__ = [
    "action",
    "PowerCfg",
    "set_dword",
    "adb_shell",
    "SCHEME_BALANCED",
    "SCHEME_HIGH_PERF",
    "SCHEME_ULTIMATE",
    "SUB_PROCESSOR",
    "SET_PROCTHROTTLEMIN",
    "SET_PROCTHROTTLEMAX",
    "SET_CPMINCORES",
    "SET_PERFBOOSTMODE",
    "SUB_WIRELESS",
    "SET_WIRELESS_POWER",
    "SUB_USB",
    "SET_USB_SUSPEND",
]
