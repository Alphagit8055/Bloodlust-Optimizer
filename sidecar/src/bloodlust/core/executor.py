"""Runs actions, applies gates, manages the undo lifecycle.

Ordering is deliberate and matters for safety:

    resolve handler -> evaluate gate -> check risk ceiling -> build context
    -> run handler -> commit journal

The gate is checked *before* the handler is even constructed, so a blocked
action cannot execute a single side effect while discovering it is blocked.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from . import registry as reg
from . import risk as risk_mod
from .backup import UndoJournal
from .catalog import RISKS, RISK_ORDER, ActionSpec, Catalog
from .context import ActionContext
from .logging import get
from .mutate import RegistryFacade, Shell
from .result import ActionResult, BatchResult, Change, ChangeKind
from ..rpc import errors

Handler = Callable[[ActionContext, dict], Any]


class Executor:
    def __init__(
        self,
        catalog: Catalog,
        handlers: dict[str, Handler],
        hw,
        emulators: dict,
        *,
        risk_ceiling: str = "safe",
        backend: Optional[reg.RegistryBackend] = None,
        shell_factory: Optional[Callable[..., Shell]] = None,
    ) -> None:
        if risk_ceiling not in RISKS:
            raise ValueError(f"risk_ceiling must be one of {RISKS}")
        self.catalog = catalog
        self.handlers = handlers
        self.hw = hw
        self.emulators = emulators
        self.risk_ceiling = risk_ceiling
        self.backend = backend or reg.default_backend()
        # Injectable so command-driven handlers (powercfg, netsh, adb) can be
        # exercised without the tools being installed.
        self.shell_factory = shell_factory or (lambda j, preview: Shell(j, preview=preview))
        self.log = get()

    # ------------------------------------------------------------- resolution

    def _resolve(self, action_id: str) -> tuple[ActionSpec, Handler]:
        spec = self.catalog.get(action_id)
        if spec is None:
            raise errors.RpcError(
                errors.ACTION_NOT_FOUND,
                f"no action {action_id!r} in the loaded catalog",
                {"catalog_version": self.catalog.provenance.catalog_version},
            )
        handler = self.handlers.get(spec.handler)
        if handler is None:
            # The allowlist is checked when the catalog loads, so reaching this
            # means the catalog and the handler table disagree at runtime.
            raise errors.RpcError(
                errors.INTERNAL_ERROR,
                f"action {action_id!r} names handler {spec.handler!r}, which is not loaded",
            )
        return spec, handler

    def _build_context(self, action_id: str, preview: bool):
        journal = UndoJournal(action_id, self.backend, preview=preview)
        return ActionContext(
            reg=RegistryFacade(self.backend, journal, preview=preview),
            shell=self.shell_factory(journal, preview),
            hw=self.hw,
            emulators=self.emulators,
            log=self.log,
            preview=preview,
            action_id=action_id,
            backend=self.backend,
            journal=journal,
            # Handlers that report on gating (audit.explain_blocks) need this.
            state={"blocks": self._all_blocks()},
        )

    def _all_blocks(self) -> list[dict]:
        out: list[dict] = []
        for spec in self.catalog.actions.values():
            gate = risk_mod.evaluate(spec, self.hw, self.emulators)
            if not gate.available:
                out.append({"id": spec.id, "label": spec.label, "reason": gate.reason})
        return out

    # ------------------------------------------------------------------ gating

    def gate_for(self, action_id: str) -> risk_mod.Gate:
        spec = self.catalog.get(action_id)
        if spec is None:
            return risk_mod.Gate(False, [f"unknown action {action_id!r}"], [])
        return risk_mod.evaluate(spec, self.hw, self.emulators)

    def describe(self, action_id: str) -> dict:
        spec, handler = self._resolve(action_id)
        gate = self.gate_for(action_id)
        ceiling_ok = risk_mod.risk_ceiling_ok(spec.risk, self.risk_ceiling, RISK_ORDER)
        schema = getattr(handler, "arg_schema", {}) or {}
        return {
            **spec.to_dict(),
            "gate": gate.to_dict(),
            "risk_ceiling_ok": ceiling_ok,
            "risk_ceiling": self.risk_ceiling,
            "has_undo": any(
                j["action_id"] == action_id for j in UndoJournal.list_available()
            ),
            "arg_schema": schema,
        }

    # ------------------------------------------------------------------ run

    def run(self, action_id: str, args: Optional[dict] = None, *, preview: bool = False) -> dict:
        spec, handler = self._resolve(action_id)

        gate = self.gate_for(action_id)
        if not gate.available:
            raise errors.RpcError(
                errors.ACTION_BLOCKED,
                f"{spec.label} is not available on this machine",
                gate.to_dict(),
            )

        if not preview and not risk_mod.risk_ceiling_ok(spec.risk, self.risk_ceiling, RISK_ORDER):
            raise errors.RpcError(
                errors.ACTION_BLOCKED,
                f"{spec.label} is risk={spec.risk}, above your ceiling of {self.risk_ceiling}",
                {"risk": spec.risk, "ceiling": self.risk_ceiling},
            )

        merged = dict(spec.args or {})
        merged.update(args or {})
        _check_args(action_id, handler, merged)

        ctx = self._build_context(action_id, preview=preview)
        started = time.perf_counter()

        try:
            outcome = handler(ctx, merged)
        except errors.RpcError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.log.exception("action %s failed", action_id)
            raise errors.RpcError(errors.ACTION_FAILED, f"{spec.label} failed: {exc}")

        result = _normalise(action_id, outcome, preview=preview)
        result.duration_ms = int((time.perf_counter() - started) * 1000)

        if not preview and result.ok and not result.changes:
            result.status = "skipped"
            result.message = result.message or "already in the desired state"

        # Commit only when something real changed. An empty journal would make
        # the action look undoable in the UI when there is nothing to undo.
        if not preview and result.ok:
            path = ctx.journal.commit()
            if path:
                result.message = result.message or f"{len(ctx.journal.entries)} change(s) recorded for undo"
            self.log.info(
                "action %s -> %s (%d changes)", action_id, result.status, len(result.changes)
            )

        if spec.needs_reboot and result.status == "applied":
            result.needs_reboot = True

        return result.to_dict()

    def preview(self, action_id: str, args: Optional[dict] = None) -> dict:
        return self.run(action_id, args, preview=True)

    # ------------------------------------------------------------------ batch

    def run_batch(
        self, mode: str, ids: Optional[list[str]] = None, *, preview: bool = False
    ) -> dict:
        ceiling = {"safe": "safe", "aggressive": "aggressive"}.get(mode)
        if ceiling is None:
            raise errors.RpcError(
                errors.INVALID_PARAMS, "mode must be 'safe' or 'aggressive'"
            )

        if ids:
            order = [i for i in ids if self.catalog.get(i)]
            unknown = [i for i in ids if not self.catalog.get(i)]
            if unknown:
                raise errors.RpcError(
                    errors.ACTION_NOT_FOUND, "unknown action id(s)", {"ids": unknown}
                )
        else:
            order = [
                spec.id
                for spec in self.catalog.actions.values()
                if spec.default_enabled and RISK_ORDER[spec.risk] <= RISK_ORDER[ceiling]
            ]
            order.sort(key=lambda i: (RISK_ORDER[self.catalog.actions[i].risk], i))

        batch = BatchResult(mode=mode)
        for action_id in order:
            spec = self.catalog.actions[action_id]
            try:
                raw = self.run(action_id, preview=preview)
            except errors.RpcError as exc:
                batch.results.append(
                    ActionResult.failure(action_id, exc.message).warn(
                        f"risk={spec.risk}"
                    )
                )
                # A blocked action is expected in a batch; keep going.
                continue
            except Exception as exc:  # noqa: BLE001
                batch.results.append(ActionResult.failure(action_id, str(exc)))
                continue

            result = ActionResult(
                action_id=raw["action_id"],
                ok=raw["ok"],
                status=raw["status"],
                message=raw["message"],
                changes=[Change(**_revive(c)) for c in raw["changes"]],
                warnings=raw["warnings"],
                error=raw["error"],
                needs_reboot=raw["needs_reboot"],
                duration_ms=raw["duration_ms"],
            )
            batch.results.append(result)

        return batch.to_dict()

    # ------------------------------------------------------------------- undo

    def undo(self, action_id: str) -> dict:
        # Undo deliberately bypasses the risk ceiling: taking a machine back to
        # where it was must always be allowed, even in safe mode.
        journal = UndoJournal.load(action_id, self.backend)
        if journal is None:
            raise errors.RpcError(
                errors.NO_UNDO,
                f"nothing recorded to undo for {action_id!r}",
            )
        # A throwaway shell (no journal) so restoring powercfg state does not
        # record new undo entries against the very journal we are discarding.
        shell = self.shell_factory(journal, False)
        changes = journal.replay(shell=shell)
        journal.discard()
        self.log.info("undid %s (%d entries)", action_id, len(changes))
        return {
            "action_id": action_id,
            "ok": True,
            "status": "undone",
            "entries": len(changes),
            "changes": changes,
            "message": f"restored {len(changes)} value(s)",
        }

    def undo_all(self) -> list[dict]:
        out = []
        for entry in UndoJournal.list_available():
            action_id = entry["action_id"]
            try:
                out.append(self.undo(action_id))
            except errors.RpcError as exc:
                out.append({"action_id": action_id, "ok": False, "error": exc.message})
        return out

    def list_undo(self) -> list[dict]:
        return UndoJournal.list_available()


# ------------------------------------------------------------------- helpers


def _check_args(action_id: str, handler: Handler, args: dict) -> None:
    """Validate runtime args against the handler's declared schema.

    The catalog already restricted args to primitives; this enforces the
    per-handler types and allowed values, so a bad remote value cannot reach a
    registry write.
    """
    schema = getattr(handler, "arg_schema", None) or {}
    unknown = [k for k in args if k not in schema]
    if unknown:
        raise errors.RpcError(
            errors.INVALID_PARAMS,
            f"action {action_id!r} does not accept arg(s) {unknown}",
        )
    for name, rules in schema.items():
        if name not in args:
            if "default" not in rules:
                raise errors.RpcError(
                    errors.INVALID_PARAMS, f"action {action_id!r} requires arg {name!r}"
                )
            continue
        value = args[name]
        expected = rules.get("type")
        if expected and not isinstance(value, expected):
            raise errors.RpcError(
                errors.INVALID_PARAMS,
                f"arg {name!r} must be {expected.__name__}, got {type(value).__name__}",
            )
        choices = rules.get("choices")
        if choices is not None and value not in choices:
            raise errors.RpcError(
                errors.INVALID_PARAMS,
                f"arg {name!r} must be one of {list(choices)}, got {value!r}",
            )
        lo, hi = rules.get("min"), rules.get("max")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if lo is not None and value < lo:
                raise errors.RpcError(errors.INVALID_PARAMS, f"arg {name!r} must be >= {lo}")
            if hi is not None and value > hi:
                raise errors.RpcError(errors.INVALID_PARAMS, f"arg {name!r} must be <= {hi}")


def _normalise(action_id: str, outcome: Any, *, preview: bool) -> ActionResult:
    """Let handlers return a bare list of Changes, a message, or a full result."""
    if isinstance(outcome, ActionResult):
        result = outcome
        result.action_id = action_id
    elif isinstance(outcome, list):
        result = ActionResult(action_id=action_id, changes=list(outcome))
    elif isinstance(outcome, str):
        result = ActionResult(action_id=action_id, message=outcome)
    elif outcome is None:
        result = ActionResult(action_id=action_id)
    else:
        raise errors.RpcError(
            errors.INTERNAL_ERROR,
            f"handler for {action_id!r} returned {type(outcome).__name__}; "
            "expected ActionResult, list[Change], str or None",
        )

    if preview and result.status == "applied":
        result.status = "preview"
    if result.ok and result.status not in ("preview", "skipped") and not result.message:
        result.message = "no changes needed" if not result.changes else "applied"
    return result


def _revive(change: dict) -> dict:
    return {**change, "kind": ChangeKind(change["kind"])}


__all__ = ["Executor", "Handler"]
