from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reframework_mcp.bridge import BridgeClient, InMemoryTransport
from reframework_mcp.config import Settings
from reframework_mcp.server import create_server
from reframework_mcp.services import ApplicationServices


def test_c2_resources_return_persisted_graph_and_evidence(
    tmp_path: Path,
    fixture_root: Path,
) -> None:
    def handler(request: dict[str, object]) -> dict[str, object]:
        return {
            "protocol": "1.0",
            "request_id": request["request_id"],
            "runtime_epoch": "runtime:test",
            "ok": True,
            "data": {},
        }

    bridge = BridgeClient(InMemoryTransport(handler))
    bridge.runtime_epoch = "runtime:test"
    services = ApplicationServices(
        Settings(data_dir=tmp_path, database_path=tmp_path / "metadata.db"),
        bridge,
    )
    imported = services.import_dump(
        fixture_root / "il2cpp_dump" / "minimal_chain.json",
    )
    snapshot_id = str(imported["data"]["snapshot_id"])
    services.index_usage_project(
        fixture_root / "mod_usage",
        game_id="mhwilds",
    )
    services.runtime_graph.record_singletons(
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

    now = datetime.now(UTC)
    with services.database.connect() as connection:
        usage_pk = int(
            connection.execute("SELECT usage_pk FROM usage_sites ORDER BY usage_pk LIMIT 1").fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO access_plans(
                plan_ref, snapshot_id, game_id, goal, plan_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                "plan:resource",
                snapshot_id,
                "mhwilds",
                "resource test",
                "{}",
                now.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO plan_validations(
                validation_ref, plan_ref, runtime_epoch, status,
                validation_json, created_at, expires_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "plan-validation:resource",
                "plan:resource",
                "runtime:test",
                "valid",
                json.dumps({"live_valid": True}),
                now.isoformat(),
                (now + timedelta(minutes=1)).isoformat(),
            ),
        )
        connection.commit()

    templates = create_server(services)._resource_manager._templates
    coverage = templates["reframework://metadata/snapshots/{snapshot_id}/coverage"].fn(snapshot_id)
    graph = templates["reframework://explorations/{exploration_id}/graph"].fn("current")
    usage = templates["reframework://usage/{usage_ref}"].fn(f"usage:{usage_pk}")
    validation = templates["reframework://access-plans/{plan_ref}/validation"].fn("plan:resource")

    assert coverage["snapshot_id"] == snapshot_id
    assert coverage["sections"]
    assert graph["runtime_epoch"] == "runtime:test"
    assert graph["nodes"][0]["node_ref"] == "obj:save-manager"
    assert usage["kind"] == "usage_site"
    assert usage["usage"]["usage_pk"] == usage_pk
    assert validation["validation_ref"] == "plan-validation:resource"
    assert validation["live_valid"] is True
