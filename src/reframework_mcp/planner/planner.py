"""Build evidence-ranked AccessPlan DAGs from the four C2 relationship graphs."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from reframework_mcp.errors import ErrorCode, ReframeworkMCPError
from reframework_mcp.graphs import RuntimeGraphStore
from reframework_mcp.metadata import MetadataRepository
from reframework_mcp.models import (
    AccessNode,
    AccessOperation,
    AccessPlan,
    MemberKind,
    MemberRef,
    PlanEvidence,
    RootKind,
    RootSpec,
)
from reframework_mcp.storage import Database


@dataclass(frozen=True, slots=True)
class PathEdge:
    source_type: str
    target_type: str
    edge_kind: str
    member_pk: int | None
    metadata: dict[str, Any]
    usage_count: int = 0
    runtime_count: int = 0


@dataclass(frozen=True, slots=True)
class RootCandidate:
    type_name: str
    kind: RootKind
    object_ref: str | None = None
    hook_ref: str | None = None
    argument_index: int | None = None
    source: str = "planner"
    confidence: float = 0.5
    reference: str | None = None


_ACCESSIBLE_EDGE_OPERATIONS: dict[str, AccessOperation] = {
    "FIELD_TYPE": AccessOperation.READ_FIELD,
    "PROPERTY_TYPE": AccessOperation.READ_PROPERTY,
    "REFLECTION_PROPERTY_TYPE": AccessOperation.READ_PROPERTY,
    "RETURN_TYPE": AccessOperation.CALL_METHOD,
    "REFLECTION_RETURN_TYPE": AccessOperation.CALL_METHOD,
    "INHERITS": AccessOperation.CAST,
    "GENERIC_ARGUMENT": AccessOperation.ITERATE,
    "RSZ_CONTAINS": AccessOperation.READ_FIELD,
}

_PRIMITIVE_TYPES = {
    "System.Boolean",
    "System.Byte",
    "System.SByte",
    "System.Int16",
    "System.UInt16",
    "System.Int32",
    "System.UInt32",
    "System.Int64",
    "System.UInt64",
    "System.Single",
    "System.Double",
    "System.Char",
    "System.String",
}

_MAX_PATH_CACHE_ENTRIES = 256
_MAX_REVERSE_EXPANSIONS = 2_000
_MAX_INCOMING_CANDIDATES = 1_024
_MAX_INCOMING_EDGES = 256


class AccessPlanner:
    def __init__(
        self,
        database: Database,
        metadata: MetadataRepository,
        runtime_graph: RuntimeGraphStore | None = None,
    ) -> None:
        self.database = database
        self.metadata = metadata
        self.runtime_graph = runtime_graph
        self._path_cache: dict[
            tuple[Any, ...],
            tuple[list[tuple[int, list[PathEdge]]], dict[str, Any]],
        ] = {}

    def find_paths(
        self,
        *,
        target_type: str | None = None,
        target_member_pk: int | None = None,
        snapshot_id: str | None = None,
        root_types: list[str] | None = None,
        allowed_root_kinds: list[str] | None = None,
        max_depth: int = 6,
        max_paths: int = 10,
        allow_getters: bool = True,
        allow_static_methods: bool = True,
        allow_hook_roots: bool = True,
        allow_usage_edges: bool = True,
        current_runtime_required: bool = False,
        runtime_epoch: str | None = None,
    ) -> dict[str, Any]:
        selected = self.metadata.require_snapshot(snapshot_id)
        target_member: dict[str, Any] | None = None
        if target_member_pk is not None:
            target_member = self.metadata.member_by_pk(target_member_pk, selected)
            if target_member is None:
                raise ReframeworkMCPError(
                    ErrorCode.MEMBER_NOT_FOUND,
                    f"Member not found: {target_member_pk}",
                )
            target_type = str(target_member["declaring_type"])
        if not target_type:
            raise ReframeworkMCPError(
                ErrorCode.INVALID_REQUEST,
                "target_type or target_member_pk is required",
            )

        roots = self._root_candidates(
            root_types=root_types,
            runtime_epoch=runtime_epoch,
            allow_hook_roots=allow_hook_roots,
            current_runtime_required=current_runtime_required,
        )
        if allowed_root_kinds:
            allowed = set(allowed_root_kinds)
            roots = [root for root in roots if root.kind.value in allowed]
        if not roots:
            raise ReframeworkMCPError(
                ErrorCode.PLAN_INVALID,
                "No eligible roots are available. Pass root_types, inspect live objects, or index MOD usage.",
            )

        path_limit = max(1, min(max_paths, 50))
        rooted_paths, traversal = self._rooted_paths(
            selected,
            roots,
            target_type,
            max_depth=max(1, min(max_depth, 10)),
            max_paths=path_limit,
            allow_getters=allow_getters,
            allow_static_methods=allow_static_methods,
            allow_usage_edges=allow_usage_edges,
            runtime_epoch=runtime_epoch,
        )

        plans = [
            self._build_plan(
                selected,
                root,
                target_type,
                path,
                target_member,
                roots,
                max_depth=max_depth,
                runtime_epoch=runtime_epoch,
            )
            for root, path in rooted_paths
        ]
        plans.sort(key=self._plan_score, reverse=True)
        for plan in plans:
            self.save_plan(plan)
        return {
            "snapshot_id": selected,
            "target_type": target_type,
            "target_member_pk": target_member_pk,
            "traversal": traversal,
            "roots_considered": [
                {
                    "type_name": root.type_name,
                    "kind": root.kind.value,
                    "object_ref": root.object_ref,
                    "source": root.source,
                    "confidence": root.confidence,
                }
                for root in roots
            ],
            "plans": [
                {
                    "plan_ref": plan.plan_ref,
                    "plan": plan.model_dump(mode="json"),
                    "unverified_assumptions": plan.assumptions,
                    "score": round(self._plan_score(plan), 6),
                    "evidence_sources": sorted(
                        {evidence.source for node in plan.nodes for evidence in node.evidence}
                    ),
                }
                for plan in plans
            ],
        }

    def save_plan(self, plan: AccessPlan) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO access_plans(
                    plan_ref, snapshot_id, game_id, goal, plan_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_ref,
                    plan.snapshot_id,
                    plan.game_id,
                    plan.goal,
                    plan.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()

    def load_plan(self, plan_ref: str) -> AccessPlan:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT plan_json FROM access_plans WHERE plan_ref = ?",
                (plan_ref,),
            ).fetchone()
        if row is None:
            raise ReframeworkMCPError(
                ErrorCode.PLAN_INVALID,
                f"Plan not found: {plan_ref}",
            )
        return AccessPlan.model_validate_json(row["plan_json"])

    def _root_candidates(
        self,
        *,
        root_types: list[str] | None,
        runtime_epoch: str | None,
        allow_hook_roots: bool,
        current_runtime_required: bool,
    ) -> list[RootCandidate]:
        if root_types:
            return [
                RootCandidate(
                    type_name=type_name,
                    kind=self._root_kind(type_name),
                    source="caller",
                    confidence=0.8,
                )
                for type_name in root_types
            ]

        roots: list[RootCandidate] = []
        if self.runtime_graph is not None:
            for item in self.runtime_graph.candidate_roots(
                runtime_epoch=runtime_epoch,
                current_only=True,
                include_hook_objects=allow_hook_roots,
            ):
                node_kind = str(item["node_kind"])
                evidence = item.get("evidence") or {}
                kind = {
                    "hook_this": RootKind.HOOK_THIS,
                    "hook_argument": RootKind.HOOK_ARGUMENT,
                    "hook_return": RootKind.HOOK_RETURN,
                }.get(node_kind, RootKind.PROVIDED_OBJECT)
                if kind in {
                    RootKind.HOOK_THIS,
                    RootKind.HOOK_ARGUMENT,
                    RootKind.HOOK_RETURN,
                } and not evidence.get("hook_ref"):
                    kind = RootKind.PROVIDED_OBJECT
                roots.append(
                    RootCandidate(
                        type_name=str(item["type_name"]),
                        kind=kind,
                        object_ref=str(item["object_ref"]),
                        hook_ref=(str(evidence.get("hook_ref")) if evidence.get("hook_ref") else None),
                        argument_index=(
                            int(evidence["index"])
                            if kind is RootKind.HOOK_ARGUMENT and evidence.get("index") is not None
                            else None
                        ),
                        source=(
                            "dynamic_hook_graph" if node_kind.startswith("hook_") else "runtime_object_graph"
                        ),
                        confidence=0.98 if node_kind == "singleton_instance" else 0.92,
                        reference=str(item["object_ref"]),
                    )
                )
        if not current_runtime_required:
            with self.database.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT type_name, root_kind, COUNT(*) AS evidence_count,
                           MIN(evidence) AS reference
                    FROM root_hints
                    GROUP BY type_name, root_kind
                    ORDER BY evidence_count DESC, type_name
                    """
                ).fetchall()
            for row in rows:
                roots.append(
                    RootCandidate(
                        type_name=str(row["type_name"]),
                        kind=RootKind(str(row["root_kind"])),
                        source="mod_usage_graph",
                        confidence=min(0.9, 0.62 + int(row["evidence_count"]) * 0.05),
                        reference=str(row["reference"] or ""),
                    )
                )

        deduplicated: list[RootCandidate] = []
        seen: set[tuple[str, str, str | None]] = set()
        for root in roots:
            key = (root.type_name, root.kind.value, root.object_ref)
            if key not in seen:
                seen.add(key)
                deduplicated.append(root)
        deduplicated.sort(key=lambda root: (-root.confidence, root.type_name, root.kind.value))
        return deduplicated[:100]

    def _rooted_paths(
        self,
        snapshot_id: str,
        roots: list[RootCandidate],
        target: str,
        *,
        max_depth: int,
        max_paths: int,
        allow_getters: bool,
        allow_static_methods: bool,
        allow_usage_edges: bool,
        runtime_epoch: str | None,
    ) -> tuple[list[tuple[RootCandidate, list[PathEdge]]], dict[str, Any]]:
        allowed = list(_ACCESSIBLE_EDGE_OPERATIONS)
        if not allow_getters:
            allowed = [
                value for value in allowed if value not in {"PROPERTY_TYPE", "REFLECTION_PROPERTY_TYPE"}
            ]
        root_signature = tuple(
            (
                root.type_name,
                root.kind.value,
                root.object_ref,
                root.hook_ref,
                root.argument_index,
                root.source,
                round(root.confidence, 6),
            )
            for root in roots
        )
        cache_key = (
            snapshot_id,
            root_signature,
            target,
            max_depth,
            max_paths,
            tuple(allowed),
            allow_static_methods,
            allow_usage_edges,
            runtime_epoch,
        )
        cached = self._path_cache.get(cache_key)
        if cached is not None:
            indexed_paths, traversal = cached
            return (
                [(roots[index], path) for index, path in indexed_paths],
                {**traversal, "cached": True},
            )

        indexed_paths, traversal = self._reverse_bfs(
            snapshot_id,
            roots,
            target,
            max_depth=max_depth,
            max_paths=max_paths,
            allowed=allowed,
            allow_static_methods=allow_static_methods,
            allow_usage_edges=allow_usage_edges,
            runtime_epoch=runtime_epoch,
        )
        if len(self._path_cache) >= _MAX_PATH_CACHE_ENTRIES:
            self._path_cache.pop(next(iter(self._path_cache)))
        self._path_cache[cache_key] = (indexed_paths, traversal)
        return (
            [(roots[index], path) for index, path in indexed_paths],
            {**traversal, "cached": False},
        )

    def _reverse_bfs(
        self,
        snapshot_id: str,
        roots: list[RootCandidate],
        target: str,
        *,
        max_depth: int,
        max_paths: int,
        allowed: list[str],
        allow_static_methods: bool,
        allow_usage_edges: bool,
        runtime_epoch: str | None,
    ) -> tuple[list[tuple[int, list[PathEdge]]], dict[str, Any]]:
        roots_by_type: dict[str, list[int]] = {}
        for index, root in enumerate(roots):
            roots_by_type.setdefault(root.type_name, []).append(index)

        found: list[tuple[int, list[PathEdge]]] = []
        for index in roots_by_type.get(target, [])[:max_paths]:
            found.append((index, []))
        found_keys: set[tuple[int, tuple[tuple[str, int | None, str], ...]]] = {
            (index, ()) for index, _ in found
        }
        queue: deque[tuple[str, list[PathEdge], frozenset[str]]] = deque([(target, [], frozenset({target}))])
        visits: dict[str, int] = {target: 1}
        expanded = 0
        placeholders = ",".join("?" for _ in allowed)
        with self.database.connect() as connection:
            while queue and len(found) < max_paths and expanded < _MAX_REVERSE_EXPANSIONS:
                current, suffix, visited = queue.popleft()
                if len(suffix) >= max_depth:
                    continue
                expanded += 1
                rows = connection.execute(
                    f"""
                    WITH candidate_edges AS (
                        SELECT e.source_type, e.target_type, e.edge_kind,
                               e.member_pk, e.metadata_json
                        FROM type_edges e
                        WHERE e.snapshot_id=? AND e.target_type=?
                          AND e.edge_kind IN ({placeholders})
                        ORDER BY
                          CASE e.edge_kind
                            WHEN 'FIELD_TYPE' THEN 0
                            WHEN 'PROPERTY_TYPE' THEN 1
                            WHEN 'RETURN_TYPE' THEN 2
                            ELSE 3
                          END,
                          e.source_type
                        LIMIT ?
                    )
                    SELECT e.source_type, e.target_type, e.edge_kind, e.member_pk,
                           e.metadata_json, m.name, m.canonical_signature,
                           m.flags, m.raw_json,
                           (SELECT COUNT(*) FROM usage_sites u
                            WHERE m.member_pk IS NOT NULL
                              AND (u.symbol=m.name
                                   OR u.symbol=m.canonical_signature)
                           ) AS usage_count,
                           (SELECT COUNT(*) FROM runtime_edges r
                            WHERE m.member_pk IS NOT NULL
                              AND r.member_signature=m.canonical_signature
                              AND (? IS NULL OR r.runtime_epoch=?)
                           ) AS runtime_count
                    FROM candidate_edges e
                    LEFT JOIN members m ON m.member_pk=e.member_pk
                    ORDER BY
                      runtime_count DESC,
                      CASE WHEN ? THEN usage_count ELSE 0 END DESC,
                      CASE e.edge_kind
                        WHEN 'FIELD_TYPE' THEN 0
                        WHEN 'PROPERTY_TYPE' THEN 1
                        WHEN 'RETURN_TYPE' THEN 2
                        ELSE 3
                      END,
                      e.source_type
                    LIMIT ?
                    """,
                    (
                        snapshot_id,
                        current,
                        *allowed,
                        _MAX_INCOMING_CANDIDATES,
                        runtime_epoch,
                        runtime_epoch,
                        int(allow_usage_edges),
                        _MAX_INCOMING_EDGES,
                    ),
                ).fetchall()
                for row in rows:
                    if (
                        not allow_static_methods
                        and row["edge_kind"]
                        in {
                            "RETURN_TYPE",
                            "REFLECTION_RETURN_TYPE",
                        }
                        and self._row_is_static(row)
                    ):
                        continue
                    source = str(row["source_type"])
                    if source in visited:
                        continue
                    metadata = json.loads(row["metadata_json"])
                    if int(row["usage_count"] or 0):
                        metadata["mod_usage_count"] = int(row["usage_count"])
                    if int(row["runtime_count"] or 0):
                        metadata["runtime_observation_count"] = int(row["runtime_count"])
                    edge = PathEdge(
                        source_type=str(row["source_type"]),
                        target_type=str(row["target_type"]),
                        edge_kind=str(row["edge_kind"]),
                        member_pk=row["member_pk"],
                        metadata=metadata,
                        usage_count=int(row["usage_count"] or 0),
                        runtime_count=int(row["runtime_count"] or 0),
                    )
                    next_path = [edge, *suffix]
                    for root_index in roots_by_type.get(source, []):
                        path_key = (
                            root_index,
                            tuple((item.edge_kind, item.member_pk, item.target_type) for item in next_path),
                        )
                        if path_key in found_keys:
                            continue
                        found_keys.add(path_key)
                        found.append((root_index, next_path))
                        if len(found) >= max_paths:
                            break
                    if len(found) >= max_paths:
                        break
                    if len(next_path) >= max_depth:
                        continue
                    visit_count = visits.get(source, 0)
                    if visit_count >= 2:
                        continue
                    visits[source] = visit_count + 1
                    queue.append((source, next_path, visited | {source}))
        found.sort(key=lambda item: (self._path_cost(item[1]), item[0]))
        return found[:max_paths], {
            "direction": "reverse_multi_root",
            "expanded_types": expanded,
            "truncated": bool(queue) and expanded >= _MAX_REVERSE_EXPANSIONS,
            "max_expanded_types": _MAX_REVERSE_EXPANSIONS,
            "max_incoming_edges_per_type": _MAX_INCOMING_EDGES,
        }

    def _build_plan(
        self,
        snapshot_id: str,
        root: RootCandidate,
        target_type: str,
        path: list[PathEdge],
        target_member: dict[str, Any] | None,
        all_roots: list[RootCandidate],
        *,
        max_depth: int,
        runtime_epoch: str | None,
    ) -> AccessPlan:
        nodes = [self._root_node("root", root)]
        assumptions: list[str] = []
        previous = self._append_path_nodes(
            nodes,
            path,
            snapshot_id,
            previous="root",
            prefix="step",
            assumptions=assumptions,
        )

        if target_member is not None:
            argument_nodes = self._append_argument_branches(
                nodes,
                snapshot_id,
                target_member,
                all_roots,
                assumptions,
                max_depth=max_depth,
                runtime_epoch=runtime_epoch,
            )
            kind = str(target_member["kind"])
            if kind in {"tdb_field", "rsz_field"}:
                operation = AccessOperation.READ_FIELD
            elif "property" in kind:
                operation = AccessOperation.READ_PROPERTY
            elif self._member_is_static(target_member):
                operation = AccessOperation.CALL_STATIC
            else:
                operation = AccessOperation.CALL_METHOD
            inputs = [] if operation is AccessOperation.CALL_STATIC else [previous]
            nodes.append(
                AccessNode(
                    node_id="target",
                    operation=operation,
                    inputs=inputs,
                    arguments=argument_nodes,
                    member=self._member_ref(snapshot_id, target_member),
                    output_type=target_member.get("return_type") or target_member.get("value_type"),
                    nullable=True,
                    side_effect="unknown",
                    options={
                        "parameters": target_member.get("params", []),
                        "requires_argument_binding": [
                            param["type_name"]
                            for param in target_member.get("params", [])
                            if self._argument_is_placeholder(nodes, argument_nodes, param)
                        ],
                    },
                    evidence=[
                        PlanEvidence(
                            source="metadata_snapshot",
                            reference=f"member:{target_member['member_pk']}",
                            confidence=0.9,
                        )
                    ],
                )
            )
            previous = "target"
            if operation in {AccessOperation.CALL_METHOD, AccessOperation.CALL_STATIC}:
                assumptions.append("target method side effects are unknown")

        return AccessPlan(
            snapshot_id=snapshot_id,
            goal=f"reach {target_member['canonical_signature'] if target_member else target_type}",
            nodes=nodes,
            targets=[previous],
            assumptions=list(dict.fromkeys(assumptions)),
        )

    def _append_path_nodes(
        self,
        nodes: list[AccessNode],
        path: list[PathEdge],
        snapshot_id: str,
        *,
        previous: str,
        prefix: str,
        assumptions: list[str],
    ) -> str:
        for position, edge in enumerate(path, start=1):
            operation = _ACCESSIBLE_EDGE_OPERATIONS[edge.edge_kind]
            member_ref: MemberRef | None = None
            if edge.member_pk is not None:
                member = self.metadata.member_by_pk(edge.member_pk, snapshot_id)
                if member is not None:
                    member_ref = self._member_ref(snapshot_id, member)
            node_id = f"{prefix}_{position}"
            evidence = [
                PlanEvidence(
                    source="metadata_snapshot",
                    reference=f"edge:{edge.edge_kind}:{edge.member_pk}",
                    confidence=0.8,
                )
            ]
            if edge.usage_count:
                evidence.append(
                    PlanEvidence(
                        source="mod_usage_graph",
                        reference=f"member:{edge.member_pk}",
                        confidence=min(0.92, 0.65 + edge.usage_count * 0.04),
                        details={"usage_count": edge.usage_count},
                    )
                )
            if edge.runtime_count:
                evidence.append(
                    PlanEvidence(
                        source="runtime_or_dynamic_graph",
                        reference=f"member:{edge.member_pk}",
                        confidence=0.98,
                        verified=True,
                        details={"observation_count": edge.runtime_count},
                    )
                )
            nodes.append(
                AccessNode(
                    node_id=node_id,
                    operation=operation,
                    inputs=[previous],
                    member=member_ref,
                    output_type=edge.target_type,
                    nullable=True,
                    side_effect=("read_like" if operation is not AccessOperation.CALL_METHOD else "unknown"),
                    options=edge.metadata,
                    evidence=evidence,
                )
            )
            if operation in {AccessOperation.CALL_METHOD, AccessOperation.READ_PROPERTY}:
                assumptions.append(f"{node_id} may execute code and requires live validation")
            previous = node_id
        return previous

    def _append_argument_branches(
        self,
        nodes: list[AccessNode],
        snapshot_id: str,
        target_member: dict[str, Any],
        roots: list[RootCandidate],
        assumptions: list[str],
        *,
        max_depth: int,
        runtime_epoch: str | None,
    ) -> list[str]:
        results: list[str] = []
        for index, param in enumerate(target_member.get("params", [])):
            type_name = str(param["type_name"])
            prefix = f"arg_{index}"
            container = self._container_operation(type_name)
            if container is not None:
                nodes.append(
                    AccessNode(
                        node_id=prefix,
                        operation=container,
                        output_type=type_name,
                        nullable=False,
                        side_effect="none",
                        options={
                            "parameter": param,
                            "empty": True,
                            "requires_user_population": True,
                        },
                        evidence=[PlanEvidence(source="planner", confidence=0.6)],
                    )
                )
                assumptions.append(f"{prefix} container contents require caller input")
                results.append(prefix)
                continue
            if type_name in _PRIMITIVE_TYPES or self._type_is_enum(snapshot_id, type_name):
                nodes.append(
                    AccessNode(
                        node_id=prefix,
                        operation=AccessOperation.BIND_CONSTANT,
                        value=None,
                        output_type=type_name,
                        nullable=type_name == "System.String",
                        side_effect="none",
                        options={
                            "parameter": param,
                            "placeholder": True,
                            "requires_user_value": True,
                        },
                        evidence=[PlanEvidence(source="planner", confidence=0.55)],
                    )
                )
                assumptions.append(f"{prefix} ({type_name}) requires an explicit value")
                results.append(prefix)
                continue

            branch = self._argument_root_and_path(
                snapshot_id,
                type_name,
                roots,
                max_depth=max_depth,
                runtime_epoch=runtime_epoch,
            )
            if branch is not None:
                root, path = branch
                root_id = f"{prefix}_root"
                nodes.append(self._root_node(root_id, root))
                result = self._append_path_nodes(
                    nodes,
                    path,
                    snapshot_id,
                    previous=root_id,
                    prefix=f"{prefix}_step",
                    assumptions=assumptions,
                )
                results.append(result)
                continue

            nodes.append(
                AccessNode(
                    node_id=prefix,
                    operation=AccessOperation.CONSTRUCT_OBJECT,
                    output_type=type_name,
                    nullable=False,
                    side_effect="allocation",
                    options={
                        "parameter": param,
                        "placeholder": True,
                        "constructor_unresolved": True,
                    },
                    evidence=[PlanEvidence(source="planner", confidence=0.3)],
                )
            )
            assumptions.append(f"{prefix} ({type_name}) has no known root path; constructor must be resolved")
            results.append(prefix)
        return results

    def _argument_root_and_path(
        self,
        snapshot_id: str,
        target_type: str,
        roots: list[RootCandidate],
        *,
        max_depth: int,
        runtime_epoch: str | None,
    ) -> tuple[RootCandidate, list[PathEdge]] | None:
        rooted_paths, _ = self._rooted_paths(
            snapshot_id,
            roots[:30],
            target_type,
            max_depth=max(1, min(max_depth, 8)),
            max_paths=1,
            allow_getters=True,
            allow_static_methods=True,
            allow_usage_edges=True,
            runtime_epoch=runtime_epoch,
        )
        return rooted_paths[0] if rooted_paths else None

    @staticmethod
    def _root_node(node_id: str, root: RootCandidate) -> AccessNode:
        if root.kind is RootKind.PROVIDED_OBJECT:
            spec = RootSpec(kind=root.kind, object_ref=root.object_ref)
        elif root.kind in {RootKind.HOOK_THIS, RootKind.HOOK_RETURN}:
            spec = RootSpec(kind=root.kind, hook_ref=root.hook_ref)
        elif root.kind is RootKind.HOOK_ARGUMENT:
            spec = RootSpec(
                kind=root.kind,
                hook_ref=root.hook_ref,
                argument_index=root.argument_index,
            )
        else:
            spec = RootSpec(kind=root.kind, type_name=root.type_name)
        return AccessNode(
            node_id=node_id,
            operation=AccessOperation.RESOLVE_ROOT,
            root=spec,
            output_type=root.type_name,
            nullable=False,
            side_effect="none",
            evidence=[
                PlanEvidence(
                    source=root.source,
                    reference=root.reference,
                    confidence=root.confidence,
                    verified=root.source in {"runtime_object_graph", "dynamic_hook_graph"},
                )
            ],
        )

    @staticmethod
    def _member_ref(snapshot_id: str, member: dict[str, Any]) -> MemberRef:
        return MemberRef(
            snapshot_id=snapshot_id,
            kind=MemberKind(str(member["kind"])),
            canonical_signature=str(member["canonical_signature"]),
            member_id=int(member["member_pk"]),
        )

    def _root_kind(self, type_name: str) -> RootKind:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT root_kind FROM root_hints
                WHERE type_name = ?
                ORDER BY CASE root_kind WHEN 'managed_singleton' THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (type_name,),
            ).fetchone()
        if row is None:
            return RootKind.STATIC_TYPE
        return RootKind(str(row["root_kind"]))

    def _type_is_enum(self, snapshot_id: str, type_name: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT flags, raw_json FROM types
                WHERE snapshot_id=? AND full_name=?
                """,
                (snapshot_id, type_name),
            ).fetchone()
        if row is None:
            return False
        raw = json.loads(row["raw_json"])
        return bool(raw.get("is_enum")) or "enum" in str(row["flags"] or "").casefold()

    @staticmethod
    def _container_operation(type_name: str) -> AccessOperation | None:
        if type_name.startswith(("System.Array<", "System.Array[")) or type_name.endswith("[]"):
            return AccessOperation.CONSTRUCT_ARRAY
        if "List<" in type_name:
            return AccessOperation.CONSTRUCT_LIST
        if "Dictionary<" in type_name:
            return AccessOperation.CONSTRUCT_DICTIONARY
        return None

    @staticmethod
    def _row_is_static(row: Any) -> bool:
        raw = json.loads(row["raw_json"] or "{}")
        return bool(raw.get("is_static")) or "static" in str(row["flags"] or "").casefold()

    @staticmethod
    def _member_is_static(member: dict[str, Any]) -> bool:
        raw_value = member.get("raw_json")
        raw = json.loads(raw_value) if isinstance(raw_value, str) else member.get("raw", {})
        return bool(raw.get("is_static")) or "static" in str(member.get("flags") or "").casefold()

    @staticmethod
    def _argument_is_placeholder(
        nodes: list[AccessNode],
        argument_nodes: list[str],
        param: dict[str, Any],
    ) -> bool:
        position = int(param.get("position", -1))
        if position < 0 or position >= len(argument_nodes):
            return True
        selected = next(
            (node for node in nodes if node.node_id == argument_nodes[position]),
            None,
        )
        return bool(selected and selected.options.get("placeholder"))

    @staticmethod
    def _path_cost(path: list[PathEdge]) -> float:
        cost = 0.0
        for edge in path:
            cost += {
                "FIELD_TYPE": 1.0,
                "RSZ_CONTAINS": 1.1,
                "PROPERTY_TYPE": 1.3,
                "REFLECTION_PROPERTY_TYPE": 1.5,
                "RETURN_TYPE": 1.8,
                "REFLECTION_RETURN_TYPE": 2.0,
                "GENERIC_ARGUMENT": 1.2,
                "INHERITS": 0.8,
            }.get(edge.edge_kind, 2.0)
            cost -= min(0.5, edge.usage_count * 0.05)
            cost -= min(0.8, edge.runtime_count * 0.1)
        return cost

    @staticmethod
    def _plan_score(plan: AccessPlan) -> float:
        evidence = [item for node in plan.nodes for item in node.evidence]
        evidence_score = sum(item.confidence + (0.15 if item.verified else 0.0) for item in evidence) / max(
            1, len(evidence)
        )
        side_effect_penalty = sum(
            0.08
            for node in plan.nodes
            if node.operation
            in {
                AccessOperation.CALL_METHOD,
                AccessOperation.CALL_STATIC,
            }
        )
        assumption_penalty = min(0.4, len(plan.assumptions) * 0.035)
        length_penalty = max(0, len(plan.nodes) - 1) * 0.012
        return max(
            0.0,
            min(
                1.0,
                evidence_score - side_effect_penalty - assumption_penalty - length_penalty,
            ),
        )
