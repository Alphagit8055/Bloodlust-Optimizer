"""Gating, risk ceiling, argument validation, batch and undo orchestration."""

from __future__ import annotations

import pytest

from bloodlust.core import catalog as cm
from bloodlust.core import risk as risk_mod
from bloodlust.detect.hardware import Gpu, HardwareInfo
from bloodlust.rpc import errors


# ---------------------------------------------------------------------- gating


def test_elevated_action_is_blocked_without_admin(executor, fake_windows_hardware):
    fake_windows_hardware.is_elevated = False
    gate = executor.gate_for("power_scheme_performance")
    assert gate.available is False
    assert "administrator" in gate.reason


def test_elevated_action_is_available_with_admin(executor):
    assert executor.gate_for("power_scheme_performance").available is True


def test_unknown_vendor_locks_vendor_specific_actions(executor, fake_windows_hardware):
    """A mis-detect must cost the user a tweak, never their machine."""
    fake_windows_hardware.cpu_vendor = None
    fake_windows_hardware.cpu_generation = None
    gate = executor.gate_for("gpu_report_driver")  # requires gpu_vendor=nvidia
    assert gate.available is True  # vendor unknown here is the GPU, still set

    fake_windows_hardware.gpus = [Gpu(name="Unknown Display", vendor=None, vram_mb=None)]
    gate = executor.gate_for("gpu_report_driver")
    assert gate.available is False
    assert "Could not verify" in gate.reason or "undetermined" in gate.reason


def test_gpu_vendor_mismatch_blocks(executor, fake_windows_hardware):
    fake_windows_hardware.gpus = [
        Gpu(name="AMD Radeon RX 7600", vendor="amd", vram_mb=8192, is_discrete=True)
    ]
    gate = executor.gate_for("gpu_report_driver")
    assert gate.available is False
    assert "nvidia" in gate.reason


def test_missing_emulator_blocks_its_actions(executor, fake_emulators):
    fake_emulators["gameloop"].installed = False
    gate = executor.gate_for("gl_clear_cache")
    assert gate.available is False
    assert "not installed" in gate.reason


def test_platform_requirement_blocks_off_windows(executor, monkeypatch):
    monkeypatch.setattr("bloodlust.core.risk.current_platform", lambda: "linux")
    gate = executor.gate_for("power_scheme_performance")
    assert gate.available is False
    assert "win32" in gate.reason


def test_generation_range_gating():
    spec = cm.ActionSpec(
        id="t", label="t", family="f", risk="safe", description="",
        handler="h", requires={"cpu_generation_min": 12, "cpu_generation_max": 14},
    )
    hw = HardwareInfo(cpu_vendor="intel", cpu_generation=13, os_build=22631, is_elevated=True)
    assert risk_mod.evaluate(spec, hw, {}).available is True

    hw.cpu_generation = 8
    assert risk_mod.evaluate(spec, hw, {}).available is False

    hw.cpu_generation = 15
    assert risk_mod.evaluate(spec, hw, {}).available is False


def test_unknown_generation_blocks_rather_than_allows():
    spec = cm.ActionSpec(
        id="t", label="t", family="f", risk="safe", description="",
        handler="h", requires={"cpu_generation_max": 14},
    )
    hw = HardwareInfo(cpu_vendor="intel", cpu_generation=None, os_build=22631, is_elevated=True)
    gate = risk_mod.evaluate(spec, hw, {})
    assert gate.available is False
    assert gate.unknown, "an undetermined generation must be reported as unknown"


def test_hypervisor_gate_blocks_when_present(executor, fake_windows_hardware):
    spec = cm.ActionSpec(
        id="t", label="t", family="f", risk="safe", description="",
        handler="h", requires={"hypervisor_absent": True},
    )
    fake_windows_hardware.hypervisor_present = True
    gate = risk_mod.evaluate(spec, fake_windows_hardware, {})
    assert gate.available is False
    assert "hypervisor" in gate.reason

    fake_windows_hardware.hypervisor_present = False
    assert risk_mod.evaluate(spec, fake_windows_hardware, {}).available is True

    fake_windows_hardware.hypervisor_present = None
    gate = risk_mod.evaluate(spec, fake_windows_hardware, {})
    assert gate.available is False
    assert gate.unknown == ["hypervisor state"]


def test_no_requirements_means_available(executor):
    gate = executor.gate_for("audit_report")
    assert gate.available is True
    assert gate.reason == ""


# ---------------------------------------------------------------- risk ceiling


