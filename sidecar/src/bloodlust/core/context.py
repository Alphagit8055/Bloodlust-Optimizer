"""The object every action handler receives.

Handlers get ``ctx`` and nothing else. That is the whole capability surface:
a handler can read hardware info, mutate the registry through the journaling
facade, and run argv commands. It cannot import the dispatcher, cannot reach
the network, and cannot touch stdout.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from . import registry as reg
from .backup import UndoJournal
from .mutate import RegistryFacade, Shell


@dataclass
class ActionContext:
    reg: RegistryFacade
    shell: Shell
    hw: Any                      # detect.hardware.HardwareInfo
    emulators: dict              # key -> detect.emulators.EmulatorInfo
    log: logging.Logger
    preview: bool
    action_id: str
    backend: reg.RegistryBackend
    journal: UndoJournal
    state: Optional[dict] = None

    @property
    def is_real_registry(self) -> bool:
        return isinstance(self.backend, reg.WinregBackend)

    def emulator(self, key: str = "gameloop"):
        return self.emulators.get(key)


__all__ = ["ActionContext"]
