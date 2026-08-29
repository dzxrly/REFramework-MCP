from __future__ import annotations

import json
from pathlib import Path

import pytest

from reframework_mcp.bridge import BridgeClient, InMemoryTransport
from reframework_mcp.config import Settings
from reframework_mcp.server import create_server
from reframework_mcp.services import ApplicationServices
from reframework_mcp.storage import Database

EXPECTED_TOOLS = {
    "runtime_status",
    "run_generate_sdk",
    "search_types",
    "describe_type",
    "search_members",
    "find_type_dependencies",
    "list_singletons",
    "inspect_object",
    "search_usage_examples",
    "find_access_paths",
    "validate_access_plan",
    "draft_lua_probe",
    "validate_lua_probe",
    "invoke_method",
    "set_field",
    "run_lua_probe",
    "install_hook",
    "remove_hook",
}

EXPECTED_RESOURCES = {
    "reframework://metadata/exports/{job_ref}",
    "reframework://metadata/snapshots/{snapshot_id}/manifest",
    "reframework://metadata/snapshots/{snapshot_id}/coverage",
    "reframework://explorations/{exploration_id}/graph",
    "reframework://access-plans/{plan_ref}",
    "reframework://access-plans/{plan_ref}/validation",
    "reframework://usage/{usage_ref}",
    "reframework://hooks/{hook_ref}/events",
    "reframework://probes/{probe_ref}/events",
}


def test_server_registers_frozen_v1_tool_set(tmp_path: Path) -> None:
    def handler(request: dict[str, object]) -> dict[str, object]:
        return {
            "protocol": "1.0",
            "request_id": request["request_id"],
            "runtime_epoch": "runtime:test",
            "ok": True,
            "data": {},
        }

    services = ApplicationServices(
        Settings(data_dir=tmp_path, database_path=tmp_path / "metadata.db"),
        BridgeClient(InMemoryTransport(handler)),
    )
    server = create_server(services)

    assert set(server._tool_manager._tools) == EXPECTED_TOOLS
    assert set(server._resource_manager._templates) == EXPECTED_RESOURCES


def test_mutation_tools_hide_context_and_accept_approval_required_output(
    tmp_path: Path,
) -> None:
    services = ApplicationServices(
        Settings(data_dir=tmp_path, database_path=tmp_path / "metadata.db"),
        BridgeClient(InMemoryTransport(lambda _request: {})),
    )
    tools = create_server(services)._tool_manager._tools

    for name in (
        "run_generate_sdk",
        "invoke_method",
        "set_field",
        "run_lua_probe",
        "install_hook",
    ):
        tool = tools[name]
        assert "ctx" not in tool.parameters.get("properties", {})
        assert "ApprovalRequiredData" in json.dumps(tool.output_schema)


@pytest.mark.asyncio
async def test_list_singletons_normalizes_optional_query_for_bridge(tmp_path: Path) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: dict[str, object]) -> dict[str, object]:
        requests.append(request)
        return {
            "protocol": "1.0",
            "request_id": request["request_id"],
            "runtime_epoch": "runtime:test",
            "ok": True,
            "data": {
                "runtime_epoch": "runtime:test",
                "items": [],
                "truncated": False,
            },
        }

    services = ApplicationServices(
        Settings(data_dir=tmp_path, database_path=tmp_path / "metadata.db"),
        BridgeClient(InMemoryTransport(handler)),
    )
    server = create_server(services)

    result = await server._tool_manager._tools["list_singletons"].fn()

    assert result["ok"] is True
    assert requests[-1]["payload"] == {
        "kinds": ["managed", "native"],
        "type_query": "",
        "limit": 500,
    }


@pytest.mark.asyncio
async def test_registered_static_exploration_tools_forward_only_public_arguments(
    tmp_path: Path,
    imported_database: tuple[Database, str],
) -> None:
    database, snapshot_id = imported_database

    def handler(request: dict[str, object]) -> dict[str, object]:
        return {
            "protocol": "1.0",
            "request_id": request["request_id"],
            "runtime_epoch": "runtime:test",
            "ok": True,
            "data": {},
        }

    services = ApplicationServices(
        Settings(data_dir=tmp_path, database_path=database.path),
        BridgeClient(InMemoryTransport(handler)),
    )
    server = create_server(services)
    tools = server._tool_manager._tools

    types = await tools["search_types"].fn(
        query="app.",
        snapshot_id=snapshot_id,
    )
    description = await tools["describe_type"].fn(
        full_name="app.UserSaveData",
        snapshot_id=snapshot_id,
    )
    members = await tools["search_members"].fn(
        query="addSaveItem",
        snapshot_id=snapshot_id,
        declaring_type="app.UserSaveData",
    )
    dependencies = await tools["find_type_dependencies"].fn(
        type_name="app.UserSaveData",
        snapshot_id=snapshot_id,
    )

    assert types["ok"] is True
    assert description["ok"] is True
    assert members["ok"] is True
    assert dependencies["ok"] is True
