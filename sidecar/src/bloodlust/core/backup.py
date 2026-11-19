"""The undo journal.

This is the safety net for the whole application. Before any mutation, the
engine records the prior state here; ``undo`` replays those records in reverse
order. An action that cannot describe its undo is refused rather than applied —
see ``core.executor``.

Journals are one JSON file per action id under ``<data>/undo/``. Re-running an
action that already has a journal **appends to the original** rather than
overwriting it, so the journal always describes how to get back to the state
before the *first* application. Without that, apply → apply → undo would leave
the system stuck at the first run's values instead of the true original.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any, Optional

from . import paths
from . import registry as reg
from .logging import get

JOURNAL_VERSION = 1


def _encode(vtype: int, value: Any) -> Any:
    """Make a registry value JSON-safe."""
    if vtype == reg.REG_BINARY and isinstance(value, (bytes, bytearray)):
        return {"__binary__": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def _decode(vtype: int, raw: Any) -> Any:
    if vtype == reg.REG_BINARY and isinstance(raw, dict) and "__binary__" in raw:
        return base64.b64decode(raw["__binary__"])
    return raw


class UndoJournal:
    """Collects prior-state records for one action and can replay them."""

    def __init__(
        self,
        action_id: str,
        backend: reg.RegistryBackend,
        preview: bool = False,
    ) -> None:
        self.action_id = action_id
        self._backend = backend
        self.preview = preview
        self.entries: list[dict] = []
        self.created_at = time.time()
        self.loaded = False

    # ------------------------------------------------------------------ record

    def record_value(
        self,
        hive: str,
        path: str,
        name: Optional[str],
        prev: Optional[tuple[int, Any]],
    ) -> None:
        """Call *before* writing. ``prev`` is ``None`` if the value was absent."""
        existed = prev is not None
        self.entries.append(
            {
                "kind": "value",
                "hive": hive,
                "path": path,
                "name": name,
                "existed": existed,
                "prev_type": prev[0] if existed else None,
                "prev_value": _encode(prev[0], prev[1]) if existed else None,
            }
        )

    def record_key_created(self, hive: str, path: str) -> None:
        self.entries.append({"kind": "key", "hive": hive, "path": path})

    @property
    def empty(self) -> bool:
        return not self.entries

    # ------------------------------------------------------------------ persist

    def _path(self) -> Path:
        return paths.undo_dir() / f"{self.action_id}.json"

    def commit(self) -> Optional[Path]:
        """Merge into any existing journal and write it out.

        Returns the path, or ``None`` when there was nothing to record (an
        action that changed no state should not leave a journal behind).
        """
        if self.empty:
            return None

        # A preview computes what *would* change; persisting that as undo state
        # would make the action look applied in the UI when nothing happened.
        # Enforced here so no caller can get it wrong by forgetting.
        if self.preview:
            get().debug("preview journal for %s discarded (%d entries)", self.action_id, len(self.entries))
            return None

        target = self._path()
        merged = list(self.entries)

        if target.exists():
            try:
                prior = json.loads(target.read_text(encoding="utf-8"))
                # Oldest first: the pre-existing records describe a state that
                # predates ours, so they must be replayed last.
                merged = list(prior.get("entries", [])) + merged
                self.created_at = prior.get("created_at", self.created_at)
                self.loaded = True
            except (OSError, ValueError) as exc:
                get().warning("could not merge journal %s: %s", target, exc)
                get().warning("overwriting with current state only")

        payload = {
            "version": JOURNAL_VERSION,
            "action_id": self.action_id,
            "created_at": self.created_at,
            "updated_at": time.time(),
            "entries": merged,
        }
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(target)  # atomic on both NTFS and POSIX
        return target

    @classmethod
    def load(cls, action_id: str, backend: reg.RegistryBackend) -> Optional["UndoJournal"]:
        target = paths.undo_dir() / f"{action_id}.json"
        if not target.exists():
            return None
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            get().error("corrupt journal %s: %s", target, exc)
            return None

        j = cls(action_id, backend)
        j.entries = list(payload.get("entries", []))
        j.created_at = payload.get("created_at", time.time())
        j.loaded = True
        return j

    @classmethod
    def list_available(cls) -> list[dict]:
        out: list[dict] = []
        d = paths.undo_dir()
        if not d.exists():
            return out
        for f in sorted(d.glob("*.json")):
            try:
                payload = json.loads(f.read_text(encoding="utf-8"))
                out.append(
                    {
                        "action_id": payload.get("action_id", f.stem),
                        "entries": len(payload.get("entries", [])),
                        "updated_at": payload.get("updated_at"),
                        "path": str(f),
                    }
                )
            except (OSError, ValueError):
                out.append({"action_id": f.stem, "entries": 0, "error": "unreadable"})
        return out

    # ------------------------------------------------------------------- replay

    def replay(self, shell: Optional[Any] = None) -> list[dict]:
        """Restore every recorded value, newest first. Returns change dicts.

        ``shell`` is required to undo ``powercfg`` entries; registry-only
        journals can be replayed without it.
        """
        changes: list[dict] = []
        # Reverse so a value written twice by one action lands on its original.
        for entry in reversed(self.entries):
            if entry["kind"] == "powercfg_active":
                if shell is None:
                    continue
                shell.run(
                    ["powercfg", "/setactive", entry["prev"]],
                    describe=entry.get("describe") or "restore power scheme",
                    record=False,
                )
                changes.append(
                    {
                        "kind": "command",
                        "target": "powercfg /setactive",
                        "detail": f"restored scheme {entry['prev']}",
                        "reversible": False,
                    }
                )
                continue

            if entry["kind"] == "powercfg_index":
                if shell is None:
                    continue
                sub, setting = entry["subgroup"], entry["setting"]
                for scope, key in (("ac", "prev_ac"), ("dc", "prev_dc")):
                    value = entry.get(key)
                    if value is None:
                        continue
                    flag = f"/set{scope}valueindex"
                    shell.run(
                        [
                            "powercfg",
                            flag,
                            "SCHEME_CURRENT",
                            sub,
                            setting,
                            str(value),
                        ],
                        describe=f"restore {setting} ({scope.upper()})",
                        record=False,
                    )
                shell.run(
                    ["powercfg", "/setactive", "SCHEME_CURRENT"],
                    describe="re-apply power scheme",
                    record=False,
                )
                changes.append(
                    {
                        "kind": "command",
                        "target": "powercfg /setvalueindex",
                        "detail": f"restored {setting} to AC={entry.get('prev_ac')} DC={entry.get('prev_dc')}",
                        "reversible": False,
                    }
                )
                continue

            if entry["kind"] == "command_restore":
                if shell is None:
                    continue
                out = shell.run(
                    entry["restore_argv"],
                    describe=entry.get("describe") or "restore previous setting",
                    record=False,
                )
                changes.append(
                    {
                        "kind": "command",
                        "target": " ".join(entry["restore_argv"][:2]),
                        "detail": f"restored: {entry.get('describe') or ''} "
                        f"(rc={out.returncode})",
                        "reversible": False,
                    }
                )
                continue

            if entry["kind"] == "command":
                # Recorded for the audit trail; there is no generic inverse.
                continue

            if entry["kind"] == "key":
                self._backend.delete_key(entry["hive"], entry["path"])
                changes.append(
                    {
                        "kind": "registry",
                        "target": f"{entry['hive']}\\{entry['path']}",
                        "detail": "removed key created by this action",
                        "reversible": False,
                    }
                )
                continue

            name = entry.get("name")
            if entry.get("existed"):
                vtype = entry["prev_type"]
                self._backend.write(
                    entry["hive"], entry["path"], name, vtype, _decode(vtype, entry["prev_value"])
                )
                detail = f"restored to {reg.type_name(vtype)}"
            else:
                self._backend.delete_value(entry["hive"], entry["path"], name)
                detail = "deleted (did not exist before)"

            changes.append(
                {
                    "kind": "registry",
                    "target": f"{entry['hive']}\\{entry['path']}\\{name or '(default)'}",
                    "detail": detail,
                    "reversible": False,
                }
            )
        return changes

    def discard(self) -> None:
        try:
            self._path().unlink(missing_ok=True)
        except OSError as exc:
            get().warning("could not remove journal for %s: %s", self.action_id, exc)


__all__ = ["UndoJournal", "JOURNAL_VERSION"]
