"""The mutation funnel handlers actually use.

``RegistryFacade`` is the only sanctioned way for an action handler to change
the registry. It reads before it writes, records the prior state in the undo
journal, and short-circuits when the value is already correct so re-running an
action is cheap and does not churn the journal.

In preview mode it computes exactly the same changes but applies none of them —
which is what makes ``action.preview`` trustworthy rather than a parallel
implementation that can drift from ``action.run``.
"""

from __future__ import annotations

from typing import Any, Optional

from . import registry as reg
from .backup import UndoJournal
from .logging import get
from .result import Change, ChangeKind


class RegistryFacade:
    def __init__(
        self,
        backend: reg.RegistryBackend,
        journal: UndoJournal,
        preview: bool = False,
    ) -> None:
        self._b = backend
        self._j = journal
        self.preview = preview
        # Keep the two flags from ever disagreeing: a facade in preview mode
        # marks its journal as preview too, so commit() cannot persist it.
        if preview:
            journal.preview = True
        self.is_real = not isinstance(backend, reg.MemoryBackend)

    def get_value(self, hive: str, path: str, name: Optional[str]) -> Optional[tuple[int, Any]]:
        return self._b.read(hive, path, name)

    def key_exists(self, hive: str, path: str) -> bool:
        return self._b.key_exists(hive, path)

    def set_value(
        self,
        hive: str,
        path: str,
        name: Optional[str],
        vtype: int,
        value: Any,
        *,
        describe: str = "",
    ) -> Optional[Change]:
        """Write a value, journaling the previous one. Returns a Change or None."""
        prev = self._b.read(hive, path, name)

        # Idempotence: nothing to do, so nothing to journal either.
        if prev is not None and prev[0] == vtype and prev[1] == value:
            return None

        self._j.record_value(hive, path, name, prev)
        if not self.preview:
            self._b.write(hive, path, name, vtype, value)

        if prev is None:
            detail = f"created {reg.type_name(vtype)}"
        else:
            detail = f"{_short(prev[1])} -> {_short(value)}"
        if describe:
            detail = f"{describe}: {detail}"

        return Change(
            kind=ChangeKind.REGISTRY,
            target=f"{hive}\\{path}\\{name or '(default)'}",
            detail=detail,
            reversible=True,
            # The actual previous value, not a rendering of it: callers use
            # this programmatically. ``detail`` carries the human-readable form.
            previous=None if prev is None else prev[1],
        )

    def delete_value(
        self, hive: str, path: str, name: Optional[str], *, describe: str = ""
    ) -> Optional[Change]:
        prev = self._b.read(hive, path, name)
        if prev is None:
            return None

        self._j.record_value(hive, path, name, prev)
        if not self.preview:
            self._b.delete_value(hive, path, name)

        return Change(
            kind=ChangeKind.REGISTRY,
            target=f"{hive}\\{path}\\{name or '(default)'}",
            detail=describe or f"deleted {reg.type_name(prev[0])}",
            reversible=True,
            previous=prev[1],
        )

    def create_key(self, hive: str, path: str, *, describe: str = "") -> Optional[Change]:
        if self._b.key_exists(hive, path):
            return None
        self._j.record_key_created(hive, path)
        if not self.preview:
            self._b.write(hive, path, "", reg.REG_SZ, "")
        return Change(
            kind=ChangeKind.REGISTRY,
            target=f"{hive}\\{path}",
            detail=describe or "created key",
            reversible=True,
        )


class Shell:
    """Command runner.

    Deliberately narrow: argv lists only, never ``shell=True``. Handlers cannot
    build a command string, which removes the entire class of injection bug that
    a catalog-driven app would otherwise have to defend against.
    """

    def __init__(self, journal: UndoJournal, preview: bool = False) -> None:
        self._j = journal
        self.preview = preview

    def run(
        self,
        argv: list[str],
        *,
        timeout: int = 60,
        describe: str = "",
        reversible: bool = True,
        record: bool = True,
    ) -> "CommandOutcome":
        import subprocess

        label = describe or " ".join(argv[:3])
        get().debug("shell: %s", argv)

        if self.preview:
            return CommandOutcome(argv=argv, returncode=None, stdout="", stderr="", skipped=True)

        try:
            proc = subprocess.run(  # noqa: S603 - argv list, no shell
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            outcome = CommandOutcome(
                argv=argv,
                returncode=proc.returncode,
                stdout=(proc.stdout or "").strip(),
                stderr=(proc.stderr or "").strip(),
            )
        except FileNotFoundError:
            outcome = CommandOutcome(
                argv=argv, returncode=127, stdout="", stderr=f"not found: {argv[0]}"
            )
        except subprocess.TimeoutExpired:
            outcome = CommandOutcome(
                argv=argv, returncode=None, stdout="", stderr=f"timed out after {timeout}s"
            )

        if record:
            self._j.entries.append(
                {"kind": "command", "argv": argv, "describe": label, "reversible": reversible}
            )
        return outcome

    def record_restore(self, argv: list[str], *, describe: str = "") -> None:
        """Register the exact command that puts this setting back."""
        self._j.entries.append(
            {"kind": "command_restore", "restore_argv": argv, "describe": describe}
        )

    def change_for(
        self, argv: list[str], describe: str = "", *, reversible: bool = True
    ) -> Change:
        return Change(
            kind=ChangeKind.COMMAND,
            target=" ".join(argv[:2]),
            detail=describe or " ".join(argv),
            reversible=reversible,
        )


class CommandOutcome:
    __slots__ = ("argv", "returncode", "stdout", "stderr", "skipped")

    def __init__(
        self,
        argv: list[str],
        returncode: Optional[int],
        stdout: str,
        stderr: str,
        skipped: bool = False,
    ) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.skipped = skipped

    @property
    def ok(self) -> bool:
        return self.skipped or self.returncode == 0

    def __repr__(self) -> str:
        return f"<CommandOutcome rc={self.returncode} argv={self.argv[:2]}>"


def _short(value: Any, limit: int = 64) -> str:
    """Compact rendering for change details shown in the UI."""
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


__all__ = ["RegistryFacade", "Shell", "CommandOutcome"]
