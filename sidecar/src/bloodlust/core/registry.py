"""Registry access with an injectable backend.

Two reasons this is not a thin ``winreg`` wrapper:

1. **Testability.** ``winreg`` does not exist off Windows, so an engine that
   called it directly could not be unit-tested at all. ``MemoryBackend`` makes
   the whole tweak/undo pipeline runnable on any OS, which is how the test
   suite exercises it.
2. **Auditability.** Every read-before-write goes through one funnel, so it is
   impossible to mutate the registry without an undo record being available.

Handlers never touch ``winreg`` directly. They call ``ctx.reg``.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

# Values match the Win32/winreg constants exactly. They are defined here so the
# rest of the package is importable on non-Windows; ``WinregBackend`` asserts
# the correspondence at import time on Windows.
REG_NONE = 0
REG_SZ = 1
REG_EXPAND_SZ = 2
REG_BINARY = 3
REG_DWORD = 4
REG_DWORD_BIG_ENDIAN = 5
REG_LINK = 6
REG_MULTI_SZ = 7
REG_QWORD = 11

_TYPE_NAMES = {
    REG_SZ: "REG_SZ",
    REG_EXPAND_SZ: "REG_EXPAND_SZ",
    REG_BINARY: "REG_BINARY",
    REG_DWORD: "REG_DWORD",
    REG_QWORD: "REG_QWORD",
    REG_MULTI_SZ: "REG_MULTI_SZ",
}

HIVES = {
    "HKLM": "HKEY_LOCAL_MACHINE",
    "HKCU": "HKEY_CURRENT_USER",
    "HKCR": "HKEY_CLASSES_ROOT",
    "HKU": "HKEY_USERS",
}


def type_name(vtype: int) -> str:
    return _TYPE_NAMES.get(vtype, f"REG_TYPE_{vtype}")


class RegistryError(Exception):
    pass


class RegistryBackend(Protocol):
    """Minimal surface the engine needs from a registry."""

    def read(self, hive: str, path: str, name: Optional[str]) -> Optional[tuple[int, Any]]: ...
    def write(self, hive: str, path: str, name: Optional[str], vtype: int, value: Any) -> None: ...
    def delete_value(self, hive: str, path: str, name: Optional[str]) -> None: ...
    def key_exists(self, hive: str, path: str) -> bool: ...
    def delete_key(self, hive: str, path: str) -> None: ...


class MemoryBackend:
    """In-memory registry used by tests and by ``action.preview``."""

    def __init__(self, seed: Optional[dict] = None) -> None:
        # key -> {name -> (vtype, value)}; the "" name is the default value.
        self._keys: dict[tuple[str, str], dict[str, tuple[int, Any]]] = {}
        for (hive, path, name, vtype, value) in seed or []:
            self.write(hive, path, name, vtype, value)

    def _k(self, hive: str, path: str) -> tuple[str, str]:
        return (hive, path.rstrip("\\"))

    def key_exists(self, hive: str, path: str) -> bool:
        return self._k(hive, path) in self._keys

    def read(self, hive: str, path: str, name: Optional[str]):
        bucket = self._keys.get(self._k(hive, path))
        if bucket is None:
            return None
        return bucket.get(name or "")

    def write(self, hive: str, path: str, name: Optional[str], vtype: int, value: Any) -> None:
        k = self._k(hive, path)
        self._keys.setdefault(k, {})[name or ""] = (vtype, value)

    def delete_value(self, hive: str, path: str, name: Optional[str]) -> None:
        k = self._k(hive, path)
        bucket = self._keys.get(k)
        if bucket is not None:
            bucket.pop(name or "", None)
            if not bucket:
                del self._keys[k]

    def delete_key(self, hive: str, path: str) -> None:
        self._keys.pop(self._k(hive, path), None)

    def snapshot(self) -> dict:
        return {f"{h}\\{p}": dict(v) for (h, p), v in self._keys.items()}


class WinregBackend:
    """Real registry, via ``winreg``. Constructed lazily inside the engine."""

    def __init__(self) -> None:
        import winreg  # noqa: F401  (raises ImportError off Windows)

        self._winreg = winreg
        # Guard against the constants ever drifting from Win32.
        assert winreg.REG_SZ == REG_SZ
        assert winreg.REG_DWORD == REG_DWORD
        assert winreg.REG_QWORD == REG_QWORD

    def _hive(self, hive: str):
        try:
            return getattr(self._winreg, HIVES[hive.upper()])
        except KeyError:
            raise RegistryError(f"unknown hive {hive!r}")

    def key_exists(self, hive: str, path: str) -> bool:
        try:
            with self._winreg.OpenKey(self._hive(hive), path):
                return True
        except OSError:
            return False

    def read(self, hive: str, path: str, name: Optional[str]):
        try:
            with self._winreg.OpenKey(self._hive(hive), path) as k:
                value, vtype = self._winreg.QueryValueEx(k, name or "")
                return (vtype, value)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RegistryError(f"read {hive}\\{path}\\{name}: {exc}")

    def write(self, hive: str, path: str, name: Optional[str], vtype: int, value: Any) -> None:
        try:
            with self._winreg.CreateKey(self._hive(hive), path) as k:
                self._winreg.SetValueEx(k, name or "", 0, vtype, value)
        except OSError as exc:
            raise RegistryError(f"write {hive}\\{path}\\{name}: {exc}")

    def delete_value(self, hive: str, path: str, name: Optional[str]) -> None:
        try:
            with self._winreg.OpenKey(self._hive(hive), path, 0, self._winreg.KEY_SET_VALUE) as k:
                self._winreg.DeleteValue(k, name or "")
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RegistryError(f"delete value {hive}\\{path}\\{name}: {exc}")

    def delete_key(self, hive: str, path: str) -> None:
        try:
            self._winreg.DeleteKey(self._hive(hive), path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RegistryError(f"delete key {hive}\\{path}: {exc}")


def default_backend() -> RegistryBackend:
    """Real registry on Windows, in-memory everywhere else.

    Falling back to memory rather than raising means the engine still starts on
    a developer machine or in CI; ``system.info`` reports ``registry: memory``
    so nobody can mistake that for the real thing.
    """
    try:
        return WinregBackend()
    except (ImportError, OSError):
        return MemoryBackend()


__all__ = [
    "REG_SZ",
    "REG_EXPAND_SZ",
    "REG_BINARY",
    "REG_DWORD",
    "REG_QWORD",
    "REG_MULTI_SZ",
    "type_name",
    "RegistryError",
    "RegistryBackend",
    "MemoryBackend",
    "WinregBackend",
    "default_backend",
]
