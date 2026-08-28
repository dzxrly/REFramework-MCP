"""Minimal structured audit trail."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from reframework_mcp.storage import Database


class AuditLog:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        outcome: str,
        runtime_epoch: str | None = None,
        snapshot_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        serialized = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        request_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_events(
                    timestamp, tool_name, runtime_epoch, snapshot_id,
                    request_hash, outcome, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(UTC).isoformat(),
                    tool_name,
                    runtime_epoch,
                    snapshot_id,
                    request_hash,
                    outcome,
                    json.dumps(details or {}, ensure_ascii=False, default=str),
                ),
            )
            connection.commit()
