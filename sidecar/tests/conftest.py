"""Shared fixtures.

Every test runs against ``MemoryBackend`` and a throwaway data dir, so the
suite exercises the real code paths — journaling, gating, undo — without
touching a registry or needing Windows.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make the package importable from a source checkout.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

CATALOG_PATH = Path(__file__).resolve().parents[2] / "catalog" / "catalog.json"


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Point every engine path at a temp dir, per test."""
    monkeypatch.setenv("BLOODLUST_DATA_DIR", str(tmp_path / "data"))
    # The logging module caches its configuration across imports.
    import bloodlust.core.logging as logging_mod

    logging_mod._configured = False
    logging_mod._TAIL = None
    yield tmp_path


class FakeShell:
    """Stand-in for the command runner.

    Records every argv it is asked to run and returns canned output, so
    powercfg/netsh/adb handlers can be tested without Windows. Also the reason
    ``PowerCfg`` parsing is covered: the canned strings below are real
    ``powercfg`` output shapes.
    """

    def __init__(self, journal, preview=False, responses=None):
        self.journal = journal
        self.preview = preview
        self.calls: list[list[str]] = []
        self.responses = responses or {}
        self.default_rc = 0

    def run(self, argv, *, timeout=60, describe="", reversible=True, record=True):
        self.calls.append(list(argv))

        class Outcome:
            def __init__(o, rc, out, err):
                o.returncode, o.stdout, o.stderr = rc, out, err
                o.argv, o.skipped = argv, False

            @property
            def ok(o):
                return o.returncode == 0

        joined = " ".join(argv)
        for needle, (rc, out, err) in self.responses.items():
            if needle in joined:
                return Outcome(rc, out, err)
        return Outcome(self.default_rc, "", "")

    def record_restore(self, argv, *, describe=""):
        self.journal.entries.append(
            {"kind": "command_restore", "restore_argv": argv, "describe": describe}
        )

    def change_for(self, argv, describe="", *, reversible=True):
        raise NotImplementedError

    def calls_containing(self, needle: str) -> list[list[str]]:
        return [c for c in self.calls if needle in " ".join(c)]


@pytest.fixture
def fake_shell():
    """Registry of shells created during a test, so assertions can inspect them."""

    class Holder:
        def __init__(self):
            self.shells: list[FakeShell] = []
            self.responses: dict = {}

        def factory(self, journal, preview):
            shell = FakeShell(journal, preview, dict(self.responses))
            self.shells.append(shell)
            return shell

        @property
        def last(self):
            return self.shells[-1]

        @property
        def all_calls(self):
            return [c for s in self.shells for c in s.calls]

    return Holder()


@pytest.fixture(autouse=True)
def pretend_windows(monkeypatch):
    """The app targets Windows only, and most gates start with ``platform: win32``.

    Without this, every Windows action is blocked at the first requirement and
    the tests would exercise nothing but the platform gate. Patching the
    indirection point rather than ``sys.platform`` keeps the rest of the stdlib
    behaving normally.
    """
    monkeypatch.setattr("bloodlust.core.risk.current_platform", lambda: "win32")
    yield


@pytest.fixture
def backend():
    from bloodlust.core.registry import MemoryBackend

    return MemoryBackend()


@pytest.fixture
def catalog_payload():
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def allowlist():
    from bloodlust import actions

    return actions.allowlist()


@pytest.fixture
def fake_windows_hardware():
    """A HardwareInfo that looks like a real gaming laptop with GameLoop."""
    from bloodlust.detect.hardware import Gpu, HardwareInfo

    return HardwareInfo(
        os_name="Windows",
        os_version="10.0.22631",
        os_build=22631,
        arch="AMD64",
        cpu_name="13th Gen Intel(R) Core(TM) i5-13450HX",
        cpu_vendor="intel",
        cpu_generation=13,
        cpu_cores=10,
        cpu_threads=16,
        cpu_max_mhz=2400,
        gpus=[
            Gpu(name="Intel(R) UHD Graphics", vendor="intel", vram_mb=128, is_discrete=False),
            Gpu(name="NVIDIA GeForce RTX 4050 Laptop GPU", vendor="nvidia", vram_mb=6144, is_discrete=True),
        ],
        primary_gpu="NVIDIA GeForce RTX 4050 Laptop GPU",
        ram_mb=16384,
        hypervisor_present=False,
        vbs_status=0,
        is_elevated=True,
        registry_backend="memory",
    )


@pytest.fixture
def fake_emulators():
    from bloodlust.detect.emulators import EmulatorInfo

    return {
        "gameloop": EmulatorInfo(
            key="gameloop",
            name="GameLoop",
            installed=True,
            root=r"C:\Program Files\TxGameAssistant",
            exe=r"C:\Program Files\TxGameAssistant\MobileGamePCShared\AndroidEmulator.exe",
            adb=r"C:\Program Files\TxGameAssistant\MobileGamePCShared\adb.exe",
            source="known-path",
        ),
        "mumu": EmulatorInfo(key="mumu", name="MuMu Player"),
        "ldplayer": EmulatorInfo(key="ldplayer", name="LDPlayer"),
    }


@pytest.fixture
def catalog(catalog_payload, allowlist):
    from bloodlust.core import catalog as catalog_mod

    return catalog_mod.validate(catalog_payload, allowlist, source="bundled")


@pytest.fixture
def executor(catalog, backend, fake_windows_hardware, fake_emulators, fake_shell):
    from bloodlust import actions
    from bloodlust.core.executor import Executor

    return Executor(
        catalog,
        actions.all_handlers(),
        fake_windows_hardware,
        fake_emulators,
        risk_ceiling="danger",  # tests exercise the full range deliberately
        backend=backend,
        shell_factory=fake_shell.factory,
    )
