"""Sidecar entry point.

    bloodlust-sidecar.exe            serve JSON-RPC on stdio
    bloodlust-sidecar.exe --version  print version and exit
    bloodlust-sidecar.exe --selftest load the catalog and report, no RPC

``--selftest`` is what the installer and CI call to prove a freshly built
binary can actually find its catalog and enumerate its handlers, before anyone
ships it.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--version" in argv:
        from .version import APP_NAME, APP_VERSION, PROTOCOL_VERSION

        # Version output goes to stderr: stdout is reserved for JSON-RPC, even
        # here, so a caller cannot accidentally desynchronise a stream.
        print(
            f"{APP_NAME} sidecar {APP_VERSION} (protocol {PROTOCOL_VERSION})",
            file=sys.stderr,
        )
        return 0

    from .core.logging import setup
    from .rpc.server import Dispatcher, Server

    log = setup()

    try:
        from .app import App

        app = App()
    except Exception as exc:  # noqa: BLE001 - report, do not traceback to the pipe
        log.exception("failed to start")
        print(f"sidecar failed to start: {exc}", file=sys.stderr)
        return 1

    if "--selftest" in argv:
        return _selftest(app)

    dispatcher = app.register(Dispatcher())
    return Server(dispatcher).serve()


def _selftest(app) -> int:
    out = sys.stderr
    problems: list[str] = []

    print(f"app        : {app.state.get('__unused__', '') or 'ok'}", file=out)
    print(f"handlers   : {len(app.handlers)}", file=out)

    if app.catalog is None or not app.catalog.actions:
        problems.append("no catalog actions loaded")
        print("catalog    : FAILED - no actions", file=out)
    else:
        prov = app.catalog.provenance
        print(
            f"catalog    : {len(app.catalog.actions)} actions, "
            f"{len(app.catalog.families)} families, source={prov.source}",
            file=out,
        )

    # Every catalog handler must resolve; every loaded handler should be used.
    if app.catalog:
        missing = [
            s.id for s in app.catalog.actions.values() if s.handler not in app.handlers
        ]
        unused = sorted(set(app.handlers) - {s.handler for s in app.catalog.actions.values()})
        if missing:
            problems.append(f"actions reference missing handlers: {missing}")
        print(f"unused     : {len(unused)} handler(s) not referenced by the catalog", file=out)

    blocked = app._blocks()
    print(f"blocked    : {len(blocked)} action(s) unavailable on this machine", file=out)
    for b in blocked[:10]:
        print(f"             - {b['id']}: {b['reason']}", file=out)

    if app.hw.registry_backend != "winreg":
        print(
            "registry   : MEMORY backend (not Windows) - tweaks will not persist",
            file=out,
        )

    if problems:
        print("SELFTEST FAILED:", file=out)
        for p in problems:
            print(f"  - {p}", file=out)
        return 1

    print("SELFTEST OK", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
