from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from reframework_mcp.jobs import ExportJobCoordinator
from reframework_mcp.metadata import Il2CppDumpImporter, MetadataRepository
from reframework_mcp.storage import Database


class CompletedExportBridge:
    connected = True

    def __init__(self, dump_path: Path, manifest_path: Path) -> None:
        self.dump_path = dump_path
        self.manifest_path = manifest_path

    async def call(
        self,
        command: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        assert command == "get_export_status"
        return {
            "job_ref": payload["job_ref"],
            "state": "completed",
            "runtime_epoch": "runtime:test",
            "provider_version": "1.0",
            "artifacts": {
                "il2cpp_dump": str(self.dump_path),
                "manifest": str(self.manifest_path),
            },
        }


async def _wait_for_terminal(
    coordinator: ExportJobCoordinator,
    job_ref: str,
) -> dict[str, object]:
    for _ in range(500):
        persisted = coordinator.status(job_ref)
        if persisted["state"] in {
            "completed",
            "failed",
            "cancelled",
            "reused",
            "host_failed",
        }:
            assert persisted.get("indexed") == persisted["status"].get("indexed")
            return dict(persisted["status"])
        await asyncio.sleep(0.01)
    raise AssertionError(f"Export job did not reach a terminal state: {job_ref}")


def test_completed_export_is_indexed_and_activated(
    tmp_path: Path,
    fixture_root: Path,
) -> None:
    snapshots = tmp_path / "snapshots"
    artifact_dir = snapshots / "mhwilds" / "sha256-test" / "job-test"
    artifact_dir.mkdir(parents=True)
    dump_path = artifact_dir / "il2cpp_dump.json"
    shutil.copyfile(
        fixture_root / "il2cpp_dump" / "minimal_chain.json",
        dump_path,
    )
    manifest_path = artifact_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "snapshot_id": "snapshot:generated",
                "snapshot_schema": "1.0",
                "game_id": "mhwilds",
                "tdb_version": 81,
                "tdb_fingerprint": "sha256:test",
                "runtime_epoch": "runtime:test",
            }
        ),
        encoding="utf-8",
    )
    database = Database(tmp_path / "metadata.db")
    database.initialize()
    coordinator = ExportJobCoordinator(
        database,
        Il2CppDumpImporter(database),
        CompletedExportBridge(dump_path, manifest_path),
        snapshots,
    )
    coordinator.register(
        {
            "job_ref": "export:runtime:test:1",
            "state": "queued",
            "runtime_epoch": "runtime:test",
        },
        mode="json_only",
        policy="reuse_if_fresh",
        activate_snapshot=True,
        index_after_export=True,
    )

    async def exercise() -> dict[str, object]:
        accepted = await coordinator.refresh("export:runtime:test:1")
        assert accepted["state"] == "indexing"
        return await _wait_for_terminal(coordinator, "export:runtime:test:1")

    result = asyncio.run(exercise())

    assert result["state"] == "completed"
    assert result["snapshot_id"] == "snapshot:generated"
    assert result["indexed"] is True
    assert result["host_state"] == "completed"
    assert result["terminal"] is True
    assert result["host_progress"]["processed_entities"] > 0
    assert MetadataRepository(database).active_snapshot_id() == "snapshot:generated"


def test_completed_export_rejects_artifact_outside_managed_root(
    tmp_path: Path,
    fixture_root: Path,
) -> None:
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    dump_path = tmp_path / "outside.json"
    shutil.copyfile(
        fixture_root / "il2cpp_dump" / "minimal_chain.json",
        dump_path,
    )
    manifest_path = tmp_path / "outside-manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    database = Database(tmp_path / "metadata.db")
    database.initialize()
    coordinator = ExportJobCoordinator(
        database,
        Il2CppDumpImporter(database),
        CompletedExportBridge(dump_path, manifest_path),
        snapshots,
    )
    coordinator.register(
        {"job_ref": "export:runtime:test:unsafe", "state": "queued"},
        mode="json_only",
        policy="reuse_if_fresh",
        activate_snapshot=True,
        index_after_export=True,
    )

    result = asyncio.run(coordinator.refresh("export:runtime:test:unsafe"))

    assert result["state"] == "host_failed"
    assert "outside the managed snapshot directory" in result["host_error"]


def test_reused_export_is_imported_when_host_database_is_empty(
    tmp_path: Path,
    fixture_root: Path,
) -> None:
    snapshots = tmp_path / "snapshots"
    artifact_dir = snapshots / "mhwilds" / "sha256-test" / "reused"
    artifact_dir.mkdir(parents=True)
    dump_path = artifact_dir / "il2cpp_dump.json"
    shutil.copyfile(
        fixture_root / "il2cpp_dump" / "minimal_chain.json",
        dump_path,
    )
    manifest_path = artifact_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "snapshot_id": "snapshot:reused",
                "snapshot_schema": "1.0",
                "game_id": "mhwilds",
                "tdb_version": 81,
                "tdb_fingerprint": "sha256:test",
            }
        ),
        encoding="utf-8",
    )
    database = Database(tmp_path / "metadata.db")
    database.initialize()
    coordinator = ExportJobCoordinator(
        database,
        Il2CppDumpImporter(database),
        CompletedExportBridge(dump_path, manifest_path),
        snapshots,
    )

    async def exercise() -> tuple[dict[str, object], dict[str, object]]:
        accepted = coordinator.register(
            {
                "state": "reused",
                "reused_snapshot_id": "snapshot:reused",
                "provider_version": "1.0",
                "artifacts": {
                    "il2cpp_dump": str(dump_path),
                    "manifest": str(manifest_path),
                },
            },
            mode="json_only",
            policy="reuse_if_fresh",
            activate_snapshot=True,
            index_after_export=True,
        )
        assert accepted["state"] == "indexing"
        assert accepted["indexed"] is False
        completed = await _wait_for_terminal(coordinator, str(accepted["job_ref"]))
        return accepted, completed

    accepted, result = asyncio.run(exercise())

    assert accepted["job_ref"]
    assert result["state"] == "reused"
    assert result["indexed"] is True
    assert result["activated"] is True
    assert MetadataRepository(database).active_snapshot_id() == "snapshot:reused"


def test_reconcile_pending_reads_host_state_without_row_error(
    tmp_path: Path,
    fixture_root: Path,
    monkeypatch,
) -> None:
    snapshots = tmp_path / "snapshots"
    artifact_dir = snapshots / "mhwilds" / "sha256-test" / "pending"
    artifact_dir.mkdir(parents=True)
    dump_path = artifact_dir / "il2cpp_dump.json"
    shutil.copyfile(
        fixture_root / "il2cpp_dump" / "minimal_chain.json",
        dump_path,
    )
    manifest_path = artifact_dir / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    database = Database(tmp_path / "metadata.db")
    database.initialize()
    coordinator = ExportJobCoordinator(
        database,
        Il2CppDumpImporter(database),
        CompletedExportBridge(dump_path, manifest_path),
        snapshots,
    )
    coordinator.register(
        {
            "job_ref": "export:runtime:test:pending",
            "state": "indexing",
            "runtime_epoch": "runtime:test",
        },
        mode="json_only",
        policy="reuse_if_fresh",
        activate_snapshot=True,
        index_after_export=True,
    )
    scheduled: list[str] = []
    monkeypatch.setattr(
        coordinator,
        "_ensure_finalizer",
        scheduled.append,
    )

    asyncio.run(coordinator.reconcile_pending_once())

    assert scheduled == ["export:runtime:test:pending"]
