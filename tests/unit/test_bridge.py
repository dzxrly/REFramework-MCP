from __future__ import annotations

import pytest

from reframework_mcp.bridge import BridgeClient, InMemoryTransport
from reframework_mcp.errors import ErrorCode, ReframeworkMCPError


def _handler(request: dict[str, object]) -> dict[str, object]:
    return {
        "protocol": "1.0",
        "request_id": request["request_id"],
        "runtime_epoch": "runtime:test",
        "ok": True,
        "data": {
            "game_id": "MHWILDS",
            "tdb": {"fingerprint": "sha256:test"},
            "future_runtime_field": {"supported": True},
            "capabilities": {"run_generate_sdk": True},
        },
    }


@pytest.mark.asyncio
async def test_bridge_negotiates_epoch_and_capabilities() -> None:
    bridge = BridgeClient(InMemoryTransport(_handler))

    status = await bridge.probe()

    assert status["connected"] is True
    assert status["runtime_epoch"] == "runtime:test"
    assert status["game_id"] == "mhwilds"
    assert status["runtime"]["game_id"] == "MHWILDS"
    assert status["tdb"]["fingerprint"] == "sha256:test"
    assert status["runtime"]["future_runtime_field"]["supported"] is True
    assert status["capabilities"]["run_generate_sdk"] is True


@pytest.mark.asyncio
async def test_bridge_rejects_protocol_major_mismatch() -> None:
    def handler(request: dict[str, object]) -> dict[str, object]:
        response = _handler(request)
        response["protocol"] = "2.0"
        return response

    bridge = BridgeClient(InMemoryTransport(handler))

    with pytest.raises(ReframeworkMCPError) as raised:
        await bridge.call("runtime_status", {})
    assert raised.value.code is ErrorCode.BRIDGE_PROTOCOL_ERROR
