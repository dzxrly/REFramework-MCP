from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from reframework_mcp.graphs import RuntimeGraphStore
from reframework_mcp.metadata import MetadataRepository
from reframework_mcp.models import AccessOperation, AccessPlan
from reframework_mcp.planner import AccessPlanner
from reframework_mcp.search import MemberSearchService
from reframework_mcp.storage import Database
from reframework_mcp.usage import UsageIndexer


def _member_pk(metadata: MetadataRepository, signature_part: str) -> int:
    result = metadata.search_members(signature_part, limit=20)
    item = next(
        candidate for candidate in result["items"] if signature_part in candidate["canonical_signature"]
    )
    return int(item["member_pk"])


def test_search_members_fuses_usage_runtime_and_reachability(
    imported_database: tuple[Database, str],
    fixture_root: Path,
) -> None:
    database, _ = imported_database
    metadata = MetadataRepository(database)
    graph = RuntimeGraphStore(database)
    graph.record_hook_install(
        {
            "hook_ref": "hook:runtime:test:add",
            "runtime_epoch": "runtime:test",
            "state": "installed",
            "member_signature": (
                "app.UserSaveData::addSaveItem(System.Int32, System.Int32) -> System.Boolean"
            ),
            "argument_layout": [
                {"argument_index": 1, "role": "this", "type": "app.UserSaveData"},
                {
                    "argument_index": 2,
                    "role": "parameter",
                    "name": "itemId",
                    "type": "System.Int32",
                },
            ],
        },
        runtime_epoch="runtime:test",
    )
    graph.record_hook_payload(
        {
            "hook_ref": "hook:runtime:test:add",
            "runtime_epoch": "runtime:test",
            "member_signature": (
                "app.UserSaveData::addSaveItem(System.Int32, System.Int32) -> System.Boolean"
            ),
            "state": "installed",
            "events": [
                {
                    "timestamp": "2026-08-28T00:00:00Z",
                    "phase": "pre",
                    "arguments": [
                        {
                            "index": 0,
                            "type": "app.UserSaveData",
                            "value": {
                                "object_ref": "obj:user-save",
                                "type": "app.UserSaveData",
                            },
                        },
                        {
                            "index": 1,
                            "type": "System.Int32",
                            "value": {"summary": "value"},
                        },
                    ],
                }
            ],
        },
        runtime_epoch="runtime:test",
    )
    UsageIndexer(database).index_project(fixture_root / "mod_usage", game_id="mhwilds")
    planner = AccessPlanner(database, metadata, graph)
    service = MemberSearchService(database, metadata, planner, graph)

    result = service.search(
        "addSaveItem",
        parameter_type="System.Int32",
        game_id="mhwilds",
        runtime_epoch="runtime:test",
    )

    observed = next(item for item in result["items"] if item["runtime_observation"]["observed"])
    assert observed["verification_state"] == "observed"
    assert observed["usage_examples"]
    assert observed["access_paths"]
    assert "live_observation_score" in observed["ranking"]["signals"]


def test_planner_builds_multi_root_method_argument_dag(
    imported_database: tuple[Database, str],
) -> None:
    database, snapshot_id = imported_database
    metadata = MetadataRepository(database)
    graph = RuntimeGraphStore(database)
    graph.record_singletons(
        {
            "runtime_epoch": "runtime:test",
            "items": [
                {
                    "kind": "managed_singleton",
                    "type_name": "app.SaveDataManager",
                    "object_ref": "obj:save-manager",
                }
            ],
        }
    )
    graph.record_inspection(
        {
            "runtime_epoch": "runtime:test",
            "nodes": [
                {
                    "object_ref": "obj:item-id",
                    "type": "app.ItemId",
                    "kind": "managed",
                    "fields": [],
                }
            ],
            "edges": [],
        }
    )
    target = _member_pk(
        metadata,
        "addSaveItem(app.ItemId, System.Int32, System.Boolean)",
    )
    planner = AccessPlanner(database, metadata, graph)

    result = planner.find_paths(
        target_member_pk=target,
        snapshot_id=snapshot_id,
        runtime_epoch="runtime:test",
        current_runtime_required=True,
        max_paths=5,
    )

    plan = AccessPlan.model_validate(result["plans"][0]["plan"])
    roots = [node for node in plan.nodes if node.operation is AccessOperation.RESOLVE_ROOT]
    target_node = next(node for node in plan.nodes if node.node_id == "target")
    assert len(roots) >= 2
    singleton_root = next(node for node in roots if node.output_type == "app.SaveDataManager")
    assert singleton_root.root is not None
    assert singleton_root.root.kind.value == "managed_singleton"
    assert singleton_root.root.object_ref is None
    assert len(target_node.arguments) == 3
    assert all(
        plan.nodes.index(next(node for node in plan.nodes if node.node_id == argument))
        < plan.nodes.index(target_node)
        for argument in target_node.arguments
    )


def test_usage_indexer_persists_explicit_graph_edges(
    tmp_path: Path,
    fixture_root: Path,
) -> None:
    database = Database(tmp_path / "usage-graph.db")
    database.initialize()
    UsageIndexer(database).index_project(fixture_root / "mod_usage", game_id="mhwilds")

    with database.connect() as connection:
        kinds = {
            str(row["edge_kind"])
            for row in connection.execute("SELECT DISTINCT edge_kind FROM usage_edges").fetchall()
        }
    assert "CODE_ACQUIRES_ROOT" in kinds
    assert "CODE_CALLS_MEMBER" in kinds
    assert "CODE_HOOKS_MEMBER" in kinds


def test_expired_object_refs_are_pruned_from_runtime_graph(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "runtime-expiry.db")
    database.initialize()
    graph = RuntimeGraphStore(database)
    graph.record_singletons(
        {
            "runtime_epoch": "runtime:test",
            "items": [
                {
                    "kind": "managed_singleton",
                    "type_name": "app.SaveDataManager",
                    "object_ref": "obj:expired",
                    "lease_seconds": 1,
                }
            ],
        }
    )
    expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with database.connect() as connection:
        connection.execute(
            "UPDATE runtime_nodes SET expires_at=? WHERE node_ref='obj:expired'",
            (expired_at,),
        )
        connection.commit()

    roots = graph.candidate_roots(
        runtime_epoch="runtime:test",
        current_only=True,
    )

    assert all(item["object_ref"] != "obj:expired" for item in roots)
    with database.connect() as connection:
        assert (
            connection.execute("SELECT 1 FROM runtime_nodes WHERE node_ref='obj:expired'").fetchone() is None
        )
        assert (
            connection.execute(
                """
                SELECT 1 FROM runtime_edges
                WHERE source_ref='obj:expired' OR target_ref='obj:expired'
                """
            ).fetchone()
            is None
        )
