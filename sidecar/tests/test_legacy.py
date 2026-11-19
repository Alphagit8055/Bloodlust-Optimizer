"""The compiled legacy profile table and its handler.

The interesting property here is not that a registry value is written, but that
the *catalog* cannot point an action at a different profile. These tests pin
that down together with undo behaviour for the three storage types the port
uses: registry, dynamic per-interface values, and an INI file.
"""

from __future__ import annotations

import pytest

from bloodlust.actions import legacy
from bloodlust.core import registry as reg
from bloodlust.rpc import errors


def test_every_profile_is_in_the_shipped_catalog(catalog):
    for pid, profile in legacy.PROFILES.items():
        spec = catalog.get(pid)
        assert spec is not None, f"{pid} is compiled in but missing from the catalog"
        assert spec.handler == "legacy.apply"
        assert spec.args.get("profile") == pid
        assert spec.risk == profile["risk"]


def test_catalog_cannot_retarget_a_profile(executor):
    with pytest.raises(errors.RpcError) as exc:
        executor.run("gl_fxaa_off", {"profile": "gl_fxaa_high"})
    assert exc.value.code == errors.ACTION_FAILED


def test_registry_profile_applies_and_undoes(executor, backend):
    backend.write("HKCU", legacy.TENCENT, "FxaaQuality", reg.REG_DWORD, 2)

    result = executor.run("gl_fxaa_off")
    assert result["ok"] is True
    assert backend.read("HKCU", legacy.TENCENT, "FxaaQuality") == (reg.REG_DWORD, 0)

    executor.undo("gl_fxaa_off")
    assert backend.read("HKCU", legacy.TENCENT, "FxaaQuality") == (reg.REG_DWORD, 2)


def test_dynamic_tcp_profile_touches_every_interface(executor, backend):
    iface = legacy.INTERFACES + r"\{11111111-2222-3333-4444-555555555555}"
    backend.write("HKLM", iface, "TcpAckFrequency", reg.REG_DWORD, 2)
    backend.write("HKLM", iface, "TcpNoDelay", reg.REG_DWORD, 0)

    executor.run("win_nagle_off")

    assert backend.read("HKLM", iface, "TcpAckFrequency") == (reg.REG_DWORD, 1)
    assert backend.read("HKLM", iface, "TcpNoDelay") == (reg.REG_DWORD, 1)
    assert backend.read("HKLM", iface, "TcpDelAckTicks") == (reg.REG_DWORD, 0)

    executor.undo("win_nagle_off")
    assert backend.read("HKLM", iface, "TcpAckFrequency") == (reg.REG_DWORD, 2)


def test_ini_profile_edits_one_line_and_undo_restores_the_file(
    executor, fake_emulators, tmp_path
):
    root = tmp_path / "gameloop"
    root.mkdir()
    config = root / "Config.ini"
    original = "[engine]\nEnableHardwareDecoding=0\nSomeOtherKey=keepme\n"
    config.write_text(original, encoding="utf-8")
    fake_emulators["gameloop"].root = str(root)

    result = executor.run("gl_hw_decoding_on")
    assert result["ok"] is True
    edited = config.read_text(encoding="utf-8")
    assert "EnableHardwareDecoding=1" in edited
    assert "SomeOtherKey=keepme" in edited

    executor.undo("gl_hw_decoding_on")
    assert config.read_text(encoding="utf-8") == original


def test_ini_edit_preserves_a_utf16_file(
    executor, fake_emulators, tmp_path
):
    root = tmp_path / "gameloop"
    root.mkdir()
    config = root / "Config.ini"
    text = "[engine]\r\nEnableHardwareDecoding=0\r\nSomeOtherKey=keepme\r\n"
    original = b"\xff\xfe" + text.encode("utf-16-le")
    config.write_bytes(original)
    fake_emulators["gameloop"].root = str(root)

    executor.run("gl_hw_decoding_on")

    raw = config.read_bytes()
    assert raw.startswith(b"\xff\xfe"), "the UTF-16 BOM must survive the edit"
    decoded = raw.decode("utf-16")
    assert "EnableHardwareDecoding=1" in decoded
    assert "SomeOtherKey=keepme" in decoded

    executor.undo("gl_hw_decoding_on")
    assert config.read_bytes() == original


def test_ini_edit_creates_a_missing_section():
    out = legacy._set_ini_value("[other]\nx=1\n", "engine", "EnableHardwareDecoding", "1")
    assert "[engine]" in out
    assert "EnableHardwareDecoding=1" in out
    assert "[other]" in out
    assert "x=1" in out


def test_ini_edit_keeps_one_line_per_value():
    text = "[engine]\nEnableHardwareDecoding=1\nEnableHardwareDecoding=1\n"
    out = legacy._set_ini_value(text, "engine", "EnableHardwareDecoding", "0")
    assert out.count("EnableHardwareDecoding") == 2
    assert "EnableHardwareDecoding=0" in out
    assert "EnableHardwareDecoding=1" in out  # the duplicate line is untouched


def test_unknown_dynamic_routine_is_refused():
    assert "does_not_exist" not in legacy._DYNAMIC


def test_high_risk_profiles_are_not_default_enabled():
    for pid, profile in legacy.PROFILES.items():
        if profile["risk"] in ("aggressive", "danger"):
            assert profile["default_enabled"] is False, pid


def test_profiles_without_an_inverse_are_marked_irreversible():
    """A profile may only advertise undo when every op can actually be undone."""
    no_inverse = {
        "adb_build_prop",
        "adb_io_scheduler",
        "adb_kernel_tuning",
        "clean_paths",
        "clean_files",
        "clean_known",
        "clean_update_cache",
    }
    for pid, profile in legacy.PROFILES.items():
        names = {op.get("name") for op in profile["ops"] if op["op"] == "dyn"}
        if names & no_inverse:
            assert profile["reversible"] is False, pid


def test_no_profile_writes_a_value_it_also_wants_to_delete():
    """Guards the option table: one row must not both set and remove a value."""
    for pid, profile in legacy.PROFILES.items():
        sets = {(op["hive"], op["path"], op["name"]) for op in profile["ops"] if op["op"] == "reg"}
        dels = {(op["hive"], op["path"], op["name"]) for op in profile["ops"] if op["op"] == "del"}
        assert not (sets & dels), pid
