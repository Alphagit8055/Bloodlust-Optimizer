"""Synchronise the shipped catalog with the compiled legacy profile table.

The runtime rule is unchanged: the catalog is data and the sidecar only executes
handlers it ships. This script keeps the *bundled* catalog in step with the
profile table in ``bloodlust.actions.legacy`` so a new profile cannot be added
to the engine and forgotten in the UI (or vice versa).

Run from the repository root:

    python scripts/sync_legacy_catalog.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "sidecar" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bloodlust.actions.legacy import PROFILES  # noqa: E402

CATALOG = ROOT / "catalog" / "catalog.json"

# Bump by hand when the shipped catalog changes so installed apps can tell a
# new catalog from the one they bundled.
CATALOG_VERSION = "2026.11.20-1"


def main() -> int:
    payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    existing = {a["id"]: a for a in payload["actions"]}

    added = 0
    for profile in PROFILES.values():
        action = {
            "id": profile["id"],
            "label": profile["label"],
            "family": profile["family"],
            "risk": profile["risk"],
            "description": profile["description"],
            "handler": "legacy.apply",
            "args": {"profile": profile["id"]},
            "tags": list(profile["tags"]),
            "reversible": bool(profile["reversible"]),
            "default_enabled": bool(profile["default_enabled"]),
            "needs_reboot": bool(profile["needs_reboot"]),
        }
        if profile["requires"]:
            action["requires"] = dict(profile["requires"])
        if profile["id"] not in existing:
            added += 1
        existing[profile["id"]] = action

    # Preserve the original ordering for old rows, then append new rows in the
    # order the profile table declares them (which groups them by family).
    ordered: list[dict] = []
    seen: set[str] = set()
    for action in payload["actions"]:
        aid = action["id"]
        if aid in seen:
            continue
        seen.add(aid)
        ordered.append(existing.get(aid, action))
    for aid, action in existing.items():
        if aid not in seen:
            ordered.append(action)
            seen.add(aid)

    payload["actions"] = ordered
    payload["catalog_version"] = CATALOG_VERSION
    CATALOG.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"catalog now has {len(ordered)} actions ({added} new)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
