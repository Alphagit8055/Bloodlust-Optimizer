"""Handler behaviour, driven through the real executor with a fake shell.

These are the tests that would catch a wrong ``powercfg`` GUID, an argv emitted
twice, or a restore command that does not actually restore.
"""

from __future__ import annotations

import pytest

from bloodlust.actions import base as action_base
from bloodlust.actions.adb import ALLOWED_SETTINGS
from bloodlust.actions.base import PowerCfg, _parse_index
from bloodlust.core import registry as reg
from bloodlust.rpc import errors


# --------------------------------------------------------- powercfg /q parsing

_AC_DC_OUTPUT = """
Power Setting Index Settings
----------------------------
Power Setting GUID: 893dee8e-2bef-41e0-89c6-b55d0929964c  (Minimum processor state)
  Possible Settings Min: 0x00000000
  Possible Settings Max: 0x00000064
  Current AC Power Setting Index: 0x00000005
  Current DC Power Setting Index: 0x00000032
"""


def test_parse_index_reads_both_scopes():
    assert _parse_index(_AC_DC_OUTPUT, "AC") == 5
    assert _parse_index(_AC_DC_OUTPUT, "DC") == 50


def test_parse_index_returns_none_when_absent():
    assert _parse_index("nothing useful here", "AC") is None
    assert _parse_index("", "DC") is None


def test_parse_index_handles_hex_above_9():
    out = _AC_DC_OUTPUT.replace("0x00000032", "0x000000ff")
    assert _parse_index(out, "DC") == 255


# ----------------------------------------------------------- PowerCfg.set_index


def test_set_index_emits_exactly_one_write_per_scope(fake_shell, backend):
    from bloodlust.core.backup import UndoJournal

    journal = UndoJournal("t", backend)
    shell = fake_shell.factory(journal, False)
    shell.responses["/q "] = (0, _AC_DC_OUTPUT, "")
    pc = PowerCfg(shell, journal)

    changes = pc.set_index("SUB", "SET", ac=100, dc=100, describe="test")

    ac = shell.calls_containing("/setacvalueindex")
    dc = shell.calls_containing("/setdcvalueindex")
    assert len(ac) == 1, f"expected one AC write, got {shell.calls}"
    assert len(dc) == 1, f"expected one DC write, got {shell.calls}"
    assert ac[0][-1] == "100"
    # The scheme must be re-applied or the indexes do not take effect.
    assert len(shell.calls_containing("/setactive")) == 1
    assert len(changes) == 2


def test_set_index_skips_a_scope_that_already_matches(fake_shell, backend):
    from bloodlust.core.backup import UndoJournal

    journal = UndoJournal("t", backend)
    shell = fake_shell.factory(journal, False)
    shell.responses["/q "] = (0, _AC_DC_OUTPUT, "")
    pc = PowerCfg(shell, journal)

    # AC is already 5 in the canned output.
    changes = pc.set_index("SUB", "SET", ac=5, dc=100, describe="test")
    assert len(shell.calls_containing("/setacvalueindex")) == 0
    assert len(shell.calls_containing("/setdcvalueindex")) == 1
    assert len(changes) == 1


def test_set_index_records_undo_for_both_scopes(fake_shell, backend):
    from bloodlust.core.backup import UndoJournal

    journal = UndoJournal("t", backend)
    shell = fake_shell.factory(journal, False)
    shell.responses["/q "] = (0, _AC_DC_OUTPUT, "")
    PowerCfg(shell, journal).set_index("SUB", "SET", ac=100, dc=100, describe="test")

    entries = [e for e in journal.entries if e["kind"] == "powercfg_index"]
    assert len(entries) == 1
    assert entries[0]["prev_ac"] == 5
    assert entries[0]["prev_dc"] == 50


def test_set_index_noop_writes_nothing(fake_shell, backend):
    from bloodlust.core.backup import UndoJournal

    journal = UndoJournal("t", backend)
    shell = fake_shell.factory(journal, False)
    shell.responses["/q "] = (0, _AC_DC_OUTPUT, "")
    changes = PowerCfg(shell, journal).set_index("SUB", "SET", ac=5, dc=50, describe="t")
    assert changes == []
    assert shell.calls_containing("/setacvalueindex") == []
    assert journal.entries == []


