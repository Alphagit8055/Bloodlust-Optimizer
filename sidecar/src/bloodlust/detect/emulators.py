"""Emulator detection and path resolution.

Detection is ordered cheapest-first: registry uninstall entries, then well-known
install roots, then the ADB transport. A miss is not an error — it just means
the GameLoop tab renders in its "not installed" state instead of guessing a
path and writing to the wrong place.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from ..core import registry as reg
from ..core.logging import get

# GameLoop (Tencent Gaming Buddy). These are the stock install roots; a custom
# install is found via the uninstall key instead.
_GAMELOOP_ROOTS = [
    r"C:\Program Files\TxGameAssistant",
    r"C:\Program Files (x86)\TxGameAssistant",
    r"D:\Program Files\TxGameAssistant",
    r"D:\Program Files (x86)\TxGameAssistant",
]

_MUMU_ROOTS = [
    r"C:\Program Files\Netease\MuMu Player 12",
    r"C:\Program Files\Netease\MuMuPlayer-12.0",
    r"D:\Program Files\Netease\MuMu Player 12",
]

_LDPLAYER_ROOTS = [
    r"C:\LDPlayer\LDPlayer9",
    r"C:\LDPlayer\LDPlayer64",
    r"D:\LDPlayer\LDPlayer9",
]

_UNINSTALL_KEYS = [
    (
        "HKLM",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    ),
    (
        "HKLM",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ),
    (
        "HKCU",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    ),
]


@dataclass
class EmulatorInfo:
    key: str                       # gameloop | mumu | ldplayer
    name: str
    installed: bool = False
    root: Optional[str] = None
    version: Optional[str] = None
    exe: Optional[str] = None
    adb: Optional[str] = None
    source: Optional[str] = None   # how it was found: registry | known-path | env
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _registry_install_roots(backend: reg.RegistryBackend, needle: str) -> list[tuple[str, str]]:
    """Scan uninstall entries for a display name containing ``needle``."""
    hits: list[tuple[str, str]] = []
    for hive, base in _UNINSTALL_KEYS:
        # Probe numbered and GUID-style subkeys is impractical without a key
        # enumerator, so rely on the well-known flat names emulators use.
        for subkey in _candidate_subkeys(needle):
            full = f"{base}\\{subkey}"
            loc = backend.read(hive, full, "InstallLocation")
            if loc and str(loc[1]).strip():
                hits.append((str(loc[1]).strip().rstrip("\\"), "registry"))
                continue
            cmd = backend.read(hive, full, "UninstallString")
            if cmd:
                extracted = _root_from_command(str(cmd[1]))
                if extracted:
                    hits.append((extracted, "registry"))
    return hits


def _candidate_subkeys(needle: str) -> list[str]:
    return [
        needle,
        f"{needle}_is1",
        f"{needle} 12",
        f"{needle}12",
    ]


def _root_from_command(command: str) -> Optional[str]:
    """Pull an install root out of an UninstallString."""
    match = re.search(r'"([^"]+\\[^"\\]+\.exe)"', command)
    target = match.group(1) if match else command.split(" /")[0].strip('"')
    if not target:
        return None
    try:
        return str(Path(target).parent)
    except (OSError, ValueError):
        return None


def _first_existing(paths: list[str]) -> Optional[str]:
    for p in paths:
        if Path(p).is_dir():
            return p
    return None


def _find_gameloop(backend: reg.RegistryBackend) -> EmulatorInfo:
    info = EmulatorInfo(key="gameloop", name="GameLoop")

    root = None
    env_root = os.environ.get("GAMELOOP_ROOT")
    if env_root and Path(env_root).is_dir():
        root, info.source = env_root, "env"
    else:
        for candidate, source in _registry_install_roots(backend, "GameLoop"):
            if Path(candidate).is_dir():
                root, info.source = candidate, source
                break
        if root is None:
            root = _first_existing(_GAMELOOP_ROOTS)
            if root:
                info.source = "known-path"

    if not root:
        return info

    info.installed = True
    info.root = root

    # GameLoop splits its payload: the launcher lives at the root, the actual
    # Android runtime under MobileGamePCShared / UI.
    candidates = [
        Path(root) / "MobileGamePCShared" / "AndroidEmulator.exe",
        Path(root) / "UI" / "AndroidEmulator.exe",
        Path(root) / "GameLoop.exe",
    ]
    for c in candidates:
        if c.is_file():
            info.exe = str(c)
            break

    adb = _find_adb([
        Path(root) / "MobileGamePCShared" / "adb.exe",
        Path(root) / "adb.exe",
    ])
    info.adb = adb
    return info


def _find_adb(candidates: list[Path]) -> Optional[str]:
    for c in candidates:
        if c.is_file():
            return str(c)
    on_path = shutil.which("adb")
    return on_path


def _find_generic(key: str, name: str, roots: list[str], exe_names: list[str]) -> EmulatorInfo:
    info = EmulatorInfo(key=key, name=name)
    env = os.environ.get(f"{key.upper()}_ROOT")
    root = env if env and Path(env).is_dir() else _first_existing(roots)
    if not root:
        return info

    info.installed = True
    info.root = root
    info.source = "env" if env else "known-path"
    for exe in exe_names:
        c = Path(root) / "shell" / exe
        if not c.is_file():
            c = Path(root) / exe
        if c.is_file():
            info.exe = str(c)
            break
    info.adb = _find_adb([Path(root) / "adb.exe", Path(root) / "shell" / "adb.exe"])
    return info


def detect_all(backend: Optional[reg.RegistryBackend] = None) -> dict[str, EmulatorInfo]:
    backend = backend or reg.default_backend()
    out: dict[str, EmulatorInfo] = {}

    try:
        out["gameloop"] = _find_gameloop(backend)
    except Exception as exc:  # noqa: BLE001 - a bad uninstall key must not abort
        get().warning("GameLoop detection failed: %s", exc)
        out["gameloop"] = EmulatorInfo(key="gameloop", name="GameLoop")

    out["mumu"] = _find_generic(
        "mumu", "MuMu Player", _MUMU_ROOTS, ["MuMuPlayer.exe", "MuMuVMMHeadless.exe"]
    )
    out["ldplayer"] = _find_generic(
        "ldplayer", "LDPlayer", _LDPLAYER_ROOTS, ["dnplayer.exe", "LdVBoxHeadless.exe"]
    )

    # Host adb.exe is shared across all three, so record it once for the UI.
    shared = shutil.which("adb")
    for info in out.values():
        if info.adb is None:
            info.adb = shared

    return out


def any_installed(emulators: dict[str, EmulatorInfo]) -> bool:
    return any(e.installed for e in emulators.values())


__all__ = ["EmulatorInfo", "detect_all", "any_installed"]
