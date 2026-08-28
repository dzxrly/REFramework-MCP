"""Evidence-ranked member search across metadata, MOD usage and live graphs."""

from __future__ import annotations

import json
import math
from typing import Any

from reframework_mcp.errors import ReframeworkMCPError
from reframework_mcp.graphs import RuntimeGraphStore
from reframework_mcp.metadata import MetadataRepository
from reframework_mcp.planner import AccessPlanner
from reframework_mcp.storage import Database


class MemberSearchService:
    """Turn metadata matches into Object Explorer-like exploration results."""

    def __init__(
        self,
        database: Database,
        metadata: MetadataRepository,
        planner: AccessPlanner,
        runtime_graph: RuntimeGraphStore,
    ) -> None:
        self.database = database
        self.metadata = metadata
        self.planner = planner
        self.runtime_graph = runtime_graph

    def search(
        self,
        query: str = "",
        *,
        snapshot_id: str | None = None,
        canonical_signature: str | None = None,
        declaring_type: str | None = None,
        member_kinds: list[str] | None = None,
        value_type: str | None = None,
        field_type: str | None = None,
        return_type: str | None = None,
        parameter_type: str | None = None,
        is_static: bool | None = None,
        is_instance: bool | None = None,
        hookable: bool | None = None,
        metadata_sources: list[str] | None = None,
        reachable_from: str | None = None,
        max_hops: int = 6,
        current_runtime_only: bool = False,
        observed_only: bool = False,
        game_id: str | None = None,
        runtime_epoch: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        selected = self.metadata.require_snapshot(snapshot_id)
        self._require_game(selected, game_id)
        bounded_limit = max(1, min(limit, 200))
        bounded_offset = max(0, offset)
        candidate_limit = min(max((bounded_offset + bounded_limit) * 5, 100), 1000)
        raw = self.metadata.search_members(
            query,
            snapshot_id=selected,
            canonical_signature=canonical_signature,
            declaring_type=declaring_type,
            member_kinds=member_kinds,
            value_type=field_type or value_type,
            return_type=return_type,
            parameter_type=parameter_type,
            is_static=is_static,
            is_instance=is_instance,
            hookable=hookable,
            metadata_sources=metadata_sources,
            limit=candidate_limit,
            offset=0,
        )

        candidates = [
            self._enrich(
                item,
                query=query,
                game_id=game_id,
                runtime_epoch=runtime_epoch,
                current_runtime_only=current_runtime_only,
            )
            for item in raw["items"]
        ]
        if current_runtime_only:
            candidates = [
                item
                for item in candidates
                if item["runtime_observation"]["observed"]
                and item["runtime_observation"]["runtime_epoch"] == runtime_epoch
            ]
        elif observed_only:
            candidates = [item for item in candidates if item["runtime_observation"]["observed"]]

        candidates.sort(
            key=lambda item: (
                -float(item["ranking"]["score"]),
                str(item["declaring_type"]),
                str(item["canonical_signature"]),
            )
        )
        path_budget = min(len(candidates), max(20, bounded_offset + bounded_limit))
        root_types = [reachable_from] if reachable_from else None
        for item in candidates[:path_budget]:
            summaries = self._path_summaries(
                int(item["member_pk"]),
                selected,
                root_types=root_types,
                max_hops=max_hops,
                runtime_epoch=runtime_epoch,
                current_runtime_required=current_runtime_only,
            )
            item["access_paths"] = summaries
            if summaries:
                item["ranking"]["signals"]["reachable_root_score"] = 1.0
                item["ranking"]["score"] = round(
                    min(1.0, float(item["ranking"]["score"]) + 0.12),
                    6,
                )
                item["ranking"]["reasons"].append(f"reachable from {summaries[0]['root']['type_name']}")
            else:
                item["ranking"]["signals"]["reachable_root_score"] = 0.0

        if reachable_from:
            candidates = [item for item in candidates if item.get("access_paths")]
        candidates.sort(
            key=lambda item: (
                -float(item["ranking"]["score"]),
                str(item["declaring_type"]),
                str(item["canonical_signature"]),
            )
        )
        page = candidates[bounded_offset : bounded_offset + bounded_limit]
        coverage = raw["coverage"]
        recommended_actions: list[dict[str, Any]] = []
        incomplete = sorted(
            section for section, status in coverage.items() if status not in {"complete", "present", "full"}
        )
        if incomplete:
            recommended_actions.append(
                {
                    "tool": "run_generate_sdk",
                    "reason": "active snapshot has partial metadata coverage",
                    "sections": incomplete,
                    "arguments": {
                        "mode": "json_only",
                        "policy": "reuse_if_fresh",
                        "activate_snapshot": True,
                        "index_after_export": True,
                    },
                }
            )
        return {
            "snapshot_id": selected,
            "game_id": self._snapshot_game(selected),
            "query": {
                "text": query,
                "canonical_signature": canonical_signature,
                "reachable_from": reachable_from,
                "current_runtime_only": current_runtime_only,
                "observed_only": observed_only,
            },
            "items": page,
            "total_candidates": len(candidates),
            "next_offset": (
                bounded_offset + bounded_limit if bounded_offset + bounded_limit < len(candidates) else None
            ),
            "coverage": coverage,
            "recommended_actions": recommended_actions,
        }

    def _enrich(
        self,
        item: dict[str, Any],
        *,
        query: str,
        game_id: str | None,
        runtime_epoch: str | None,
        current_runtime_only: bool,
    ) -> dict[str, Any]:
        signature = str(item["canonical_signature"])
        name = str(item["name"])
        usage = self._usage_examples(signature, name, game_id)
        relationships = self._member_relationships(int(item["member_pk"]))
        runtime = self.runtime_graph.member_evidence(
            signature,
            runtime_epoch=runtime_epoch,
            current_only=current_runtime_only,
        )
        validation_count = self._validated_plan_count(signature, str(item["snapshot_id"]))
        hook_layout = self._hook_layout(signature, runtime_epoch)

        lowered_query = query.casefold()
        lowered_name = name.casefold()
        lowered_signature = signature.casefold()
        if query and lowered_query == lowered_signature:
            lexical = 1.0
            lexical_reason = "exact canonical signature"
        elif query and lowered_query == lowered_name:
            lexical = 0.92
            lexical_reason = "exact member name"
        elif query and lowered_name.startswith(lowered_query):
            lexical = 0.75
            lexical_reason = "member-name prefix"
        elif query and lowered_query in lowered_signature:
            lexical = 0.58
            lexical_reason = "signature substring"
        elif not query:
            lexical = 0.3
            lexical_reason = "structural filter"
        else:
            lexical = 0.1
            lexical_reason = "weak lexical match"

        usage_score = min(1.0, math.log2(len(usage) + 1) / 3.0)
        live_score = 1.0 if runtime["observed"] else 0.0
        validated_score = min(1.0, validation_count / 2.0)
        structural = min(1.0, 0.25 + 0.1 * len(relationships))
        score = (
            lexical * 0.46
            + structural * 0.12
            + usage_score * 0.14
            + live_score * 0.2
            + validated_score * 0.08
        )
        reasons = [lexical_reason]
        if usage:
            reasons.append(f"{len(usage)} indexed MOD usage examples")
        if runtime["observed"]:
            reasons.append(f"{runtime['event_count']} Hook/runtime observations")
        if validation_count:
            reasons.append(f"{validation_count} validated AccessPlan records")
        verification_state = (
            "live_observed"
            if runtime["observed"] and current_runtime_only
            else "observed"
            if runtime["observed"]
            else "validated_plan"
            if validation_count
            else "static_only"
        )
        confidence = min(
            0.99,
            0.5
            + (0.2 if runtime["observed"] else 0.0)
            + (0.12 if usage else 0.0)
            + (0.12 if validation_count else 0.0)
            + (0.05 if item.get("address") else 0.0),
        )
        result = dict(item)
        result.update(
            {
                "relationships": relationships,
                "usage_examples": usage,
                "runtime_observation": runtime,
                "hook_argument_layout": hook_layout,
                "access_paths": [],
                "verification_state": verification_state,
                "confidence": round(confidence, 4),
                "ranking": {
                    "score": round(score, 6),
                    "signals": {
                        "lexical_match": lexical,
                        "structural_match": structural,
                        "reachable_root_score": 0.0,
                        "same_game_usage_score": usage_score,
                        "live_observation_score": live_score,
                        "validated_plan_score": validated_score,
                    },
                    "reasons": reasons,
                },
            }
        )
        return result

    def _usage_examples(
        self,
        signature: str,
        name: str,
        game_id: str | None,
    ) -> list[dict[str, Any]]:
        clauses = [
            """
            (
                u.symbol = ? COLLATE NOCASE
                OR u.symbol = ? COLLATE NOCASE
                OR u.source_excerpt LIKE ? COLLATE NOCASE
            )
            """
        ]
        params: list[Any] = [signature, name, f"%{name}%"]
        if game_id:
            clauses.append("p.game_id = ?")
            params.append(game_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT u.usage_kind, u.symbol, u.file_path, u.line, u.column_number,
                       u.chain_id, u.source_excerpt, u.metadata_json,
                       p.project_id, p.root_path, p.game_id, p.source_hash
                FROM usage_sites u
                JOIN usage_projects p ON p.project_id=u.project_id
                WHERE {" AND ".join(clauses)}
                ORDER BY CASE WHEN u.symbol=? THEN 0 WHEN u.symbol=? THEN 1 ELSE 2 END,
                         u.line
                LIMIT 10
                """,
                (*params, signature, name),
            ).fetchall()
        return [
            {
                "project_id": row["project_id"],
                "game_id": row["game_id"],
                "file": row["file_path"],
                "line": row["line"],
                "column": row["column_number"],
                "usage_kind": row["usage_kind"],
                "symbol": row["symbol"],
                "chain_id": row["chain_id"],
                "source_excerpt": row["source_excerpt"],
                "root_path": row["root_path"],
                "source_hash": row["source_hash"],
                "metadata": json.loads(row["metadata_json"]),
            }
            for row in rows
        ]

    def _member_relationships(self, member_pk: int) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT edge_kind, source_type, target_type, metadata_json
                FROM type_edges WHERE member_pk=?
                ORDER BY edge_kind, source_type, target_type
                """,
                (member_pk,),
            ).fetchall()
        return [
            {
                "edge_kind": row["edge_kind"],
                "source_type": row["source_type"],
                "target_type": row["target_type"],
                "metadata": json.loads(row["metadata_json"]),
            }
            for row in rows
        ]

    def _validated_plan_count(self, signature: str, snapshot_id: str) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(DISTINCT p.plan_ref)
                FROM access_plans p
                JOIN plan_validations v ON v.plan_ref=p.plan_ref
                WHERE p.snapshot_id=? AND v.status='valid'
                  AND p.plan_json LIKE ?
                """,
                (snapshot_id, f"%{signature}%"),
            ).fetchone()
        return int(row[0]) if row else 0

    def _hook_layout(
        self,
        signature: str,
        runtime_epoch: str | None,
    ) -> list[dict[str, Any]]:
        clauses = ["member_signature=?"]
        params: list[Any] = [signature]
        if runtime_epoch:
            clauses.append("runtime_epoch=?")
            params.append(runtime_epoch)
        with self.database.connect() as connection:
            row = connection.execute(
                f"""
                SELECT argument_layout_json FROM hook_sessions
                WHERE {" AND ".join(clauses)}
                ORDER BY updated_at DESC LIMIT 1
                """,
                params,
            ).fetchone()
        return json.loads(row["argument_layout_json"]) if row else []

    def _path_summaries(
        self,
        member_pk: int,
        snapshot_id: str,
        *,
        root_types: list[str] | None,
        max_hops: int,
        runtime_epoch: str | None,
        current_runtime_required: bool,
    ) -> list[dict[str, Any]]:
        try:
            result = self.planner.find_paths(
                target_member_pk=member_pk,
                snapshot_id=snapshot_id,
                root_types=root_types,
                max_depth=max_hops,
                max_paths=3,
                runtime_epoch=runtime_epoch,
                current_runtime_required=current_runtime_required,
            )
        except ReframeworkMCPError:
            return []
        summaries: list[dict[str, Any]] = []
        for candidate in result["plans"]:
            plan = candidate["plan"]
            root = plan["nodes"][0].get("root") or {}
            summaries.append(
                {
                    "plan_ref": candidate["plan_ref"],
                    "root": root,
                    "operations": [node["operation"] for node in plan["nodes"]],
                    "node_count": len(plan["nodes"]),
                    "unverified_assumptions": candidate["unverified_assumptions"],
                    "score": candidate.get("score"),
                    "evidence_sources": candidate.get("evidence_sources", []),
                }
            )
        return summaries

    def _require_game(self, snapshot_id: str, game_id: str | None) -> None:
        if not game_id:
            return
        actual = self._snapshot_game(snapshot_id)
        if actual and actual != game_id:
            from reframework_mcp.errors import ErrorCode

            raise ReframeworkMCPError(
                ErrorCode.SNAPSHOT_MISMATCH,
                "Selected snapshot belongs to a different game",
                details={"expected_game_id": game_id, "actual_game_id": actual},
            )

    def _snapshot_game(self, snapshot_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT game_id FROM snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
        return str(row["game_id"]) if row and row["game_id"] else None