def test_set_index_failure_raises(executor, fake_shell):
    fake_shell.responses["/q "] = (0, _AC_DC_OUTPUT, "")
    fake_shell.responses["/setacvalueindex"] = (1, "", "access denied")
    with pytest.raises(errors.RpcError) as exc:
        executor.run("power_min_processor_state")
    assert exc.value.code == errors.ACTION_FAILED


# ------------------------------------------------------- power scheme switching


def test_performance_scheme_activates(executor, fake_shell):
    fake_shell.responses["/getactivescheme"] = (
        0, "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)", "")
    fake_shell.responses["/list"] = (
        0, "Power Scheme GUID: e9a42b02-d5df-448d-aa66-8f4416fc4484  (Ultimate Performance)", "")

    result = executor.run("power_scheme_performance")
    assert result["ok"] is True
    actives = fake_shell.all_calls
    assert any("/setactive" in c and "e9a42b02-d5df-448d-aa66-8f4416fc4484" in c for c in actives)


def test_scheme_already_active_is_a_noop(executor, fake_shell):
    fake_shell.responses["/getactivescheme"] = (
        0, "Power Scheme GUID: e9a42b02-d5df-448d-aa66-8f4416fc4484  (Ultimate Performance)", "")
    fake_shell.responses["/list"] = (
        0, "Power Scheme GUID: e9a42b02-d5df-448d-aa66-8f4416fc4484  (Ultimate Performance)", "")

    result = executor.run("power_scheme_performance")
    assert result["status"] == "skipped"
    assert not any("/setactive" in c for c in fake_shell.all_calls)


def test_scheme_switch_records_undo(executor, fake_shell):
    fake_shell.responses["/getactivescheme"] = (
        0, "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)", "")
    fake_shell.responses["/list"] = (
        0, "Power Scheme GUID: e9a42b02-d5df-448d-aa66-8f4416fc4484  (Ultimate Performance)", "")

    executor.run("power_scheme_performance")
    assert any(e["action_id"] == "power_scheme_performance" for e in executor.list_undo())


# ------------------------------------------------------------------ registry


def test_mmcss_writes_the_expected_values(executor, backend):
    executor.run("win_mmcss_games")
    mmcs = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
    games = mmcs + r"\Tasks\Games"

    assert backend.read("HKLM", mmcs, "SystemResponsiveness") == (reg.REG_DWORD, 10)
    assert backend.read("HKLM", mmcs, "NetworkThrottlingIndex") == (reg.REG_DWORD, 0xFFFFFFFF)
    assert backend.read("HKLM", games, "GPU Priority") == (reg.REG_DWORD, 8)
    assert backend.read("HKLM", games, "Priority") == (reg.REG_DWORD, 6)
    assert backend.read("HKLM", games, "Scheduling Category") == (reg.REG_SZ, "High")
    assert backend.read("HKLM", games, "SFIO Priority") == (reg.REG_SZ, "High")


def test_pointer_settings_are_written_as_strings(executor, backend):
    """These are REG_SZ in Windows; writing DWORD makes them silently not apply."""
    executor.run("win_pointer_raw")
    for name in ("MouseSpeed", "MouseThreshold1", "MouseThreshold2"):
        hit = backend.read("HKCU", r"Control Panel\Mouse", name)
        assert hit is not None, f"{name} was not written"
        assert hit[0] == reg.REG_SZ, f"{name} must be REG_SZ, got {hit[0]}"
        assert hit[1] == "0"


def test_game_dvr_undo_removes_the_policy_key(executor, backend):
    executor.run("win_game_dvr_off")
    key = r"SOFTWARE\Policies\Microsoft\Windows\GameDVR"
    assert backend.read("HKLM", key, "AllowGameDVR") == (reg.REG_DWORD, 0)

    executor.undo("win_game_dvr_off")
    assert backend.read("HKCU", r"Software\Microsoft\Windows\CurrentVersion\GameDVR",
                        "AppCaptureEnabled") is None


def test_gpu_preference_uses_the_detected_exe_not_a_catalog_path(executor, backend):
    """A remote catalog must not be able to point this at an arbitrary binary."""
    executor.run("gpu_route_emulator_to_dgpu")
    written = [
        (k, v) for k, v in backend.snapshot().get(
            r"HKCU\Software\Microsoft\DirectX\UserGpuPreferences", {}
        ).items()
    ]
    assert written, "no GPU preference was written"
    for path, value in written:
        assert "AndroidEmulator.exe" in path
        assert value[1].startswith("GpuPreference=")


