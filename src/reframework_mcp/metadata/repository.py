"""Queries over imported metadata snapshots."""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from typing import Any

from reframework_mcp.errors import ErrorCode, ReframeworkMCPError
from reframework_mcp.storage import Database


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {str(key): row[index] for index, key in enumerate(row.keys())}


class MetadataRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def active_snapshot_id(self) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT snapshot_id FROM snapshots WHERE active = 1 AND import_state = 'complete'"
            ).fetchone()
        return str(row["snapshot_id"]) if row else None

    def require_snapshot(self, snapshot_id: str | None = None) -> str:
        selected = snapshot_id or self.active_snapshot_id()
        if selected is None:
            raise ReframeworkMCPError(
                ErrorCode.SNAPSHOT_NOT_FOUND,
                "No active metadata snapshot. Run run_generate_sdk or import an existing dump.",
                details={
                    "recommended_action": {
                        "tool": "run_generate_sdk",
                        "arguments": {
                            "mode": "json_only",
                            "policy": "reuse_if_fresh",
                            "activate_snapshot": True,
                            "index_after_export": True,
                        },
                    }
                },
            )
        return selected

    def snapshot_status(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            active = connection.execute("SELECT * FROM snapshots WHERE active = 1").fetchone()
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE import_state = 'complete'"
                ).fetchone()[0]
            )
            coverage: dict[str, str] = {}
            if active:
                rows = connection.execute(
                    "SELECT section, status FROM snapshot_coverage WHERE snapshot_id = ?",
                    (active["snapshot_id"],),
                ).fetchall()
                coverage = {str(row["section"]): str(row["status"]) for row in rows}
        return {
            "active": _row_dict(active) if active else None,
            "complete_snapshot_count": total,
            "coverage": coverage,
        }

    def search_types(
        self,
        query: str,
        *,
        snapshot_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        selected = self.require_snapshot(snapshot_id)
        bounded_limit = max(1, min(limit, 200))
        bounded_offset = max(0, offset)
        like = f"%{query}%"
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT type_pk, snapshot_id, source_type_id, full_name, namespace,
                       name, parent_name, native_typename, size_text, flags,
                       is_generic_type, is_generic_definition, source
                FROM types
                WHERE snapshot_id = ?
                  AND (? = '' OR full_name LIKE ? COLLATE NOCASE OR name LIKE ? COLLATE NOCASE)
                ORDER BY
                    CASE WHEN full_name = ? COLLATE NOCASE THEN 0
                         WHEN full_name LIKE ? COLLATE NOCASE THEN 1
                         ELSE 2 END,
                    length(full_name),
                    full_name
                LIMIT ? OFFSET ?
                """,
                (
                    selected,
                    query,
                    like,
                    like,
                    query,
                    f"{query}%",
                    bounded_limit + 1,
                    bounded_offset,
                ),
            ).fetchall()
        has_more = len(rows) > bounded_limit
        return {
            "snapshot_id": selected,
            "items": [_row_dict(row) for row in rows[:bounded_limit]],
            "next_offset": bounded_offset + bounded_limit if has_more else None,
        }

    def describe_type(
        self,
        full_name: str,
        *,
        snapshot_id: str | None = None,
        member_limit: int = 500,
    ) -> dict[str, Any]:
        selected = self.require_snapshot(snapshot_id)
        with self.database.connect() as connection:
            type_row = connection.execute(
                "SELECT * FROM types WHERE snapshot_id = ? AND full_name = ?",
                (selected, full_name),
            ).fetchone()
            if type_row is None:
                raise ReframeworkMCPError(
                    ErrorCode.TYPE_NOT_FOUND,
                    f"Type not found: {full_name}",
                    details={"snapshot_id": selected},
                )
            members = connection.execute(
                """
                SELECT member_pk, source_member_id, kind, name, canonical_signature,
                       value_type, return_type, flags, address, offset_from_base,
                       getter_name, setter_name, source
                FROM members
                WHERE snapshot_id = ? AND declaring_type = ?
                ORDER BY kind, name, canonical_signature
                LIMIT ?
                """,
                (selected, full_name, max(1, min(member_limit, 2000))),
            ).fetchall()
            edges = connection.execute(
                """
                SELECT edge_kind, source_type, target_type, member_pk, metadata_json
                FROM type_edges
                WHERE snapshot_id = ? AND (source_type = ? OR target_type = ?)
                ORDER BY edge_kind, source_type, target_type
                LIMIT 1000
                """,
                (selected, full_name, full_name),
            ).fetchall()
            coverage_rows = connection.execute(
                "SELECT section, status FROM snapshot_coverage WHERE snapshot_id = ?",
                (selected,),
            ).fetchall()
        return {
            "snapshot_id": selected,
            "type": _row_dict(type_row),
            "members": [_row_dict(row) for row in members],
            "dependencies": [
                {
                    **_row_dict(row),
                    "metadata": json.loads(row["metadata_json"]),
                }
                for row in edges
            ],
            "coverage": {str(row["section"]): str(row["status"]) for row in coverage_rows},
        }

    def search_members(
        self,
        query: str = "",
        *,
        snapshot_id: str | None = None,
        canonical_signature: str | None = None,
        declaring_type: str | None = None,
        member_kinds: list[str] | None = None,
        value_type: str | None = None,
        return_type: str | None = None,
        parameter_type: str | None = None,
        is_static: bool | None = None,
        is_instance: bool | None = None,
        hookable: bool | None = None,
        metadata_sources: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        selected = self.require_snapshot(snapshot_id)
        bounded_limit = max(1, min(limit, 1000))
        bounded_offset = max(0, offset)
        clauses = ["m.snapshot_id = ?"]
        params: list[Any] = [selected]
        if query:
            clauses.append("(m.name LIKE ? COLLATE NOCASE OR m.canonical_signature LIKE ? COLLATE NOCASE)")
            like = f"%{query}%"
            params.extend((like, like))
        if canonical_signature:
            clauses.append("m.canonical_signature = ?")
            params.append(canonical_signature)
        if declaring_type:
            clauses.append("m.declaring_type = ?")
            params.append(declaring_type)
        if member_kinds:
            placeholders = ",".join("?" for _ in member_kinds)
            clauses.append(f"m.kind IN ({placeholders})")
            params.extend(member_kinds)
        if value_type:
            clauses.append("m.value_type = ?")
            params.append(value_type)
        if return_type:
            clauses.append("m.return_type = ?")
            params.append(return_type)
        if parameter_type:
            clauses.append(
                "EXISTS (SELECT 1 FROM method_params p WHERE p.member_pk = m.member_pk AND p.type_name = ?)"
            )
            params.append(parameter_type)
        static_expression = """
            (
                LOWER(COALESCE(m.flags, '')) LIKE '%static%'
                OR COALESCE(json_extract(m.raw_json, '$.is_static'), 0)
                   IN (1, 'true')
            )
        """
        if is_static is not None:
            clauses.append(static_expression if is_static else f"NOT {static_expression}")
        if is_instance is not None:
            clauses.append(f"NOT {static_expression}" if is_instance else static_expression)
        if hookable is not None:
            hookable_expression = """
                (
                    m.kind IN ('tdb_method', 'reflection_method')
                    AND NULLIF(m.address, '') IS NOT NULL
                )
            """
            clauses.append(hookable_expression if hookable else f"NOT {hookable_expression}")
        if metadata_sources:
            placeholders = ",".join("?" for _ in metadata_sources)
            clauses.append(f"m.source IN ({placeholders})")
            params.extend(metadata_sources)

        sql = f"""
            SELECT m.*,
                   (SELECT COUNT(*) FROM usage_sites u
                    WHERE u.symbol = m.name OR u.symbol = m.canonical_signature) AS usage_count
            FROM members m
            WHERE {" AND ".join(clauses)}
            ORDER BY
                CASE WHEN m.canonical_signature = ? THEN 0
                     WHEN m.name = ? COLLATE NOCASE THEN 1
                     ELSE 2 END,
                usage_count DESC,
                m.declaring_type,
                m.name,
                m.canonical_signature
            LIMIT ? OFFSET ?
        """
        params.extend((query, query, bounded_limit + 1, bounded_offset))
        with self.database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
            coverage_rows = connection.execute(
                "SELECT section, status FROM snapshot_coverage WHERE snapshot_id = ?",
                (selected,),
            ).fetchall()
        has_more = len(rows) > bounded_limit
        items = []
        for row in rows[:bounded_limit]:
            item = _row_dict(row)
            with self.database.connect() as connection:
                method_params = connection.execute(
                    """
                    SELECT position, name, type_name, by_ref, by_ptr
                    FROM method_params WHERE member_pk=?
                    ORDER BY position
                    """,
                    (item["member_pk"],),
                ).fetchall()
            item["member_ref"] = {
                "snapshot_id": selected,
                "kind": item["kind"],
                "canonical_signature": item["canonical_signature"],
                "member_id": item["member_pk"],
            }
            item["parameters"] = [_row_dict(param) for param in method_params]
            item["raw"] = json.loads(item.pop("raw_json"))
            item["verification_state"] = "static_only"
            items.append(item)
        return {
            "snapshot_id": selected,
            "items": items,
            "next_offset": bounded_offset + bounded_limit if has_more else None,
            "coverage": {str(row["section"]): str(row["status"]) for row in coverage_rows},
        }

    def find_type_dependencies(
        self,
        type_name: str,
        *,
        snapshot_id: str | None = None,
        direction: str = "both",
        max_depth: int = 2,
        edge_kinds: list[str] | None = None,
        max_nodes: int = 500,
    ) -> dict[str, Any]:
        selected = self.require_snapshot(snapshot_id)
        depth_limit = max(0, min(max_depth, 8))
        node_limit = max(1, min(max_nodes, 5000))
        if direction not in {"outgoing", "incoming", "both"}:
            raise ReframeworkMCPError(
                ErrorCode.INVALID_REQUEST,
                f"Invalid direction: {direction}",
            )

        queue: deque[tuple[str, int]] = deque([(type_name, 0)])
        seen = {type_name}
        discovered_edges: list[dict[str, Any]] = []
        with self.database.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM types WHERE snapshot_id = ? AND full_name = ?",
                (selected, type_name),
            ).fetchone()
            if not exists:
                raise ReframeworkMCPError(
                    ErrorCode.TYPE_NOT_FOUND,
                    f"Type not found: {type_name}",
                )
            while queue and len(seen) < node_limit:
                current, depth = queue.popleft()
                if depth >= depth_limit:
                    continue
                clauses = ["snapshot_id = ?"]
                params: list[Any] = [selected]
                if direction == "outgoing":
                    clauses.append("source_type = ?")
                    params.append(current)
                elif direction == "incoming":
                    clauses.append("target_type = ?")
                    params.append(current)
                else:
                    clauses.append("(source_type = ? OR target_type = ?)")
                    params.extend((current, current))
                if edge_kinds:
                    placeholders = ",".join("?" for _ in edge_kinds)
                    clauses.append(f"edge_kind IN ({placeholders})")
                    params.extend(edge_kinds)
                rows = connection.execute(
                    f"""
                    SELECT source_type, target_type, edge_kind, member_pk, metadata_json
                    FROM type_edges
                    WHERE {" AND ".join(clauses)}
                    """,
                    params,
                ).fetchall()
                for row in rows:
                    edge = _row_dict(row)
                    edge["depth"] = depth + 1
                    edge["metadata"] = json.loads(row["metadata_json"])
                    discovered_edges.append(edge)
                    neighbor = (
                        str(row["target_type"]) if row["source_type"] == current else str(row["source_type"])
                    )
                    if neighbor not in seen and len(seen) < node_limit:
                        seen.add(neighbor)
                        queue.append((neighbor, depth + 1))
        return {
            "snapshot_id": selected,
            "root": type_name,
            "direction": direction,
            "nodes": sorted(seen),
            "edges": discovered_edges,
            "truncated": len(seen) >= node_limit,
        }

    def member_by_pk(self, member_pk: int, snapshot_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM members WHERE member_pk = ? AND snapshot_id = ?",
                (member_pk, snapshot_id),
            ).fetchone()
            if row is None:
                return None
            params = connection.execute(
                "SELECT * FROM method_params WHERE member_pk = ? ORDER BY position",
                (member_pk,),
            ).fetchall()
        result = _row_dict(row)
        result["params"] = [_row_dict(param) for param in params]
        return result
