"""Logging for the sidecar.

CRITICAL INVARIANT: stdout is the JSON-RPC transport. A single stray ``print``
here corrupts every subsequent message the Rust shell reads. So the logger is
built with stdout structurally unavailable: handlers are stderr + file only,
and ``core.logging`` exposes no print helper at all.

There is a regression test (``test_rpc.py::test_stdout_stays_clean``) that
asserts this holds after a full round trip.
"""

from __future__ import annotations

import logging
import logging.handlers
from typing import Optional

from . import paths

_LOGGER_NAME = "bloodlust"
_configured = False


def setup(level: int = logging.INFO) -> logging.Logger:
    global _configured
    log = logging.getLogger(_LOGGER_NAME)
    log.setLevel(level)
    log.propagate = False  # never let records reach the root handler

    if _configured:
        return log

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # stderr: captured by the Rust shell and surfaced in the devtools console.
    err = logging.StreamHandler()
    err.setFormatter(fmt)
    log.addHandler(err)

    # file: rotating, because this runs unattended during gaming sessions.
    try:
        fh = logging.handlers.RotatingFileHandler(
            paths.log_dir() / "engine.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        # A read-only data dir must not stop the engine; stderr still works.
        log.warning("could not open log file, continuing with stderr only")

    _configured = True
    return log


def get() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)


class _TailBuffer(logging.Handler):
    """In-memory ring buffer so the UI can show recent logs without a file read."""

    def __init__(self, capacity: int = 200) -> None:
        super().__init__()
        self.capacity = capacity
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        self.records.append(entry)
        if len(self.records) > self.capacity:
            del self.records[: len(self.records) - self.capacity]

    def tail(self, n: Optional[int] = None) -> list[dict]:
        return self.records[-n:] if n else list(self.records)


_TAIL: Optional[_TailBuffer] = None


def tail_buffer(capacity: int = 200) -> _TailBuffer:
    global _TAIL
    if _TAIL is None:
        _TAIL = _TailBuffer(capacity)
        get().addHandler(_TAIL)
    return _TAIL


__all__ = ["setup", "get", "tail_buffer"]