def test_gpu_preference_is_blocked_when_the_emulator_is_absent(
    executor, backend, fake_emulators
):
    fake_emulators["gameloop"].installed = False
    with pytest.raises(errors.RpcError) as exc:
        executor.run("gpu_route_emulator_to_dgpu")
    assert exc.value.code == errors.ACTION_BLOCKED


# --------------------------------------------------------------------- network


def test_tcp_autotuning_records_the_previous_level(executor, fake_shell):
    fake_shell.responses["show global"] = (
        0, "Receive Window Auto-Tuning Level    : disabled\n", "")

    result = executor.run("net_tcp_autotuning_normal")
    assert result["ok"] is True
    assert any("autotuninglevel=normal" in " ".join(c) for c in fake_shell.all_calls)


def test_tcp_autotuning_undo_restores_disabled(executor, fake_shell):
    fake_shell.responses["show global"] = (
        0, "Receive Window Auto-Tuning Level    : disabled\n", "")
    executor.run("net_tcp_autotuning_normal")
    executor.undo("net_tcp_autotuning_normal")
    assert any(
        "autotuninglevel=disabled" in " ".join(c) for c in fake_shell.all_calls
    ), "undo did not restore the previous autotuning level"


def test_tcp_autotuning_already_correct_is_skipped(executor, fake_shell):
    fake_shell.responses["show global"] = (
        0, "Receive Window Auto-Tuning Level    : normal\n", "")
    result = executor.run("net_tcp_autotuning_normal")
    assert result["status"] == "skipped"


# ------------------------------------------------------------------------- adb


def test_adb_rejects_a_key_outside_the_allowlist(executor):
    with pytest.raises(errors.RpcError) as exc:
        executor.run("adb_touches_off", {"namespace": "global", "key": "adb_enabled", "value": "1"})
    assert "not in the allowlist" in exc.value.message


def test_adb_rejects_an_out_of_range_value(executor):
    with pytest.raises(errors.RpcError) as exc:
        executor.run("adb_touches_off", {"namespace": "system", "key": "show_touches", "value": "maybe"})
    assert "not permitted" in exc.value.message


def test_adb_allowlist_has_no_dangerous_keys():
    """Guard against someone widening the allowlist without thinking."""
    forbidden = {"adb_enabled", "development_settings_enabled", "package_verifier_user_consent"}
    for namespace, keys in ALLOWED_SETTINGS.items():
        for key in keys:
            assert key not in forbidden, f"{namespace}/{key} should not be ADB-settable"


# --------------------------------------------------------------------- audits


def test_audit_report_is_read_only(executor, backend, fake_shell):
    before = backend.snapshot()
    calls_before = len(fake_shell.all_calls)
    result = executor.run("audit_report")
    assert result["ok"] is True
    assert backend.snapshot() == before
    assert len(fake_shell.all_calls) == calls_before
    assert not any(e["action_id"] == "audit_report" for e in executor.list_undo())


def test_audit_report_lists_detected_emulators(executor):
    result = executor.run("audit_report")
    targets = [c["target"] for c in result["changes"]]
    assert any("GameLoop" in t for t in targets)


def test_explain_blocks_lists_the_reason(executor, fake_windows_hardware):
    fake_windows_hardware.is_elevated = False
    result = executor.run("audit_explain_blocks", {})
    assert result["ok"] is True


def test_explain_blocks_reports_which_requirement_failed(executor, fake_windows_hardware):
    """Regression: ctx.state was never populated, so this always said 'all available'."""
    fake_windows_hardware.is_elevated = False
    result = executor.run("audit_explain_blocks")
    details = [c["detail"] for c in result["changes"]]
    assert details, "no blocked actions were reported despite elevation being off"
    assert any("administrator" in d for d in details)


def test_explain_blocks_only_reports_real_mismatches(executor):
    """The fake machine is Intel + NVIDIA, so AMD-only rows are legitimately locked.

    What must not happen is a block with no concrete reason, or a block on an
    action whose requirements this hardware actually satisfies.
    """
    result = executor.run("audit_explain_blocks")
    details = [c["detail"] for c in result["changes"]]
    assert details, "expected the AMD-only rows to be reported as blocked"
    assert any("requires an amd GPU" in d for d in details)
    assert not any("Could not verify" in d for d in details)
