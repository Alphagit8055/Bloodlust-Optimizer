"""Handler registry and the catalog allowlist.

``allowlist()`` is the set of handler names this *build* can execute. It is
computed from the handler tables rather than written by hand, so the two can
never disagree, and it is passed to ``core.catalog.validate`` on every load.

That is the enforcement point for the central security rule: a catalog fetched
from GitHub can only select and configure handlers that already ship inside the
binary.
"""

from __future__ import annotations

from typing import Callable

from . import adb, audit, cleanup, gameloop, network, nvidia, power, windows

_MODULES = (power, network, nvidia, windows, gameloop, adb, audit, cleanup)


def all_handlers() -> dict[str, Callable]:
    handlers: dict[str, Callable] = {}
    for module in _MODULES:
        for name, fn in getattr(module, "HANDLERS", {}).items():
            if name in handlers:
                # A collision means two families claim the same handler name.
                # Fail loudly at import rather than letting one shadow the other.
                raise RuntimeError(f"duplicate handler name {name!r}")
            handlers[name] = fn
    return handlers


def allowlist() -> set[str]:
    return set(all_handlers())


def describe_handlers() -> list[dict]:
    """Metadata for the developer docs and the debug console."""
    out = []
    for name, fn in sorted(all_handlers().items()):
        out.append(
            {
                "handler": name,
                "doc": (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else "",
                "args": {
                    k: {
                        "type": v.get("type").__name__ if v.get("type") else None,
                        "default": v.get("default"),
                        "choices": list(v["choices"]) if v.get("choices") else None,
                        "min": v.get("min"),
                        "max": v.get("max"),
                    }
                    for k, v in (getattr(fn, "arg_schema", None) or {}).items()
                },
            }
        )
    return out


__all__ = ["all_handlers", "allowlist", "describe_handlers"]
