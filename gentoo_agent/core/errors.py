"""Typed errors with stable codes; front ends turn them into text or MCP results."""

from __future__ import annotations


CONFIG_INVALID = "CONFIG_INVALID"
CONFIG_CONFLICT = "CONFIG_CONFLICT"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
PHASE_FAILED = "PHASE_FAILED"
HANDBOOK_UNAVAILABLE = "HANDBOOK_UNAVAILABLE"
INVENTORY_MISSING = "INVENTORY_MISSING"
TARGET_DRIFT = "TARGET_DRIFT"
SSH_UNREACHABLE = "SSH_UNREACHABLE"
DISK_AMBIGUOUS = "DISK_AMBIGUOUS"
PLAN_NOT_FOUND = "PLAN_NOT_FOUND"
PLAN_HASH_MISMATCH = "PLAN_HASH_MISMATCH"


class AgentError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def __reduce__(self):
        return (type(self), (self.code, self.message, self.details))

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}
