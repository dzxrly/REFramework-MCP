"""Structured errors shared by the MCP tools and bridge client."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    BRIDGE_DISCONNECTED = "BRIDGE_DISCONNECTED"
    BRIDGE_PROTOCOL_ERROR = "BRIDGE_PROTOCOL_ERROR"
    BRIDGE_TIMEOUT = "BRIDGE_TIMEOUT"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    EXPORT_ALREADY_RUNNING = "EXPORT_ALREADY_RUNNING"
    EXPORT_FAILED = "EXPORT_FAILED"
    EXPORT_JOB_NOT_FOUND = "EXPORT_JOB_NOT_FOUND"
    EXPORT_ARTIFACT_INVALID = "EXPORT_ARTIFACT_INVALID"
    SNAPSHOT_NOT_FOUND = "SNAPSHOT_NOT_FOUND"
    SNAPSHOT_MISMATCH = "SNAPSHOT_MISMATCH"
    TYPE_NOT_FOUND = "TYPE_NOT_FOUND"
    MEMBER_NOT_FOUND = "MEMBER_NOT_FOUND"
    AMBIGUOUS_MEMBER = "AMBIGUOUS_MEMBER"
    OBJECT_EXPIRED = "OBJECT_EXPIRED"
    PLAN_INVALID = "PLAN_INVALID"
    PLAN_NOT_VALIDATED = "PLAN_NOT_VALIDATED"
    PLAN_TARGET_MISMATCH = "PLAN_TARGET_MISMATCH"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_INVALID = "APPROVAL_INVALID"
    POLICY_DENIED = "POLICY_DENIED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ReframeworkMCPError(RuntimeError):
    """An error safe to return as structured MCP output."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = retryable

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code.value,
                "message": self.message,
                "details": self.details,
                "retryable": self.retryable,
            },
        }


def success(data: Any, **metadata: Any) -> dict[str, Any]:
    """Return the common success envelope used by every tool."""

    payload: dict[str, Any] = {"ok": True, "data": data}
    if metadata:
        payload["meta"] = metadata
    return payload
