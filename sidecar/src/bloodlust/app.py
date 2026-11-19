"""Application state and the RPC method table.

The sidecar performs **no network I/O whatsoever**. The Rust shell fetches the
catalog and passes the bytes in; the sidecar only validates them. That keeps
the elevated, registry-mutating process entirely off the network, which removes
a large class of attack surface and makes the trust boundary easy to state:
one process talks to the internet, a different process talks to the machine.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from . import actions
from .core import catalog as catalog_mod
from .core import paths
from .core import registry as reg
from .core import risk as risk_mod
from .core.executor import Executor
from .core.logging import get, tail_buffer
from .core.result import ActionResult
from .detect import emulators as emu_detect
from .detect import hardware as hw_detect
from .rpc import errors
from .rpc.server import Dispatcher
from .version import (
    APP_NAME,
    APP_VERSION,
    CATALOG_SCHEMA_VERSION,
    DEVELOPER,
    PROTOCOL_VERSION,
)

_BUNDLED_CATALOG = "catalog.json"


class App:
    def __init__(self, backend: Optional[reg.RegistryBackend] = None) -> None:
        paths.ensure_dirs()
        self.log = get()
        self.backend = backend or reg.default_backend()

        self.handlers = actions.all_handlers()
        self.allowlist = actions.allowlist()

        self.hw = hw_detect.detect(self.backend)
        self.emulators = emu_detect.detect_all(self.backend)

        self.catalog: Optional[catalog_mod.Catalog] = None
        self.executor: Optional[Executor] = None
        self.risk_ceiling = "safe"
        self.state = self._load_state()
        self.started_at = time.time()

        self._load_catalog()

    # ------------------------------------------------------------- catalog

    def _bundled_path(self) -> str:
        """Locate the catalog that ships inside the frozen binary."""
        import os
        import sys
        from pathlib import Path

        # PyInstaller onedir puts data files next to the executable; a source
        # checkout puts them beside the package.
        if getattr(sys, "frozen", False):
            base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
            candidates = [
                base / _BUNDLED_CATALOG,
                Path(sys.executable).parent / _BUNDLED_CATALOG,
            ]
        else:
            here = Path(__file__).resolve().parent
            candidates = [
                here.parent.parent.parent / "catalog" / _BUNDLED_CATALOG,
                here / _BUNDLED_CATALOG,
            ]
        for c in candidates:
            if c.is_file():
                return str(c)
        raise FileNotFoundError(
            "bundled catalog.json not found; looked in "
            + ", ".join(str(c) for c in candidates)
        )

    def _load_catalog(self) -> None:
        """Load the bundled catalog. Always succeeds or the app is unusable."""
        try:
            path = self._bundled_path()
        except FileNotFoundError as exc:
            self.log.error("%s", exc)
            self.log.error("starting with an empty catalog; the UI will show no actions")
            self.catalog = catalog_mod.Catalog(actions={}, families={}, provenance=catalog_mod.Provenance(source="missing"))
            return

        try:
            self.catalog = catalog_mod.load_file(
                path, self.allowlist, source="bundled"
            )
            self.log.info(
                "bundled catalog loaded: %d actions from %s",
                len(self.catalog.actions),
                self.catalog.provenance.catalog_version or "unversioned",
            )
        except catalog_mod.CatalogError as exc:
            self.log.error("bundled catalog is invalid: %s", exc.problems[:5])
            self.catalog = catalog_mod.Catalog(
                actions={},
                families={},
                provenance=catalog_mod.Provenance(source="invalid"),
            )

        self._rebuild_executor()

    def _rebuild_executor(self) -> None:
        self.executor = Executor(
            self.catalog,
            self.handlers,
            self.hw,
            self.emulators,
            risk_ceiling=self.risk_ceiling,
            backend=self.backend,
        )

    def _require_executor(self) -> Executor:
        if self.executor is None or not self.catalog or not self.catalog.actions:
            raise errors.RpcError(errors.NOT_READY, "no catalog loaded; nothing to run")
        return self.executor

    # --------------------------------------------------------------- state

    def _load_state(self) -> dict:
        try:
            return json.loads(paths.state_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            paths.state_file().write_text(
                json.dumps(self.state, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            self.log.warning("could not persist state: %s", exc)

    def _blocks(self) -> list[dict]:
        out = []
        if not self.catalog:
            return out
        for spec in self.catalog.actions.values():
            gate = risk_mod.evaluate(spec, self.hw, self.emulators)
            if not gate.available:
                out.append({"id": spec.id, "label": spec.label, "reason": gate.reason})
        return out

    # -------------------------------------------------------------- methods

    def ping(self, params: dict) -> dict:
        return {
            "pong": True,
            "protocol": PROTOCOL_VERSION,
            "version": APP_VERSION,
            "uptime_s": round(time.time() - self.started_at, 2),
        }

    def system_info(self, params: dict) -> dict:
        return {
            "app": {
                "name": APP_NAME,
                "version": APP_VERSION,
                "developer": DEVELOPER,
                "protocol": PROTOCOL_VERSION,
            },
            "hardware": self.hw.to_dict(),
            "blocked_actions": len(self._blocks()),
            "state": self.state,
        }

    def system_emulators(self, params: dict) -> dict:
        return {
            key: info.to_dict() for key, info in self.emulators.items()
        }

    def system_refresh(self, params: dict) -> dict:
        """Re-detect. Cheap, and lets the UI recover after a hardware change."""
        self.hw = hw_detect.detect(self.backend)
        self.emulators = emu_detect.detect_all(self.backend)
        self._rebuild_executor()
        return {"ok": True, "blocked_actions": len(self._blocks())}

    # ------------------------------------------------------------- catalog

    def catalog_load(self, params: dict) -> dict:
        """Install a catalog the shell fetched. Replaces the bundled one."""
        payload = params.get("payload")
        sha256 = params.get("sha256")
        url = params.get("url")

        if payload is None:
            raise errors.RpcError(errors.INVALID_PARAMS, "payload is required")
        if isinstance(payload, (bytes, bytearray)):
            raw: bytes = bytes(payload)
        elif isinstance(payload, str):
            raw = payload.encode("utf-8")
        else:
            raw = json.dumps(payload).encode("utf-8")

        try:
            new = catalog_mod.load_bytes(
                raw, self.allowlist, source="remote", url=url, sha256=sha256
            )
        except catalog_mod.CatalogError as exc:
            self.log.warning(
                "remote catalog rejected (%d problems); keeping %s",
                len(exc.problems),
                self.catalog.provenance.source if self.catalog else "nothing",
            )
            raise errors.RpcError(
                errors.CATALOG_INVALID,
                "the catalog from GitHub was rejected and the bundled copy is in use",
                {"problems": exc.problems[:25], "total": len(exc.problems)},
            )

        self.catalog = new
        self._rebuild_executor()
        self.log.info(
            "remote catalog accepted: %d actions (version %s)",
            len(new.actions),
            new.provenance.catalog_version or "unversioned",
        )
        return {
            "ok": True,
            "actions": len(new.actions),
            "provenance": new.provenance.to_dict(),
        }

    def catalog_state(self, params: dict) -> dict:
        if self.catalog is None:
            raise errors.RpcError(errors.NOT_READY, "no catalog loaded")
        return self.catalog.to_dict()

    def catalog_families(self, params: dict) -> list[dict]:
        if self.catalog is None:
            return []
        return [
            f.to_dict() for f in sorted(self.catalog.families.values(), key=lambda f: f.order)
        ]

    # ------------------------------------------------------------- actions

    def action_list(self, params: dict) -> list[dict]:
        self._require_executor()
        family = params.get("family")
        out = []
        for spec in self.catalog.actions.values():
            if family and spec.family != family:
                continue
            gate = self.executor.gate_for(spec.id)
            ceiling_ok = risk_mod.risk_ceiling_ok(
                spec.risk, self.risk_ceiling, catalog_mod.RISK_ORDER
            )
            out.append(
                {
                    **spec.to_dict(),
                    "available": gate.available,
                    "reason": gate.reason,
                    "risk_ceiling_ok": ceiling_ok,
                }
            )
        out.sort(key=lambda a: (a["family"], a["id"]))
        return out

    def action_describe(self, params: dict) -> dict:
        executor = self._require_executor()
        action_id = params.get("id")
        if not action_id:
            raise errors.RpcError(errors.INVALID_PARAMS, "id is required")
        return executor.describe(action_id)

    def action_preview(self, params: dict) -> dict:
        executor = self._require_executor()
        action_id = params.get("id")
        if not action_id:
            raise errors.RpcError(errors.INVALID_PARAMS, "id is required")
        return executor.preview(action_id, params.get("args"))

    def action_run(self, params: dict) -> dict:
        executor = self._require_executor()
        action_id = params.get("id")
        if not action_id:
            raise errors.RpcError(errors.INVALID_PARAMS, "id is required")
        return executor.run(action_id, params.get("args"))

    def batch_run(self, params: dict) -> dict:
        executor = self._require_executor()
        mode = params.get("mode", "safe")
        return executor.run_batch(mode, params.get("ids"), preview=bool(params.get("preview")))

    # ---------------------------------------------------------------- undo

    def undo_list(self, params: dict) -> list[dict]:
        executor = self._require_executor()
        entries = executor.list_undo()
        by_id = {e["action_id"]: e for e in entries}
        for entry in entries:
            spec = self.catalog.get(entry["action_id"])
            entry["label"] = spec.label if spec else entry["action_id"]
        return entries

    def undo_action(self, params: dict) -> dict:
        executor = self._require_executor()
        action_id = params.get("id")
        if not action_id:
            raise errors.RpcError(errors.INVALID_PARAMS, "id is required")
        return executor.undo(action_id)

    def undo_all(self, params: dict) -> dict:
        executor = self._require_executor()
        results = executor.undo_all()
        return {
            "results": results,
            "restored": sum(1 for r in results if r.get("ok")),
            "failed": sum(1 for r in results if not r.get("ok")),
        }

    # --------------------------------------------------------------- state

    def state_get(self, params: dict) -> dict:
        return {"state": self.state, "risk_ceiling": self.risk_ceiling}

    def state_set(self, params: dict) -> dict:
        updates = params.get("values")
        if not isinstance(updates, dict):
            raise errors.RpcError(errors.INVALID_PARAMS, "values must be an object")
        for key, value in updates.items():
            if not isinstance(key, str) or len(key) > 64:
                raise errors.RpcError(errors.INVALID_PARAMS, f"bad state key {key!r}")
            if isinstance(value, (dict, list)):
                raise errors.RpcError(errors.INVALID_PARAMS, f"state value {key!r} must be a primitive")
            self.state[key] = value
        self._save_state()
        return {"ok": True, "state": self.state}

    def set_risk_ceiling(self, params: dict) -> dict:
        ceiling = params.get("ceiling", "safe")
        if ceiling not in catalog_mod.RISKS:
            raise errors.RpcError(
                errors.INVALID_PARAMS, f"ceiling must be one of {list(catalog_mod.RISKS)}"
            )
        self.risk_ceiling = ceiling
        if self.executor:
            self.executor.risk_ceiling = ceiling
        self.state["risk_ceiling"] = ceiling
        self._save_state()
        return {"ok": True, "risk_ceiling": ceiling}

    # --------------------------------------------------------------- debug

    def log_tail(self, params: dict) -> list[dict]:
        limit = params.get("limit", 100)
        return tail_buffer().tail(int(limit) if limit else 100)

    def handlers_list(self, params: dict) -> dict:
        return {
            "count": len(self.handlers),
            "handlers": actions.describe_handlers(),
            "note": "a remote catalog can only use handlers listed here",
        }

    def blocks_explain(self, params: dict) -> list[dict]:
        return self._blocks()

    # ----------------------------------------------------------- dispatch

    def register(self, dispatcher: Dispatcher) -> Dispatcher:
        table = {
            "ping": self.ping,
            "system.info": self.system_info,
            "system.emulators": self.system_emulators,
            "system.refresh": self.system_refresh,
            "catalog.load": self.catalog_load,
            "catalog.state": self.catalog_state,
            "catalog.families": self.catalog_families,
            "action.list": self.action_list,
            "action.describe": self.action_describe,
            "action.preview": self.action_preview,
            "action.run": self.action_run,
            "batch.run": self.batch_run,
            "undo.list": self.undo_list,
            "undo.action": self.undo_action,
            "undo.all": self.undo_all,
            "state.get": self.state_get,
            "state.set": self.state_set,
            "risk.setCeiling": self.set_risk_ceiling,
            "log.tail": self.log_tail,
            "debug.handlers": self.handlers_list,
            "debug.blocks": self.blocks_explain,
        }
        for name, fn in table.items():
            dispatcher.method(name, fn)
        return dispatcher


__all__ = ["App"]
