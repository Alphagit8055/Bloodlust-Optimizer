"""Result objects returned by every action handler.

Every mutation an action performs is described by a :class:`Change`, and every
change carries enough information to be undone. The Rust shell renders these to
the user, so they are part of the public wire contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class ChangeKind(str, Enum):
    """What kind of system mutation happened."""

    REGISTRY = "registry"
    FILE = "file"
    PROCESS = "process"
    SERVICE = "service"
    COMMAND = "command"
    ADB = "adb"
    NOTE = "note"


@dataclass
class Change:
    """One atomic, describable mutation.

    ``reversible`` is a statement of fact, not intent: it is only true when the
    undo journal (or an explicit restore handler) can actually put the system
    back the way it was.
    """

    kind: ChangeKind
    target: str
    detail: str = ""
    reversible: bool = True
    previous: Any = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


@dataclass
class ActionResult:
    """Outcome of running (or previewing) one action."""

    action_id: str
    ok: bool = True
    status: str = "applied"  # applied | skipped | preview | failed | blocked
    message: str = ""
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    needs_elevation: bool = False
    needs_reboot: bool = False
    duration_ms: int = 0

    def add(self, change: Change) -> "ActionResult":
        self.changes.append(change)
        return self

    def warn(self, message: str) -> "ActionResult":
        self.warnings.append(message)
        return self

    @property
    def change_count(self) -> int:
        return len(self.changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "ok": self.ok,
            "status": self.status,
            "message": self.message,
            "changes": [c.to_dict() for c in self.changes],
            "warnings": list(self.warnings),
            "error": self.error,
            "needs_elevation": self.needs_elevation,
            "needs_reboot": self.needs_reboot,
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def failure(cls, action_id: str, error: str) -> "ActionResult":
        return cls(action_id=action_id, ok=False, status="failed", error=error)


@dataclass
class BatchResult:
    """Outcome of running several actions in one go."""

    mode: str
    results: list[ActionResult] = field(default_factory=list)

    @property
    def applied(self) -> int:
        return sum(1 for r in self.results if r.status == "applied")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "total": len(self.results),
            "applied": self.applied,
            "skipped": sum(1 for r in self.results if r.status == "skipped"),
            "failed": self.failed,
            "results": [r.to_dict() for r in self.results],
        }


__all__ = ["Change", "ChangeKind", "ActionResult", "BatchResult"]