def test_ceiling_blocks_aggressive_in_safe_mode(executor):
    executor.risk_ceiling = "safe"
    with pytest.raises(errors.RpcError) as exc:
        executor.run("power_usb_suspend_off")
    assert exc.value.code == errors.ACTION_BLOCKED
    assert "ceiling" in exc.value.message


def test_ceiling_allows_aggressive_when_raised(executor):
    executor.risk_ceiling = "aggressive"
    result = executor.run("power_usb_suspend_off")
    assert result["ok"] is True


def test_ceiling_never_blocks_a_preview(executor):
    """Seeing what a tweak would do is always allowed."""
    executor.risk_ceiling = "safe"
    result = executor.preview("power_usb_suspend_off")
    assert result["status"] == "preview"


def test_risk_ordering_is_monotonic():
    order = cm.RISK_ORDER
    assert order["safe"] < order["moderate"] < order["aggressive"] < order["danger"]


def test_invalid_ceiling_is_rejected(catalog, backend, fake_windows_hardware, fake_emulators):
    from bloodlust import actions
    from bloodlust.core.executor import Executor

    with pytest.raises(ValueError):
        Executor(catalog, actions.all_handlers(), fake_windows_hardware,
                 fake_emulators, risk_ceiling="yolo", backend=backend)


# ------------------------------------------------------------ arg validation


def test_unknown_runtime_arg_is_rejected(executor):
    with pytest.raises(errors.RpcError) as exc:
        executor.run("power_scheme_performance", {"surprise": 1})
    assert exc.value.code == errors.INVALID_PARAMS


def test_wrong_arg_type_is_rejected(executor):
    with pytest.raises(errors.RpcError) as exc:
        executor.run("power_min_processor_state", {"value": "lots"})
    assert exc.value.code == errors.INVALID_PARAMS


def test_out_of_range_arg_is_rejected(executor):
    with pytest.raises(errors.RpcError) as exc:
        executor.run("power_min_processor_state", {"value": 999})
    assert "must be <=" in exc.value.message


def test_bad_choice_is_rejected(executor):
    with pytest.raises(errors.RpcError) as exc:
        executor.run("power_scheme_performance", {"scheme": "turbo_mega"})
    assert "must be one of" in exc.value.message


def test_runtime_arg_overrides_catalog_default(executor):
    result = executor.preview("power_scheme_performance", {"scheme": "high_performance"})
    assert result["ok"] is True


# --------------------------------------------------------------- run / undo


def test_unknown_action_raises(executor):
    with pytest.raises(errors.RpcError) as exc:
        executor.run("does_not_exist")
    assert exc.value.code == errors.ACTION_NOT_FOUND


def test_run_records_an_undo_entry(executor, backend):
    executor.run("win_game_dvr_off")
    assert any(e["action_id"] == "win_game_dvr_off" for e in executor.list_undo())


def test_undo_restores_and_clears_the_journal(executor, backend):
    executor.run("win_game_dvr_off")
    key = r"Software\Microsoft\Windows\CurrentVersion\GameDVR"
    assert backend.read("HKCU", key, "AppCaptureEnabled") is not None

    result = executor.undo("win_game_dvr_off")
    assert result["status"] == "undone"
    assert backend.read("HKCU", key, "AppCaptureEnabled") is None
    assert not any(e["action_id"] == "win_game_dvr_off" for e in executor.list_undo())


def test_undo_with_nothing_recorded_raises(executor):
    with pytest.raises(errors.RpcError) as exc:
        executor.undo("win_game_mode_on")
    assert exc.value.code == errors.NO_UNDO


def test_undo_bypasses_the_risk_ceiling(executor):
    """Taking the machine back must always be allowed."""
    executor.risk_ceiling = "aggressive"
    executor.run("power_usb_suspend_off")
    executor.risk_ceiling = "safe"
    result = executor.undo("power_usb_suspend_off")
    assert result["status"] == "undone"


def test_no_op_action_leaves_no_journal(executor):
    """An action that changed nothing must not look undoable."""
    result = executor.run("audit_report")
    assert result["status"] in ("applied", "skipped")
    assert not any(e["action_id"] == "audit_report" for e in executor.list_undo())


def test_undo_all_restores_everything(executor, backend):
    executor.run("win_game_dvr_off")
    executor.run("win_pointer_raw")
    results = executor.undo_all()
    assert len(results) == 2
    assert all(r["ok"] for r in results)
    assert executor.list_undo() == []


