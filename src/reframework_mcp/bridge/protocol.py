"""Versioned JSON envelope used over the local bridge transport."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

BRIDGE_PROTOCOL_VERSION = "1.0"
MAX_FRAME_BYTES = 16 * 1024 * 1024


class BridgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: str = BRIDGE_PROTOCOL_VERSION
    request_id: str
    runtime_epoch: str | None = None
    kind: Literal["command"] = "command"
    command: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class BridgeError(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False


class BridgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: str
    request_id: str
    runtime_epoch: str | None = None
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: BridgeError | None = None
