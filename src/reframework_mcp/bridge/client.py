"""High-level bridge client with protocol validation and connection state."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from reframework_mcp.bridge.protocol import (
    BRIDGE_PROTOCOL_VERSION,
    BridgeRequest,
    BridgeResponse,
)
from reframework_mcp.bridge.transport import BridgeTransport
from reframework_mcp.errors import ErrorCode, ReframeworkMCPError


class BridgeClient:
    def __init__(
        self,
        transport: BridgeTransport,
        *,
        request_timeout: float = 30.0,
    ) -> None:
        self.transport = transport
        self.request_timeout = request_timeout
        self._connected = False
        self.runtime_epoch: str | None = None
        self.last_error: str | None = None
        self.last_seen: str | None = None
        self.capabilities: dict[str, Any] = {}
        self.runtime_info: dict[str, Any] = {}

    @property
    def connected(self) -> bool:
        return self._connected

    async def probe(self) -> dict[str, Any]:
        try:
            data = await self.call("runtime_status", {})
        except ReframeworkMCPError as error:
            self._connected = False
            self.last_error = error.message
            return self.status()
        self.capabilities = dict(data.get("capabilities", {}))
        self.runtime_info = {
            key: value for key, value in data.items() if key not in {"capabilities", "runtime_epoch"}
        }
        return self.status()

    async def call(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = BridgeRequest(
            request_id=str(uuid.uuid4()),
            runtime_epoch=self.runtime_epoch,
            command=command,
            payload=payload,
        )
        try:
            raw = await self.transport.request(
                request.model_dump(mode="json"),
                self.request_timeout,
            )
            response = BridgeResponse.model_validate(raw)
        except ReframeworkMCPError:
            self._connected = False
            raise
        except Exception as error:
            self._connected = False
            self.last_error = str(error)
            raise ReframeworkMCPError(
                ErrorCode.BRIDGE_PROTOCOL_ERROR,
                "Invalid response from REFramework bridge",
                details={"error": str(error)},
                retryable=True,
            ) from error

        if response.protocol.split(".", 1)[0] != BRIDGE_PROTOCOL_VERSION.split(".", 1)[0]:
            self._connected = False
            raise ReframeworkMCPError(
                ErrorCode.BRIDGE_PROTOCOL_ERROR,
                f"Incompatible bridge protocol: {response.protocol}",
            )
        if response.request_id != request.request_id:
            self._connected = False
            raise ReframeworkMCPError(
                ErrorCode.BRIDGE_PROTOCOL_ERROR,
                "Bridge response request_id does not match",
            )
        self._connected = True
        self.runtime_epoch = response.runtime_epoch
        self.last_seen = datetime.now(UTC).isoformat()
        self.last_error = None
        if not response.ok:
            response_error = response.error
            code = ErrorCode.INTERNAL_ERROR
            if response_error is not None:
                try:
                    code = ErrorCode(response_error.code)
                except ValueError:
                    code = ErrorCode.INTERNAL_ERROR
            raise ReframeworkMCPError(
                code,
                response_error.message if response_error else "Bridge command failed",
                details=response_error.details if response_error else {},
                retryable=response_error.retryable if response_error else False,
            )
        return response.data

    def status(self) -> dict[str, Any]:
        known_runtime: dict[str, Any] = {}
        for key in (
            "bridge_version",
            "game_id",
            "reframework_version",
            "tdb",
            "object_registry_size",
            "active_hooks",
        ):
            if key in self.runtime_info:
                known_runtime[key] = self.runtime_info[key]
        game_id = known_runtime.get("game_id")
        if isinstance(game_id, str):
            known_runtime["game_id"] = game_id.lower()
        return {
            "connected": self._connected,
            "protocol": BRIDGE_PROTOCOL_VERSION,
            "runtime_epoch": self.runtime_epoch,
            "last_seen": self.last_seen,
            "last_error": self.last_error,
            **known_runtime,
            "runtime": self.runtime_info,
            "capabilities": self.capabilities,
        }