def test_describe_reports_gate_and_undo_state(executor):
    info = executor.describe("win_game_dvr_off")
    assert info["id"] == "win_game_dvr_off"
    assert info["gate"]["available"] is True
    assert info["has_undo"] is False

    executor.run("win_game_dvr_off")
    assert executor.describe("win_game_dvr_off")["has_undo"] is True


# --------------------------------------------------------------------- batch


def test_batch_safe_excludes_aggressive(executor, catalog):
    result = executor.run_batch("safe", preview=True)
    applied_ids = {r["action_id"] for r in result["results"]}
    for action_id in applied_ids:
        spec = catalog.actions[action_id]
        assert cm.RISK_ORDER[spec.risk] <= cm.RISK_ORDER["safe"], (
            f"{action_id} is risk={spec.risk} but ran in a safe batch"
        )


def test_batch_continues_past_a_blocked_action(executor, fake_windows_hardware):
    """One unavailable action must not abort the whole batch."""
    fake_windows_hardware.is_elevated = False
    result = executor.run_batch("safe", preview=True)
    assert result["total"] > 1
    assert result["failed"] > 0
    assert result["total"] - result["failed"] > 0


def test_batch_with_explicit_ids_rejects_unknown(executor):
    with pytest.raises(errors.RpcError) as exc:
        executor.run_batch("safe", ["win_game_mode_on", "nope"])
    assert exc.value.code == errors.ACTION_NOT_FOUND
    assert "nope" in exc.value.data["ids"]


def test_batch_with_explicit_ids_runs_only_those(executor):
    result = executor.run_batch("safe", ["win_game_mode_on", "win_visual_fx_performance"], preview=True)
    assert result["total"] == 2
    assert {r["action_id"] for r in result["results"]} == {
        "win_game_mode_on", "win_visual_fx_performance"
    }


def test_batch_invalid_mode(executor):
    with pytest.raises(errors.RpcError) as exc:
        executor.run_batch("turbo")
    assert exc.value.code == errors.INVALID_PARAMS


# ------------------------------------------------------------ handler errors


def test_handler_exception_becomes_a_clean_failure(catalog, backend, fake_windows_hardware, fake_emulators):
    """A raising handler must not take the session down."""
    from bloodlust.core.executor import Executor

    def explode(ctx, args):
        raise RuntimeError("simulated driver failure")

    handlers = {"boom.handler": explode}
    cat = cm.Catalog(
        actions={
            "boom": cm.ActionSpec(
                id="boom", label="Boom", family="audit", risk="safe",
                description="", handler="boom.handler",
            )
        },
        families={"audit": cm.Family(id="audit", label="Audit")},
        provenance=cm.Provenance(),
    )
    ex = Executor(cat, handlers, fake_windows_hardware, fake_emulators,
                  risk_ceiling="danger", backend=backend)
    with pytest.raises(errors.RpcError) as exc:
        ex.run("boom")
    assert exc.value.code == errors.ACTION_FAILED
    assert "simulated driver failure" in exc.value.message


def test_handler_returning_garbage_is_reported(catalog, backend, fake_windows_hardware, fake_emulators):
    from bloodlust.core.executor import Executor

    handlers = {"bad.handler": lambda ctx, args: 42}
    cat = cm.Catalog(
        actions={
            "bad": cm.ActionSpec(
                id="bad", label="Bad", family="audit", risk="safe",
                description="", handler="bad.handler",
            )
        },
        families={"audit": cm.Family(id="audit", label="Audit")},
        provenance=cm.Provenance(),
    )
    ex = Executor(cat, handlers, fake_windows_hardware, fake_emulators,
                  risk_ceiling="danger", backend=backend)
    with pytest.raises(errors.RpcError) as exc:
        ex.run("bad")
    assert "expected ActionResult" in exc.value.message


def test_handler_can_return_a_bare_change_list(catalog, backend, fake_windows_hardware, fake_emulators):
    from bloodlust.core.executor import Executor
    from bloodlust.core.result import Change, ChangeKind

    def simple(ctx, args):
        return [Change(kind=ChangeKind.NOTE, target="x", detail="y", reversible=False)]

    handlers = {"simple.handler": simple}
    cat = cm.Catalog(
        actions={
            "simple": cm.ActionSpec(
                id="simple", label="Simple", family="audit", risk="safe",
                description="", handler="simple.handler",
            )
        },
        families={"audit": cm.Family(id="audit", label="Audit")},
        provenance=cm.Provenance(),
    )
    ex = Executor(cat, handlers, fake_windows_hardware, fake_emulators,
                  risk_ceiling="danger", backend=backend)
    result = ex.run("simple")
    assert result["ok"] is True
    assert len(result["changes"]) == 1
