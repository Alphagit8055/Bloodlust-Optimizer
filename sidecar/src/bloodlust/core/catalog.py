"""Catalog loading and validation.

The catalog is fetched at runtime from the GitHub repo, so it crosses a trust
boundary. The rule that makes that safe is simple and absolute:

    **The catalog is data. It can never introduce code.**

Concretely, validation rejects a catalog unless every one of these holds:

1. ``schema`` matches ``CATALOG_SCHEMA_VERSION``.
2. Every ``handler`` string is present in the handler allowlist compiled into
   *this* build. A remote catalog cannot register a new action, only configure
   one the binary already ships. This is the single most important control:
   even a fully compromised repo cannot make the app do something new.
3. ``args`` contain only JSON primitives of the declared types, and every key
   is declared by the handler's schema. No unknown keys.
4. No action may declare a shell command, script, PowerShell, path or any other
   executable payload. The schema has no such field, and unknown action keys
   are rejected rather than ignored — so an attacker cannot smuggle one in and
   hope a later version honours it.
5. ``id``s are unique and match ``^[a-z][a-z0-9_]{2,63}$``.

Validation is *all-or-nothing*: a catalog with one bad action is rejected in
full and the bundled fallback is used, rather than partially applying. Partial
application would mean a user could never be sure what set of tweaks they had.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..version import CATALOG_SCHEMA_VERSION
from .logging import get

ACTION_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")

RISKS = ("safe", "moderate", "aggressive", "danger")
RISK_ORDER = {r: i for i, r in enumerate(RISKS)}

# Keys an action object may contain. Anything else -> reject.
_ALLOWED_ACTION_KEYS = frozenset(
    {
        "id",
        "label",
        "family",
        "risk",
        "reversible",
        "description",
        "handler",
        "args",
        "requires",
        "tags",
        "default_enabled",
        "needs_reboot",
    }
)

_ALLOWED_REQUIRES_KEYS = frozenset(
    {
        "platform",
        "elevated",
        "min_os_build",
        "cpu_vendor",
        "cpu_generation_min",
        "cpu_generation_max",
        "gpu_vendor",
        "emulator",
        "hypervisor_absent",
    }
)

_ALLOWED_FAMILIES_KEYS = frozenset({"id", "label", "description", "icon", "order"})
_ALLOWED_TOP_KEYS = frozenset(
    {"schema", "catalog_version", "min_app_version", "families", "actions", "notice"}
)

# JSON primitive types permitted in args. No lists of objects, no nesting.
_PRIMITIVES = (str, int, float, bool)


class CatalogError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"catalog rejected ({len(problems)} problem(s))")
        self.problems = problems


@dataclass
class Family:
    id: str
    label: str
    description: str = ""
    icon: str = ""
    order: int = 100

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "icon": self.icon,
            "order": self.order,
        }


@dataclass
class ActionSpec:
    id: str
    label: str
    family: str
    risk: str
    description: str
    handler: str
    args: dict = field(default_factory=dict)
    requires: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    reversible: bool = True
    default_enabled: bool = True
    needs_reboot: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "family": self.family,
            "risk": self.risk,
            "description": self.description,
            "handler": self.handler,
            "args": self.args,
            "requires": self.requires,
            "tags": self.tags,
            "reversible": self.reversible,
            "default_enabled": self.default_enabled,
            "needs_reboot": self.needs_reboot,
        }


@dataclass
class Provenance:
    """Where the active catalog came from. Surfaced in the UI."""

    source: str = "bundled"      # bundled | remote
    url: Optional[str] = None
    sha256: Optional[str] = None
    fetched_at: Optional[float] = None
    catalog_version: Optional[str] = None
    etag: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "source": self.source,
            "url": self.url,
            "sha256": self.sha256,
            "fetched_at": self.fetched_at,
            "catalog_version": self.catalog_version,
        }
        if self.etag:
            d["etag"] = self.etag
        return d


@dataclass
class Catalog:
    actions: dict[str, ActionSpec]
    families: dict[str, Family]
    provenance: Provenance
    notice: str = ""

    def get(self, action_id: str) -> Optional[ActionSpec]:
        return self.actions.get(action_id)

    def ids(self) -> list[str]:
        return sorted(self.actions)

    def by_family(self) -> dict[str, list[ActionSpec]]:
        out: dict[str, list[ActionSpec]] = {}
        for spec in self.actions.values():
            out.setdefault(spec.family, []).append(spec)
        for v in out.values():
            v.sort(key=lambda s: s.id)
        return out

    def count_by_risk(self) -> dict[str, int]:
        out = {r: 0 for r in RISKS}
        for spec in self.actions.values():
            out[spec.risk] = out.get(spec.risk, 0) + 1
        return out

    def to_dict(self) -> dict:
        return {
            "provenance": self.provenance.to_dict(),
            "notice": self.notice,
            "counts": {
                "actions": len(self.actions),
                "families": len(self.families),
                **{f"risk_{k}": v for k, v in self.count_by_risk().items()},
            },
            "families": [f.to_dict() for f in sorted(self.families.values(), key=lambda f: f.order)],
        }


# ------------------------------------------------------------------ validation


def validate(
    payload: Any,
    handler_allowlist: set[str],
    *,
    source: str = "bundled",
    url: Optional[str] = None,
    sha256: Optional[str] = None,
) -> Catalog:
    """Validate and build a :class:`Catalog`. Raises :class:`CatalogError`.

    Every problem is collected so the user (and the repo CI) sees the whole
    list at once rather than fixing them one rebuild at a time.
    """
    problems: list[str] = []

    if not isinstance(payload, dict):
        raise CatalogError(["catalog root must be a JSON object"])

    for key in payload:
        if key not in _ALLOWED_TOP_KEYS:
            problems.append(f"unknown top-level key {key!r}")

    schema = payload.get("schema")
    if schema != CATALOG_SCHEMA_VERSION:
        problems.append(
            f"schema {schema!r} != expected {CATALOG_SCHEMA_VERSION} (update the app)"
        )

    # --- families ---------------------------------------------------------
    families: dict[str, Family] = {}
    raw_families = payload.get("families", [])
    if not isinstance(raw_families, list):
        problems.append('"families" must be a list')
        raw_families = []
    for i, fam in enumerate(raw_families):
        if not isinstance(fam, dict):
            problems.append(f"families[{i}] must be an object")
            continue
        for key in fam:
            if key not in _ALLOWED_FAMILIES_KEYS:
                problems.append(f"families[{i}]: unknown key {key!r}")
        fid = fam.get("id")
        if not isinstance(fid, str) or not ACTION_ID_RE.match(fid):
            problems.append(f"families[{i}]: bad id {fid!r}")
            continue
        if fid in families:
            problems.append(f"families[{i}]: duplicate id {fid!r}")
            continue
        families[fid] = Family(
            id=fid,
            label=str(fam.get("label") or fid),
            description=str(fam.get("description") or ""),
            icon=str(fam.get("icon") or ""),
            order=int(fam.get("order", 100)) if isinstance(fam.get("order", 100), int) else 100,
        )

    # --- actions ----------------------------------------------------------
    actions: dict[str, ActionSpec] = {}
    raw_actions = payload.get("actions", [])
    if not isinstance(raw_actions, list):
        problems.append('"actions" must be a list')
        raw_actions = []

    if not raw_actions:
        problems.append("catalog declares no actions")

    for i, act in enumerate(raw_actions):
        where = f"actions[{i}]"
        if not isinstance(act, dict):
            problems.append(f"{where} must be an object")
            continue

        aid = act.get("id")
        if isinstance(aid, str) and ACTION_ID_RE.match(aid):
            where = f"actions[{i}] ({aid})"

        for key in act:
            if key not in _ALLOWED_ACTION_KEYS:
                # Deliberately a hard error, not a warning: this is what stops
                # a future-proofed payload field from being smuggled in.
                problems.append(f"{where}: unknown key {key!r} (catalog cannot carry executables)")

        if not isinstance(aid, str) or not ACTION_ID_RE.match(aid):
            problems.append(f"actions[{i}]: id must match {ACTION_ID_RE.pattern}, got {aid!r}")
            continue
        if aid in actions:
            problems.append(f"{where}: duplicate id")
            continue

        label = act.get("label")
        if not isinstance(label, str) or not label.strip():
            problems.append(f"{where}: label must be a non-empty string")
            continue

        handler = act.get("handler")
        if not isinstance(handler, str) or not handler.strip():
            problems.append(f"{where}: handler must be a non-empty string")
            continue
        if handler not in handler_allowlist:
            problems.append(
                f"{where}: handler {handler!r} is not shipped by this build"
                " (a remote catalog cannot add code)"
            )
            continue

        risk = act.get("risk", "safe")
        if risk not in RISKS:
            problems.append(f"{where}: risk must be one of {RISKS}, got {risk!r}")
            continue

        family = act.get("family")
        if not isinstance(family, str) or family not in families:
            problems.append(f"{where}: family {family!r} is not declared in 'families'")
            continue

        args = act.get("args", {})
        if not isinstance(args, dict):
            problems.append(f"{where}: args must be an object")
            continue
        for name, value in args.items():
            if isinstance(value, (list, dict)) or (value is not None and not isinstance(value, _PRIMITIVES)):
                problems.append(
                    f"{where}: arg {name!r} must be a JSON primitive, got {type(value).__name__}"
                )

        requires = act.get("requires", {})
        if not isinstance(requires, dict):
            problems.append(f"{where}: requires must be an object")
            continue
        for key in requires:
            if key not in _ALLOWED_REQUIRES_KEYS:
                problems.append(f"{where}: unknown requires key {key!r}")

        for flag in ("reversible", "default_enabled", "needs_reboot"):
            if flag in act and not isinstance(act[flag], bool):
                problems.append(f"{where}: {flag} must be a boolean")

        tags = act.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            problems.append(f"{where}: tags must be a list of strings")
            continue

        actions[aid] = ActionSpec(
            id=aid,
            label=label.strip(),
            family=family,
            risk=risk,
            description=str(act.get("description") or ""),
            handler=handler,
            args=args,
            requires=requires,
            tags=list(tags),
            reversible=bool(act.get("reversible", True)),
            default_enabled=bool(act.get("default_enabled", True)),
            needs_reboot=bool(act.get("needs_reboot", False)),
        )

    if problems:
        for p in problems[:20]:
            get().error("catalog: %s", p)
        raise CatalogError(problems)

    notice = payload.get("notice")
    if notice is not None and not isinstance(notice, str):
        notice = ""

    return Catalog(
        actions=actions,
        families=families,
        notice=str(notice or ""),
        provenance=Provenance(
            source=source,
            url=url,
            sha256=sha256,
            fetched_at=time.time() if source == "remote" else None,
            catalog_version=str(payload.get("catalog_version") or ""),
        ),
    )


def load_bytes(
    raw: bytes | str,
    handler_allowlist: set[str],
    *,
    source: str = "bundled",
    url: Optional[str] = None,
    sha256: Optional[str] = None,
    max_bytes: int = 4 * 1024 * 1024,
) -> Catalog:
    """Parse raw catalog bytes, enforcing a size cap before parsing.

    The cap is checked on the raw payload, not the parsed tree, so a
    decompression-style blow-up cannot exhaust memory first.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > max_bytes:
        raise CatalogError([f"catalog is {len(raw)} bytes, over the {max_bytes} byte cap"])
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CatalogError([f"catalog is not valid JSON: {exc}"])
    return validate(payload, handler_allowlist, source=source, url=url, sha256=sha256)


def load_file(
    path: str, handler_allowlist: set[str], *, source: str = "bundled"
) -> Catalog:
    import hashlib
    from pathlib import Path

    data = Path(path).read_bytes()
    return load_bytes(
        data,
        handler_allowlist,
        source=source,
        url=str(path),
        sha256=hashlib.sha256(data).hexdigest(),
    )


__all__ = [
    "Catalog",
    "ActionSpec",
    "Family",
    "Provenance",
    "CatalogError",
    "validate",
    "load_bytes",
    "load_file",
    "RISKS",
    "RISK_ORDER",
    "ACTION_ID_RE",
]
