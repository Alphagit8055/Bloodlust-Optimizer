"""Registry facade, journaling and undo.

This is the safety net for the whole application, so it gets the most thorough
treatment: idempotence, double-apply, partial failure, and the case where a
value never existed before.
"""

from __future__ import annotations

import json

import pytest

from bloodlust.core import paths, registry as reg
from bloodlust.core.backup import UndoJournal
from bloodlust.core.mutate import RegistryFacade


@pytest.fixture
def journal(backend):
    return UndoJournal("test_action", backend)


@pytest.fixture
def facade(backend, journal):
    return RegistryFacade(backend, journal)


# ------------------------------------------------------------------ read/write


def test_set_then_read_roundtrip(facade, backend):
    facade.set_value("HKLM", r"SOFTWARE\Test", "Flag", reg.REG_DWORD, 1)
    assert backend.read("HKLM", r"SOFTWARE\Test", "Flag") == (reg.REG_DWORD, 1)


def test_set_returns_a_change(facade):
    change = facade.set_value("HKCU", r"Software\Test", "Name", reg.REG_SZ, "hi")
    assert change is not None
    assert change.target == r"HKCU\Software\Test\Name"
    assert "created" in change.detail
    assert change.reversible is True


def test_change_shows_before_and_after(facade, backend):
    backend.write("HKCU", r"Software\Test", "N", reg.REG_DWORD, 5)
    change = facade.set_value("HKCU", r"Software\Test", "N", reg.REG_DWORD, 9)
    assert change.detail == "5 -> 9"
    assert change.previous == 5


def test_idempotent_write_returns_none(facade, backend):
    """Re-applying an action must not churn the journal."""
    backend.write("HKCU", r"Software\Test", "N", reg.REG_DWORD, 7)
    assert facade.set_value("HKCU", r"Software\Test", "N", reg.REG_DWORD, 7) is None
    assert facade._j.empty is True


def test_type_change_is_not_treated_as_idempotent(facade, backend):
    """Same value, different type: REG_SZ '7' is not REG_DWORD 7."""
    backend.write("HKCU", r"Software\Test", "N", reg.REG_SZ, "7")
    change = facade.set_value("HKCU", r"Software\Test", "N", reg.REG_DWORD, 7)
    assert change is not None


def test_dword_max_value_survives(facade, backend):
    """0xFFFFFFFF is the NetworkThrottlingIndex sentinel; it must not wrap."""
    facade.set_value("HKLM", r"SOFTWARE\Test", "N", reg.REG_DWORD, 0xFFFFFFFF)
    assert backend.read("HKLM", r"SOFTWARE\Test", "N") == (reg.REG_DWORD, 4294967295)


def test_multi_sz_roundtrip(facade, backend):
    values = ["a", "b", "c"]
    facade.set_value("HKLM", r"SOFTWARE\Test", "L", reg.REG_MULTI_SZ, values)
    assert backend.read("HKLM", r"SOFTWARE\Test", "L") == (reg.REG_MULTI_SZ, values)


def test_delete_nonexistent_value_is_a_noop(facade):
    assert facade.delete_value("HKCU", r"Software\Nothing", "X") is None
    assert facade._j.empty


# ------------------------------------------------------------------- preview


def test_preview_computes_changes_without_applying(backend):
    journal = UndoJournal("preview_action", backend)
    facade = RegistryFacade(backend, journal, preview=True)

    change = facade.set_value("HKLM", r"SOFTWARE\Test", "N", reg.REG_DWORD, 3)
    assert change is not None
    assert change.detail == "created REG_DWORD"
    # The whole point: the backend was not touched.
    assert backend.read("HKLM", r"SOFTWARE\Test", "N") is None


def test_preview_does_not_persist_a_journal(backend):
    journal = UndoJournal("preview_action", backend)
    facade = RegistryFacade(backend, journal, preview=True)
    facade.set_value("HKLM", r"SOFTWARE\Test", "N", reg.REG_DWORD, 3)
    assert journal.commit() is None


