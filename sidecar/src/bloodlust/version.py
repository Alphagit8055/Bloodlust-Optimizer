"""Single source of truth for version metadata.

Kept dependency-free so every layer (rpc, core, build scripts) can import it
without pulling in the rest of the package.
"""

from __future__ import annotations

APP_NAME = "Bloodlust Optimizer"
APP_SHORT = "Bloodlust"
APP_VERSION = "0.1.0"
DEVELOPER = "Telegram: @Alpha0NemesiSS"
DEVELOPER_HANDLE = "@Alpha0NemesiSS"

# Bumped whenever the wire protocol between the Rust shell and this sidecar
# changes incompatibly. The shell refuses to talk to a sidecar whose
# PROTOCOL_VERSION it does not recognise.
PROTOCOL_VERSION = 1

# Bumped when the catalog schema changes.
CATALOG_SCHEMA_VERSION = 1

__all__ = [
    "APP_NAME",
    "APP_SHORT",
    "APP_VERSION",
    "DEVELOPER",
    "DEVELOPER_HANDLE",
    "PROTOCOL_VERSION",
    "CATALOG_SCHEMA_VERSION",
]
