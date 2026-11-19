"""JSON-RPC 2.0 error codes plus the application-specific ones."""

from __future__ import annotations

# Reserved by the specification.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Application range (-32000 .. -32099).
NOT_READY = -32001          # called before catalog.load
ACTION_NOT_FOUND = -32002   # id not in the loaded catalog
ACTION_BLOCKED = -32003     # hardware/OS requirements not met
ACTION_FAILED = -32004      # handler raised or a command failed
NO_UNDO = -32005            # nothing recorded to undo for that action
NEEDS_ELEVATION = -32006    # action requires admin and we are not elevated
CATALOG_INVALID = -32007    # catalog failed schema or allowlist validation
PROTOCOL_MISMATCH = -32008  # shell and sidecar disagree on PROTOCOL_VERSION
SESSION_ACTIVE = -32009     # a boost session already holds the system
LICENSE_REQUIRED = -32090   # reserved; licensing is not implemented yet


class RpcError(Exception):
    """An error the caller should see as a JSON-RPC error object."""

    def __init__(self, code: int, message: str, data=None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out


__all__ = [
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "NOT_READY",
    "ACTION_NOT_FOUND",
    "ACTION_BLOCKED",
    "ACTION_FAILED",
    "NO_UNDO",
    "NEEDS_ELEVATION",
    "CATALOG_INVALID",
    "PROTOCOL_MISMATCH",
    "SESSION_ACTIVE",
    "LICENSE_REQUIRED",
    "RpcError",
]