def test_preview_and_apply_agree_on_the_changes(backend):
    """If preview and run disagree, the UI is lying to the user."""
    j1 = UndoJournal("a", backend)
    preview_changes = [
        c for c in [
            RegistryFacade(backend, j1, preview=True).set_value(
                "HKLM", r"SOFTWARE\X", "A", reg.REG_DWORD, 1
            ),
            RegistryFacade(backend, j1, preview=True).set_value(
                "HKLM", r"SOFTWARE\X", "B", reg.REG_SZ, "z"
            ),
        ] if c
    ]

    j2 = UndoJournal("a", backend)
    facade = RegistryFacade(backend, j2, preview=False)
    applied = [
        c for c in [
            facade.set_value("HKLM", r"SOFTWARE\X", "A", reg.REG_DWORD, 1),
            facade.set_value("HKLM", r"SOFTWARE\X", "B", reg.REG_SZ, "z"),
        ] if c
    ]

    assert [c.target for c in preview_changes] == [c.target for c in applied]
    assert [c.detail for c in preview_changes] == [c.detail for c in applied]


# -------------------------------------------------------------------- journal


def test_journal_roundtrip_through_disk(facade, backend):
    backend.write("HKCU", r"Software\Test", "N", reg.REG_DWORD, 1)
    facade.set_value("HKCU", r"Software\Test", "N", reg.REG_DWORD, 2)
    path = facade._j.commit()

    assert path is not None and path.exists()
    loaded = UndoJournal.load("test_action", backend)
    assert loaded is not None
    assert len(loaded.entries) == 1
    assert loaded.entries[0]["prev_value"] == 1


def test_empty_journal_is_not_written(facade):
    assert facade._j.commit() is None
    assert not (paths.undo_dir() / "test_action.json").exists()


def test_undo_restores_the_original_value(backend):
    backend.write("HKCU", r"Software\Test", "N", reg.REG_DWORD, 42)

    j = UndoJournal("act", backend)
    RegistryFacade(backend, j).set_value("HKCU", r"Software\Test", "N", reg.REG_DWORD, 99)
    j.commit()

    loaded = UndoJournal.load("act", backend)
    changes = loaded.replay()
    assert backend.read("HKCU", r"Software\Test", "N") == (reg.REG_DWORD, 42)
    assert len(changes) == 1


def test_undo_deletes_a_value_that_never_existed(backend):
    j = UndoJournal("act", backend)
    RegistryFacade(backend, j).set_value("HKCU", r"Software\New", "Fresh", reg.REG_SZ, "x")
    j.commit()

    assert backend.read("HKCU", r"Software\New", "Fresh") is not None
    UndoJournal.load("act", backend).replay()
    assert backend.read("HKCU", r"Software\New", "Fresh") is None


def test_double_apply_then_single_undo_reaches_the_true_original(backend):
    """The bug this guards against: apply, apply, undo landing on run #1's value."""
    backend.write("HKCU", r"Software\Test", "N", reg.REG_DWORD, 0)

    for new_value in (10, 20):
        j = UndoJournal("act", backend)
        RegistryFacade(backend, j).set_value("HKCU", r"Software\Test", "N", reg.REG_DWORD, new_value)
        j.commit()

    loaded = UndoJournal.load("act", backend)
    # Both applications were journaled, merged oldest-first.
    assert len(loaded.entries) == 2
    loaded.replay()

    assert backend.read("HKCU", r"Software\Test", "N") == (reg.REG_DWORD, 0), (
        "undo landed on an intermediate value instead of the original"
    )


def test_undo_is_idempotent_after_discard(backend):
    j = UndoJournal("act", backend)
    RegistryFacade(backend, j).set_value("HKCU", r"Software\X", "A", reg.REG_DWORD, 1)
    j.commit()

    loaded = UndoJournal.load("act", backend)
    loaded.replay()
    loaded.discard()
    assert UndoJournal.load("act", backend) is None


