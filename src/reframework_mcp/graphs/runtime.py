"""Store runtime object, Hook and Probe observations without exposing pointers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from reframework_mcp.storage import Database


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RuntimeGraphStore:
    """Normalize transient Bridge responses into the two live evidence graphs."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def record_singletons(self, response: dict[str, Any]) -> dict[str, Any]:
        epoch = str(response.get("runtime_epoch") or "")
        if not epoch:
            return response
        with self.database.connect() as connection:
            for item in _dict_items(response.get("items")):
                object_ref = str(item.get("object_ref") or "")
                type_name = str(item.get("type_name") or "")
                root_kind = str(item.get("kind") or "singleton")
                if not object_ref:
                    continue
                self._upsert_node(
                    connection,
                    object_ref,
                    epoch,
                    type_name,
                    "singleton_instance",
                    {"root_kind": root_kind},
                )
                root_ref = self._synthetic_ref(epoch, "root", root_kind, type_name)
                self._upsert_node(
                    connection,
                    root_ref,
                    epoch,
                    type_name,
                    "singleton_root",
                    {"root_kind": root_kind},
                )
                self._insert_edge(
                    connection,
                    epoch,
                    root_ref,
                    object_ref,
                    "ROOT_INSTANCE",
                    metadata={"root_kind": root_kind, "type_name": type_name},
                )
            connection.commit()
        return response

    def record_inspection(self, response: dict[str, Any]) -> dict[str, Any]:
        epoch = str(response.get("runtime_epoch") or "")
        if not epoch:
            return response
        nodes = list(_dict_items(response.get("nodes")))
        with self.database.connect() as connection:
            for node in nodes:
                object_ref = str(node.get("object_ref") or "")
                if not object_ref:
                    continue
                self._upsert_node(
                    connection,
                    object_ref,
                    epoch,
                    str(node.get("type") or ""),
                    str(node.get("kind") or "runtime_object"),
                    {
                        "field_count": len(list(_dict_items(node.get("fields")))),
                        "source": "inspect_object",
                    },
                )
                for field in _dict_items(node.get("fields")):
                    value = field.get("value")
                    if isinstance(value, dict) and value.get("object_ref"):
                        target = str(value["object_ref"])
                        target_type = str(value.get("type") or field.get("type") or "")
                        self._upsert_node(
                            connection,
                            target,
                            epoch,
                            target_type,
                            "runtime_object",
                            {"source": "field_value"},
                        )
                        self._insert_edge(
                            connection,
                            epoch,
                            object_ref,
                            target,
                            "FIELD_VALUE",
                            member_signature=str(field.get("canonical_signature") or ""),
                            metadata={
                                "member": field.get("name"),
                                "declared_type": field.get("type"),
                            },
                        )
            for edge in _dict_items(response.get("edges")):
                source = str(edge.get("source_ref") or "")
                target = str(edge.get("target_ref") or "")
                if not source or not target:
                    continue
                raw_kind = str(edge.get("edge_kind") or "runtime").upper()
                self._insert_edge(
                    connection,
                    epoch,
                    source,
                    target,
                    {
                        "FIELD": "FIELD_VALUE",
                        "PROPERTY": "PROPERTY_VALUE",
                        "METHOD": "METHOD_RETURN",
                        "COLLECTION": "COLLECTION_ELEMENT",
                    }.get(raw_kind, raw_kind),
                    member_signature=str(edge.get("member_signature") or ""),
                    metadata={
                        key: value
                        for key, value in edge.items()
                        if key not in {"source_ref", "target_ref", "edge_kind", "member_signature"}
                    },
                )
            connection.commit()
        return response

    def record_hook_install(
        self,
        response: dict[str, Any],
        *,
        runtime_epoch: str | None,
    ) -> dict[str, Any]:
        hook_ref = str(response.get("hook_ref") or "")
        signature = str(response.get("member_signature") or "")
        epoch = str(response.get("runtime_epoch") or runtime_epoch or "")
        if not hook_ref or not epoch:
            return response
        now = _now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO hook_sessions(
                    hook_ref, runtime_epoch, member_signature, state,
                    argument_layout_json, installed_at, updated_at, stats_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hook_ref) DO UPDATE SET
                    state=excluded.state,
                    argument_layout_json=excluded.argument_layout_json,
                    updated_at=excluded.updated_at
                """,
                (
                    hook_ref,
                    epoch,
                    signature,
                    str(response.get("state") or "installed"),
                    json.dumps(response.get("argument_layout", []), ensure_ascii=False),
                    now,
                    now,
                    "{}",
                ),
            )
            connection.commit()
        return response

    def record_hook_payload(
        self,
        response: dict[str, Any],
        *,
        runtime_epoch: str | None,
    ) -> dict[str, Any]:
        hook_ref = str(response.get("hook_ref") or "")
        if not hook_ref:
            return response
        with self.database.connect() as connection:
            session = connection.execute(
                "SELECT * FROM hook_sessions WHERE hook_ref = ?",
                (hook_ref,),
            ).fetchone()
            epoch = str(
                response.get("runtime_epoch")
                or runtime_epoch
                or (session["runtime_epoch"] if session else "")
            )
            signature = str(
                response.get("member_signature") or (session["member_signature"] if session else "")
            )
            layout = (
                json.loads(session["argument_layout_json"])
                if session and session["argument_layout_json"]
                else []
            )
            layout_by_index = {int(item.get("argument_index", -1)) - 1: item for item in _dict_items(layout)}
            for event in _dict_items(response.get("events")):
                event_signature = str(event.get("member_signature") or signature)
                timestamp = str(event.get("timestamp") or _now())
                phase = str(event.get("phase") or "unknown")
                event_hash = _canonical_hash(
                    {"hook_ref": hook_ref, "event": event, "signature": event_signature}
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO hook_events(
                        hook_ref, runtime_epoch, timestamp, phase,
                        member_signature, payload_json, event_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        hook_ref,
                        epoch,
                        timestamp,
                        phase,
                        event_signature,
                        json.dumps(event, ensure_ascii=False),
                        event_hash,
                    ),
                )
                self._record_hook_event_graph(
                    connection,
                    epoch,
                    event_signature,
                    event,
                    layout_by_index,
                    hook_ref,
                )
            if session:
                connection.execute(
                    """
                    UPDATE hook_sessions
                    SET state=?, updated_at=?, stats_json=?
                    WHERE hook_ref=?
                    """,
                    (
                        str(response.get("state") or session["state"]),
                        _now(),
                        json.dumps(
                            {
                                key: response[key]
                                for key in ("calls", "dropped", "event_count")
                                if key in response
                            },
                            ensure_ascii=False,
                        ),
                        hook_ref,
                    ),
                )
            connection.commit()
        return response

    def record_probe_payload(
        self,
        response: dict[str, Any],
        *,
        runtime_epoch: str | None,
        validation_ref: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        probe_ref = str(response.get("probe_ref") or "")
        if not probe_ref:
            return response
        epoch = str(response.get("runtime_epoch") or runtime_epoch or "")
        now = _now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO probe_runs(
                    probe_ref, runtime_epoch, validation_ref, mode, state,
                    status_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(probe_ref) DO UPDATE SET
                    state=excluded.state,
                    status_json=excluded.status_json,
                    updated_at=excluded.updated_at
                """,
                (
                    probe_ref,
                    epoch or None,
                    validation_ref,
                    str(mode or response.get("mode") or "oneshot"),
                    str(response.get("state") or "completed"),
                    json.dumps(response, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            for event in _dict_items(response.get("events")):
                event_hash = _canonical_hash(event)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO probe_events(
                        probe_ref, runtime_epoch, timestamp, event_json, event_hash
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        probe_ref,
                        epoch or None,
                        str(event.get("timestamp") or now),
                        json.dumps(event, ensure_ascii=False),
                        event_hash,
                    ),
                )
                self._record_probe_value_graph(connection, epoch, probe_ref, event)
            connection.commit()
        return response

    def member_evidence(
        self,
        canonical_signature: str,
        *,
        runtime_epoch: str | None = None,
        current_only: bool = False,
    ) -> dict[str, Any]:
        clauses = ["member_signature = ?"]
        params: list[Any] = [canonical_signature]
        if current_only:
            if not runtime_epoch:
                return {
                    "observed": False,
                    "event_count": 0,
                    "runtime_epoch": None,
                    "observed_types": [],
                }
            clauses.append("runtime_epoch = ?")
            params.append(runtime_epoch)
        with self.database.connect() as connection:
            hook_rows = connection.execute(
                f"""
                SELECT runtime_epoch, COUNT(*) AS count, MAX(timestamp) AS last_seen
                FROM hook_events
                WHERE {" AND ".join(clauses)}
                GROUP BY runtime_epoch
                ORDER BY last_seen DESC
                """,
                params,
            ).fetchall()
            edge_clauses = ["member_signature = ?"]
            edge_params: list[Any] = [canonical_signature]
            if current_only and runtime_epoch:
                edge_clauses.append("runtime_epoch = ?")
                edge_params.append(runtime_epoch)
            edge_rows = connection.execute(
                f"""
                SELECT runtime_epoch, edge_kind, target_ref, metadata_json
                FROM runtime_edges
                WHERE {" AND ".join(edge_clauses)}
                  AND edge_kind LIKE 'OBSERVED_%'
                ORDER BY edge_pk DESC LIMIT 100
                """,
                edge_params,
            ).fetchall()
        observed_types: set[str] = set()
        observations: list[dict[str, Any]] = []
        for row in edge_rows:
            metadata = json.loads(row["metadata_json"])
            type_name = str(metadata.get("type_name") or "")
            if type_name:
                observed_types.add(type_name)
            observations.append(
                {
                    "runtime_epoch": row["runtime_epoch"],
                    "edge_kind": row["edge_kind"],
                    "target_ref": row["target_ref"],
                    "metadata": metadata,
                }
            )
        event_count = sum(int(row["count"]) for row in hook_rows)
        return {
            "observed": bool(event_count or observations),
            "event_count": event_count,
            "runtime_epoch": runtime_epoch if current_only else None,
            "epochs": [
                {
                    "runtime_epoch": row["runtime_epoch"],
                    "event_count": int(row["count"]),
                    "last_seen": row["last_seen"],
                }
                for row in hook_rows
            ],
            "observed_types": sorted(observed_types),
            "observations": observations[:20],
        }

    def candidate_roots(
        self,
        *,
        runtime_epoch: str | None,
        current_only: bool = True,
        include_hook_objects: bool = True,
    ) -> list[dict[str, Any]]:
        if current_only and not runtime_epoch:
            return []
        kinds = ["singleton_instance", "managed", "native", "runtime_object"]
        if include_hook_objects:
            kinds.extend(("hook_this", "hook_argument", "hook_return", "probe_object"))
        placeholders = ",".join("?" for _ in kinds)
        clauses = [f"node_kind IN ({placeholders})", "type_name IS NOT NULL", "type_name <> ''"]
        params: list[Any] = [*kinds]
        if current_only and runtime_epoch:
            clauses.append("runtime_epoch = ?")
            params.append(runtime_epoch)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT node_ref, runtime_epoch, type_name, node_kind, value_json
                FROM runtime_nodes
                WHERE {" AND ".join(clauses)}
                ORDER BY CASE node_kind
                    WHEN 'singleton_instance' THEN 0
                    WHEN 'hook_this' THEN 1
                    WHEN 'hook_argument' THEN 2
                    ELSE 3 END,
                    node_ref
                LIMIT 500
                """,
                params,
            ).fetchall()
        seen: set[tuple[str, str]] = set()
        result: list[dict[str, Any]] = []
        for row in rows:
            key = (str(row["type_name"]), str(row["node_ref"]))
            if key in seen:
                continue
            seen.add(key)
            details = json.loads(row["value_json"])
            result.append(
                {
                    "type_name": row["type_name"],
                    "object_ref": row["node_ref"],
                    "runtime_epoch": row["runtime_epoch"],
                    "node_kind": row["node_kind"],
                    "root_kind": details.get("root_kind", "provided_object_ref"),
                    "evidence": details,
                }
            )
        return result

    def hook_events(self, hook_ref: str, *, limit: int = 1000) -> dict[str, Any]:
        with self.database.connect() as connection:
            session = connection.execute(
                "SELECT * FROM hook_sessions WHERE hook_ref=?",
                (hook_ref,),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT timestamp, phase, member_signature, payload_json
                FROM hook_events WHERE hook_ref=?
                ORDER BY event_pk DESC LIMIT ?
                """,
                (hook_ref, max(1, min(limit, 10000))),
            ).fetchall()
        return {
            "hook_ref": hook_ref,
            "state": session["state"] if session else "unknown",
            "runtime_epoch": session["runtime_epoch"] if session else None,
            "member_signature": session["member_signature"] if session else None,
            "argument_layout": (json.loads(session["argument_layout_json"]) if session else []),
            "events": [json.loads(row["payload_json"]) for row in rows],
        }

    def probe_events(self, probe_ref: str, *, limit: int = 1000) -> dict[str, Any]:
        with self.database.connect() as connection:
            run = connection.execute(
                "SELECT * FROM probe_runs WHERE probe_ref=?",
                (probe_ref,),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT timestamp, event_json FROM probe_events
                WHERE probe_ref=? ORDER BY event_pk DESC LIMIT ?
                """,
                (probe_ref, max(1, min(limit, 10000))),
            ).fetchall()
        return {
            "probe_ref": probe_ref,
            "state": run["state"] if run else "unknown",
            "runtime_epoch": run["runtime_epoch"] if run else None,
            "status": json.loads(run["status_json"]) if run else None,
            "events": [json.loads(row["event_json"]) for row in rows],
        }

    def _record_hook_event_graph(
        self,
        connection: Any,
        epoch: str,
        signature: str,
        event: dict[str, Any],
        layout_by_index: dict[int, dict[str, Any]],
        hook_ref: str,
    ) -> None:
        if not epoch or not signature:
            return
        member_node = self._synthetic_ref(epoch, "member", signature)
        self._upsert_node(
            connection,
            member_node,
            epoch,
            "",
            "observed_member",
            {"canonical_signature": signature, "hook_ref": hook_ref},
        )
        for argument in _dict_items(event.get("arguments")):
            index = int(argument.get("index", -1))
            layout = layout_by_index.get(index, {})
            role = str(layout.get("role") or "parameter")
            has_this = any(str(item.get("role") or "") == "this" for item in layout_by_index.values())
            logical_index = index - 1 if role == "parameter" and has_this else index
            type_name = str(argument.get("type") or layout.get("type") or "")
            value = argument.get("value")
            edge_kind = "OBSERVED_THIS_TYPE" if role == "this" else "OBSERVED_ARGUMENT_TYPE"
            self._record_observed_value(
                connection,
                epoch,
                member_node,
                signature,
                value,
                type_name,
                edge_kind,
                "hook_this" if role == "this" else "hook_argument",
                {
                    "hook_ref": hook_ref,
                    "index": logical_index,
                    "raw_index": index,
                    "role": role,
                },
            )
        if "return_value" in event or event.get("return_type"):
            self._record_observed_value(
                connection,
                epoch,
                member_node,
                signature,
                event.get("return_value"),
                str(event.get("return_type") or ""),
                "OBSERVED_RETURN_TYPE",
                "hook_return",
                {"hook_ref": hook_ref},
            )

    def _record_observed_value(
        self,
        connection: Any,
        epoch: str,
        member_node: str,
        signature: str,
        value: Any,
        type_name: str,
        edge_kind: str,
        node_kind: str,
        metadata: dict[str, Any],
    ) -> None:
        object_ref = str(value.get("object_ref") or "") if isinstance(value, dict) else ""
        if object_ref:
            target_ref = object_ref
            self._upsert_node(
                connection,
                target_ref,
                epoch,
                type_name,
                node_kind,
                metadata,
            )
        else:
            target_ref = self._synthetic_ref(epoch, "type", type_name or "unknown")
            self._upsert_node(
                connection,
                target_ref,
                epoch,
                type_name,
                "observed_type",
                metadata,
            )
        self._insert_edge(
            connection,
            epoch,
            member_node,
            target_ref,
            edge_kind,
            member_signature=signature,
            metadata={**metadata, "type_name": type_name},
        )

    def _record_probe_value_graph(
        self,
        connection: Any,
        epoch: str,
        probe_ref: str,
        value: Any,
    ) -> None:
        if not epoch:
            return
        stack: list[Any] = [value]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                object_ref = current.get("object_ref")
                if object_ref:
                    type_name = str(current.get("type") or current.get("type_name") or "")
                    self._upsert_node(
                        connection,
                        str(object_ref),
                        epoch,
                        type_name,
                        "probe_object",
                        {"probe_ref": probe_ref},
                    )
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)

    @staticmethod
    def _synthetic_ref(epoch: str, category: str, *parts: str) -> str:
        digest = _canonical_hash([epoch, category, *parts])[:24]
        return f"{category}:{epoch}:{digest}"

    @staticmethod
    def _upsert_node(
        connection: Any,
        node_ref: str,
        epoch: str,
        type_name: str,
        node_kind: str,
        value: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO runtime_nodes(
                node_ref, runtime_epoch, type_name, node_kind, value_json
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(node_ref) DO UPDATE SET
                type_name=CASE WHEN excluded.type_name <> ''
                    THEN excluded.type_name ELSE runtime_nodes.type_name END,
                node_kind=excluded.node_kind,
                value_json=excluded.value_json
            """,
            (
                node_ref,
                epoch,
                type_name or None,
                node_kind,
                json.dumps(value, ensure_ascii=False),
            ),
        )

    @staticmethod
    def _insert_edge(
        connection: Any,
        epoch: str,
        source_ref: str,
        target_ref: str,
        edge_kind: str,
        *,
        member_signature: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        encoded = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        exists = connection.execute(
            """
            SELECT 1 FROM runtime_edges
            WHERE runtime_epoch=? AND source_ref=? AND target_ref=?
              AND edge_kind=? AND COALESCE(member_signature, '')=?
              AND metadata_json=?
            """,
            (
                epoch,
                source_ref,
                target_ref,
                edge_kind,
                member_signature,
                encoded,
            ),
        ).fetchone()
        if exists is None:
            connection.execute(
                """
                INSERT INTO runtime_edges(
                    runtime_epoch, source_ref, target_ref, edge_kind,
                    member_signature, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    epoch,
                    source_ref,
                    target_ref,
                    edge_kind,
                    member_signature or None,
                    encoded,
                ),
            )


def _dict_items(value: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(value, list):
        return ()
    return (item for item in value if isinstance(item, dict))
