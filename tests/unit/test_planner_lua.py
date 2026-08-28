from __future__ import annotations

from pathlib import Path

import pytest

from reframework_mcp.lua import LuaDraftService, LuaValidationService
from reframework_mcp.metadata import MetadataRepository
from reframework_mcp.models import (
    AccessNode,
    AccessOperation,
    AccessPlan,
    RootKind,
    RootSpec,
)
from reframework_mcp.planner import AccessPlanner, AccessPlanValidator
from reframework_mcp.storage import Database
from reframework_mcp.usage import UsageIndexer


class OfflineBridge:
    connected = False
    runtime_epoch = None

    async def call(self, command: str, payload: dict[str, object]) -> dict[str, object]:
        raise AssertionError(f"offline bridge was called: {command} {payload}")


def test_planner_builds_chain_from_indexed_singleton(
    imported_database: tuple[Database, str],
    fixture_root: Path,
) -> None:
    database, _ = imported_database
    metadata = MetadataRepository(database)
    UsageIndexer(database).index_project(fixture_root / "mod_usage", game_id="mhwilds")
    planner = AccessPlanner(database, metadata)

    result = planner.find_paths(target_type="app.ItemSlot", max_depth=6)

    assert result["plans"]
    plan = AccessPlan.model_validate(result["plans"][0]["plan"])
    assert plan.nodes[0].root is not None
    assert plan.nodes[0].root.kind is RootKind.MANAGED_SINGLETON
    assert [node.operation for node in plan.nodes] == [
        AccessOperation.RESOLVE_ROOT,
        AccessOperation.READ_PROPERTY,
        AccessOperation.READ_FIELD,
        AccessOperation.READ_FIELD,
        AccessOperation.ITERATE,
    ]


def test_planner_keeps_the_matching_empty_path_root(
    imported_database: tuple[Database, str],
) -> None:
    database, _ = imported_database
    planner = AccessPlanner(database, MetadataRepository(database))

    result = planner.find_paths(
        target_type="app.UserSaveData",
        root_types=["app.SaveDataManager", "app.UserSaveData"],
    )
    direct = next(
        AccessPlan.model_validate(candidate["plan"])
        for candidate in result["plans"]
        if len(candidate["plan"]["nodes"]) == 1
    )

    assert direct.nodes[0].root is not None
    assert direct.nodes[0].root.type_name == "app.UserSaveData"


def test_planner_bounds_unreachable_search_from_high_fanout_root(
    imported_database: tuple[Database, str],
) -> None:
    database, snapshot_id = imported_database
    with database.connect() as connection:
        connection.executemany(
            """
            INSERT INTO type_edges(
                snapshot_id, source_type, target_type, edge_kind, metadata_json
            ) VALUES(?, 'app.SaveDataManager', ?, 'FIELD_TYPE', '{}')
            """,
            [(snapshot_id, f"app.Unrelated{index}") for index in range(500)],
        )
        connection.commit()
    planner = AccessPlanner(database, MetadataRepository(database))

    result = planner.find_paths(
        target_type="app.UnreachableTarget",
        snapshot_id=snapshot_id,
        root_types=["app.SaveDataManager"],
        max_depth=6,
    )
    cached = planner.find_paths(
        target_type="app.UnreachableTarget",
        snapshot_id=snapshot_id,
        root_types=["app.SaveDataManager"],
        max_depth=6,
    )

    assert result["plans"] == []
    assert result["traversal"]["direction"] == "reverse_multi_root"
    assert result["traversal"]["expanded_types"] == 1
    assert result["traversal"]["truncated"] is False
    assert cached["traversal"]["cached"] is True


@pytest.mark.asyncio
async def test_access_plan_and_lua_validation_support_multi_root_dag(
    imported_database: tuple[Database, str],
) -> None:
    database, snapshot_id = imported_database
    metadata = MetadataRepository(database)
    plan = AccessPlan(
        snapshot_id=snapshot_id,
        goal="combine roots and constants",
        nodes=[
            AccessNode(
                node_id="save",
                operation=AccessOperation.RESOLVE_ROOT,
                root=RootSpec(
                    kind=RootKind.MANAGED_SINGLETON,
                    type_name="app.SaveDataManager",
                ),
                output_type="app.SaveDataManager",
            ),
            AccessNode(
                node_id="count",
                operation=AccessOperation.BIND_CONSTANT,
                value=10,
                output_type="System.Int32",
                nullable=False,
                side_effect="none",
            ),
            AccessNode(
                node_id="ids",
                operation=AccessOperation.CONSTRUCT_LIST,
                arguments=["count"],
                output_type="System.Collections.Generic.List<System.Int32>",
                options={"element_type": "System.Int32"},
            ),
            AccessNode(
                node_id="emit",
                operation=AccessOperation.EMIT,
                inputs=["save"],
                arguments=["ids"],
                output_type="app.SaveDataManager",
            ),
        ],
        targets=["emit", "ids"],
    )
    validation = await AccessPlanValidator(
        database,
        metadata,
        OfflineBridge(),
    ).validate(plan, live=False)
    draft = LuaDraftService(metadata).draft(plan)
    lua_validation = await LuaValidationService(
        database,
        metadata,
        OfflineBridge(),
    ).validate(
        draft["code"],
        mode="oneshot",
        snapshot_id=snapshot_id,
        plan_ref=plan.plan_ref,
        runtime_epoch=None,
        live_compile=False,
    )

    assert validation.symbolic_valid is True
    assert "probe.construct_list" in draft["code"]
    assert lua_validation["valid"] is True