def test_binary_values_survive_the_json_journal(backend):
    # Seed a previous value so the journal actually has to encode one.
    previous = b"\xde\xad\xbe\xef"
    backend.write("HKLM", r"SOFTWARE\Bin", "Data", reg.REG_BINARY, previous)

    payload = bytes(range(256))
    j = UndoJournal("act", backend)
    RegistryFacade(backend, j).set_value("HKLM", r"SOFTWARE\Bin", "Data", reg.REG_BINARY, payload)
    j.commit()

    # Prove it went through JSON rather than just staying in memory.
    on_disk = (paths.undo_dir() / "act.json").read_text(encoding="utf-8")
    assert "__binary__" in on_disk

    loaded = UndoJournal.load("act", backend)
    loaded.replay()
    assert backend.read("HKLM", r"SOFTWARE\Bin", "Data") == (reg.REG_BINARY, previous)


def test_corrupt_journal_is_reported_not_fatal(backend):
    (paths.undo_dir() / "broken.json").write_text("{not json", encoding="utf-8")
    assert UndoJournal.load("broken", backend) is None


def test_list_available_reports_every_journal(backend):
    for name, value in (("a", 1), ("b", 2)):
        j = UndoJournal(name, backend)
        RegistryFacade(backend, j).set_value("HKCU", rf"Software\{name}", "V", reg.REG_DWORD, value)
        j.commit()

    available = {e["action_id"] for e in UndoJournal.list_available()}
    assert available == {"a", "b"}


# ---------------------------------------------------------------- powercfg undo


def test_powercfg_index_undo_restores_both_ac_and_dc(backend):
    """Journal replay for powercfg entries needs a shell; verify the argv."""
    captured: list[list[str]] = []

    class FakeShell:
        def run(self, argv, **kwargs):
            captured.append(argv)

            class O:
                returncode = 0
                stdout = ""
                stderr = ""

            return O()

    j = UndoJournal("pwr", backend)
    j.entries.append(
        {
            "kind": "powercfg_index",
            "subgroup": "SUB",
            "setting": "SET",
            "prev_ac": 2,
            "prev_dc": 2,
            "describe": "test",
        }
    )
    j.commit()

    loaded = UndoJournal.load("pwr", backend)
    loaded.replay(shell=FakeShell())

    ac_calls = [c for c in captured if "/setacvalueindex" in c]
    dc_calls = [c for c in captured if "/setdcvalueindex" in c]
    assert len(ac_calls) == 1, f"expected exactly one AC restore, got {captured}"
    assert len(dc_calls) == 1, f"expected exactly one DC restore, got {captured}"
    assert ac_calls[0][-1] == "2"
    # And the scheme must be re-applied for the indexes to take effect.
    assert any("/setactive" in c for c in captured)


def test_registry_only_journal_needs_no_shell(backend):
    j = UndoJournal("reg", backend)
    RegistryFacade(backend, j).set_value("HKCU", r"Software\X", "A", reg.REG_DWORD, 1)
    j.commit()
    changes = UndoJournal.load("reg", backend).replay(shell=None)
    assert len(changes) == 1


def test_command_entries_are_audited_but_not_replayed(backend):
    """A bare command has no generic inverse, so it must not be pretended away."""
    j = UndoJournal("cmd", backend)
    j.entries.append({"kind": "command", "argv": ["ipconfig", "/flushdns"], "reversible": False})
    j.commit()
    changes = UndoJournal.load("cmd", backend).replay()
    assert changes == []


def test_command_restore_replays_the_inverse(backend):
    captured = []

    class FakeShell:
        def run(self, argv, **kwargs):
            captured.append(argv)

            class O:
                returncode = 0

            return O()

    j = UndoJournal("net", backend)
    j.entries.append(
        {
            "kind": "command_restore",
            "restore_argv": ["netsh", "interface", "tcp", "set", "global", "autotuninglevel=normal"],
            "describe": "restore autotuning",
        }
    )
    j.commit()
    UndoJournal.load("net", backend).replay(shell=FakeShell())
    assert len(captured) == 1
    assert "autotuninglevel=normal" in captured[0]
