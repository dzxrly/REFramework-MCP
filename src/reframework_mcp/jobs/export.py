"""Persist, monitor and finalize Generate SDK jobs."""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from reframework_mcp.errors import ErrorCode, ReframeworkMCPError
from reframework_mcp.metadata import Il2CppDumpImporter
from reframework_mcp.storage import Database


class ExportBridge(Protocol):
    @property
    def connected(self) -> bool: ...

    async def call(self, command: str, payload: dict[str, Any]) -> dict[str, Any]: ...


_TERMINAL_STATES = {"completed", "failed", "cancelled", "reused", "host_failed"}
_HOST_STATES = {"indexing", "activating"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ExportJobCoordinator:
    def __init__(
        self,
        database: Database,
        importer: Il2CppDumpImporter,
        bridge: ExportBridge,
        snapshots_dir: Path,
    ) -> None:
        self.database = database
        self.importer = importer
        self.bridge = bridge
        self.snapshots_dir = snapshots_dir.resolve()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._finalizers: dict[str, asyncio.Task[None]] = {}

    def register(
        self,
        response: dict[str, Any],
        *,
        mode: str,
        policy: str,
        activate_snapshot: bool,
        index_after_export: bool,
    ) -> dict[str, Any]:
        state = str(response.get("state", "queued"))
        job_ref = response.get("job_ref")
        if not job_ref:
            if state == "reused":
                response = self._register_reused(
                    response,
                    activate_snapshot=activate_snapshot,
                    index_after_export=index_after_export,
                    mode=mode,
                    policy=policy,
                )
            return response

        created = _now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO export_jobs(
                    job_ref, runtime_epoch, mode, request_policy, state,
                    activate_snapshot, index_after_export, status_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_ref) DO UPDATE SET
                    state=excluded.state,
                    status_json=excluded.status_json,
                    updated_at=excluded.updated_at
                """,
                (
                    str(job_ref),
                    response.get("runtime_epoch"),
                    mode,
                    policy,
                    state,
                    int(activate_snapshot),
                    int(index_after_export),
                    json.dumps(response, ensure_ascii=False),
                    created,
                    created,
                ),
            )
            connection.commit()
        self._ensure_watcher(str(job_ref))
        return response

    async def refresh(self, job_ref: str) -> dict[str, Any]:
        persisted = self.status(job_ref)
        persisted_state = str(persisted["state"])
        if persisted_state in _TERMINAL_STATES:
            return dict(persisted["status"])
        if persisted_state in _HOST_STATES:
            self._ensure_finalizer(job_ref)
            return dict(persisted["status"])
        status = await self.bridge.call("get_export_status", {"job_ref": job_ref})
        return self._persist_and_finalize(job_ref, status)

    async def reconcile_pending_once(self) -> None:
        if not self.bridge.connected:
            return
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT job_ref FROM export_jobs
                WHERE state NOT IN ('completed', 'failed', 'cancelled', 'reused', 'host_failed')
                ORDER BY created_at
                """
            ).fetchall()
        for row in rows:
            job_ref = str(row["job_ref"])
            if str(row["state"]) in _HOST_STATES:
                self._ensure_finalizer(job_ref)
                continue
            try:
                await self.refresh(job_ref)
            except ReframeworkMCPError:
                break
            self._ensure_watcher(job_ref)

    def status(self, job_ref: str | None = None) -> dict[str, Any]:
        with self.database.connect() as connection:
            if job_ref:
                row = connection.execute(
                    "SELECT * FROM export_jobs WHERE job_ref = ?",
                    (job_ref,),
                ).fetchone()
                if row is None:
                    raise ReframeworkMCPError(
                        ErrorCode.EXPORT_JOB_NOT_FOUND,
                        f"Export job not found: {job_ref}",
                    )
                return self._row(row)
            rows = connection.execute(
                """
                SELECT * FROM export_jobs
                ORDER BY
                    CASE WHEN state IN ('completed', 'failed', 'cancelled', 'reused', 'host_failed')
                         THEN 1 ELSE 0 END,
                    updated_at DESC
                LIMIT 20
                """
            ).fetchall()
        return {"jobs": [self._row(row) for row in rows]}

    def _ensure_watcher(self, job_ref: str) -> None:
        current = self._tasks.get(job_ref)
        if current is not None and not current.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._watch(job_ref), name=f"export-watch:{job_ref}")
        self._tasks[job_ref] = task
        task.add_done_callback(lambda _: self._tasks.pop(job_ref, None))

    def _ensure_finalizer(self, job_ref: str) -> None:
        current = self._finalizers.get(job_ref)
        if current is not None and not current.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self._finalize_host_job(job_ref),
            name=f"export-finalize:{job_ref}",
        )
        self._finalizers[job_ref] = task
        task.add_done_callback(lambda _: self._finalizers.pop(job_ref, None))

    async def _finalize_host_job(self, job_ref: str) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM export_jobs WHERE job_ref = ?",
                (job_ref,),
            ).fetchone()
        if row is None or str(row["state"]) not in _HOST_STATES:
            return

        status = json.loads(row["status_json"])
        bridge_state = str(status.get("bridge_state") or "completed")
        dump_path: Path | None = None
        snapshot_id: str | None = row["snapshot_id"]
        try:
            dump_path, manifest_path = self._artifact_paths(status)
            manifest = self._read_manifest(manifest_path)
            result = await asyncio.to_thread(
                self.importer.import_file,
                dump_path,
                provider="generate_sdk",
                provider_version=str(status.get("provider_version", "1.0")),
                mode=str(row["mode"]),
                manifest=manifest,
                activate=False,
            )
            snapshot_id = result.snapshot_id
            activated = False
            if bool(row["activate_snapshot"]):
                self._set_intermediate(
                    job_ref,
                    "activating",
                    {
                        **status,
                        "snapshot_id": snapshot_id,
                        "indexed": True,
                    },
                )
                await asyncio.to_thread(self._activate_existing, snapshot_id)
                activated = True
            final_state = bridge_state if bridge_state in {"completed", "reused"} else "completed"
            status = {
                **status,
                "state": final_state,
                "bridge_state": bridge_state,
                "snapshot_id": snapshot_id,
                "reused_snapshot_id": (
                    snapshot_id if bridge_state == "reused" else status.get("reused_snapshot_id")
                ),
                "indexed": True,
                "activated": activated,
            }
            error: str | None = None
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            error = str(exception)
            status = {
                **status,
                "state": "host_failed",
                "bridge_state": bridge_state,
                "host_error": error,
            }

        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE export_jobs
                SET state=?, status_json=?, artifact_path=?, snapshot_id=?,
                    updated_at=?, error=?
                WHERE job_ref=?
                """,
                (
                    status["state"],
                    json.dumps(status, ensure_ascii=False),
                    str(dump_path) if dump_path is not None else row["artifact_path"],
                    snapshot_id,
                    _now(),
                    error,
                    job_ref,
                ),
            )
            connection.commit()

    async def _watch(self, job_ref: str) -> None:
        delay = 0.25
        while True:
            try:
                status = await self.refresh(job_ref)
            except ReframeworkMCPError:
                return
            if str(status.get("state")) in _TERMINAL_STATES | _HOST_STATES:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 2.0)

    def _persist_and_finalize(
        self,
        job_ref: str,
        status: dict[str, Any],
    ) -> dict[str, Any]:
        state = str(status.get("state", "failed"))
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM export_jobs WHERE job_ref = ?",
                (job_ref,),
            ).fetchone()
        if row is None:
            raise ReframeworkMCPError(
                ErrorCode.EXPORT_JOB_NOT_FOUND,
                f"Export job not found: {job_ref}",
            )

        snapshot_id: str | None = row["snapshot_id"]
        artifact_path: str | None = row["artifact_path"]
        host_error: str | None = None
        if state == "completed" and bool(row["index_after_export"]) and not snapshot_id:
            try:
                dump_path, manifest_path = self._artifact_paths(status)
                artifact_path = str(dump_path)
                self._read_manifest(manifest_path)
                status = {
                    **status,
                    "state": "indexing",
                    "bridge_state": "completed",
                    "indexed": False,
                    "activated": False,
                }
                state = "indexing"
            except Exception as error:
                host_error = str(error)
                state = "host_failed"
                status = {
                    **status,
                    "state": state,
                    "bridge_state": "completed",
                    "host_error": host_error,
                }

        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE export_jobs
                SET state=?, status_json=?, artifact_path=?, snapshot_id=?,
                    updated_at=?, error=?
                WHERE job_ref=?
                """,
                (
                    state,
                    json.dumps(status, ensure_ascii=False),
                    artifact_path,
                    snapshot_id,
                    _now(),
                    host_error or status.get("error"),
                    job_ref,
                ),
            )
            connection.commit()
        if state == "indexing":
            self._ensure_finalizer(job_ref)
        return status

    def _set_intermediate(
        self,
        job_ref: str,
        state: str,
        status: dict[str, Any],
    ) -> None:
        value = {
            **status,
            "state": state,
            "bridge_state": status.get("bridge_state", status.get("state")),
        }
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE export_jobs
                SET state=?, status_json=?, updated_at=?
                WHERE job_ref=?
                """,
                (state, json.dumps(value, ensure_ascii=False), _now(), job_ref),
            )
            connection.commit()

    def _artifact_paths(self, status: dict[str, Any]) -> tuple[Path, Path | None]:
        artifacts = status.get("artifacts")
        values = artifacts if isinstance(artifacts, dict) else {}
        raw_dump = values.get("il2cpp_dump") or status.get("il2cpp_dump_path")
        if not raw_dump:
            raise ReframeworkMCPError(
                ErrorCode.EXPORT_ARTIFACT_INVALID,
                "Completed export did not provide il2cpp_dump path",
            )
        dump = self._contained_path(Path(str(raw_dump)))
        manifest_value = values.get("manifest") or status.get("manifest_path")
        manifest = self._contained_path(Path(str(manifest_value))) if manifest_value else None
        if not dump.is_file():
            raise ReframeworkMCPError(
                ErrorCode.EXPORT_ARTIFACT_INVALID,
                f"Export artifact does not exist: {dump}",
            )
        return dump, manifest

    def _contained_path(self, path: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.snapshots_dir)
        except ValueError as error:
            raise ReframeworkMCPError(
                ErrorCode.EXPORT_ARTIFACT_INVALID,
                "Bridge returned an artifact outside the managed snapshot directory",
                details={"path": str(resolved), "root": str(self.snapshots_dir)},
            ) from error
        return resolved

    @staticmethod
    def _read_manifest(path: Path | None) -> dict[str, Any] | None:
        if path is None:
            return None
        if not path.is_file():
            raise ReframeworkMCPError(
                ErrorCode.EXPORT_ARTIFACT_INVALID,
                f"Export manifest does not exist: {path}",
            )
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ReframeworkMCPError(
                ErrorCode.EXPORT_ARTIFACT_INVALID,
                "Export manifest must be a JSON object",
            )
        return value

    def _activate_existing(self, snapshot_id: str) -> None:
        with self.database.connect() as connection:
            exists = connection.execute(
                """
                SELECT 1 FROM snapshots
                WHERE snapshot_id=? AND import_state='complete'
                """,
                (snapshot_id,),
            ).fetchone()
            if exists is None:
                raise ReframeworkMCPError(
                    ErrorCode.SNAPSHOT_NOT_FOUND,
                    f"Reusable snapshot is not available locally: {snapshot_id}",
                )
            connection.execute("UPDATE snapshots SET active=0 WHERE active=1")
            connection.execute(
                "UPDATE snapshots SET active=1 WHERE snapshot_id=?",
                (snapshot_id,),
            )
            connection.commit()

    def _register_reused(
        self,
        response: dict[str, Any],
        *,
        activate_snapshot: bool,
        index_after_export: bool,
        mode: str,
        policy: str,
    ) -> dict[str, Any]:
        snapshot_id = str(response.get("reused_snapshot_id") or "")
        with self.database.connect() as connection:
            exists = (
                connection.execute(
                    """
                    SELECT 1 FROM snapshots
                    WHERE snapshot_id=? AND import_state='complete'
                    """,
                    (snapshot_id,),
                ).fetchone()
                if snapshot_id
                else None
            )
        indexed = exists is not None
        if indexed:
            activated = False
            if activate_snapshot:
                self._activate_existing(snapshot_id)
                activated = True
            return {
                **response,
                "reused_snapshot_id": snapshot_id,
                "indexed": True,
                "activated": activated,
            }
        if not index_after_export:
            if activate_snapshot:
                raise ReframeworkMCPError(
                    ErrorCode.SNAPSHOT_NOT_FOUND,
                    "Reused export is not indexed locally; enable index_after_export",
                )
            return {
                **response,
                "reused_snapshot_id": snapshot_id or None,
                "indexed": False,
                "activated": False,
            }

        dump_path, manifest_path = self._artifact_paths(response)
        self._read_manifest(manifest_path)
        job_ref = f"export:host:{secrets.token_hex(12)}"
        status = {
            **response,
            "job_ref": job_ref,
            "state": "indexing",
            "bridge_state": "reused",
            "indexed": False,
            "activated": False,
        }
        created = _now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO export_jobs(
                    job_ref, runtime_epoch, mode, request_policy, state,
                    activate_snapshot, index_after_export, status_json,
                    artifact_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'indexing', ?, 1, ?, ?, ?, ?)
                """,
                (
                    job_ref,
                    response.get("runtime_epoch"),
                    mode,
                    policy,
                    int(activate_snapshot),
                    json.dumps(status, ensure_ascii=False),
                    str(dump_path),
                    created,
                    created,
                ),
            )
            connection.commit()
        self._ensure_finalizer(job_ref)
        return status

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        status = json.loads(row["status_json"])
        return {
            **status,
            "job_ref": row["job_ref"],
            "runtime_epoch": row["runtime_epoch"],
            "mode": row["mode"],
            "policy": row["request_policy"],
            "state": row["state"],
            "activate_snapshot": bool(row["activate_snapshot"]),
            "index_after_export": bool(row["index_after_export"]),
            "status": status,
            "artifact_path": row["artifact_path"],
            "snapshot_id": row["snapshot_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "error": row["error"],
        }
