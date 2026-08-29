"""MCPServer registration for the C2 tool and resource contracts."""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from pydantic import BaseModel, Field

from reframework_mcp import __version__
from reframework_mcp.errors import ErrorCode, ReframeworkMCPError, success
from reframework_mcp.models.tool_results import (
    AccessPathsResult,
    DescribeTypeResult,
    GenerateSdkResult,
    HookInstallResult,
    HookRemoveResult,
    InvokeMethodResult,
    LuaDraftResult,
    LuaValidationResult,
    ObjectInspectionResult,
    PlanValidationResult,
    ProbeRunResult,
    RuntimeStatusResult,
    SearchMembersResult,
    SearchTypesResult,
    SetFieldResult,
    SingletonListResult,
    TypeDependenciesResult,
    UsageExamplesResult,
)
from reframework_mcp.services import ApplicationServices

SERVER_INSTRUCTIONS = """
REFramework-MCP explores metadata and live objects in RE Engine games.
Use run_generate_sdk to create a complete metadata snapshot, then search members
and build an AccessPlan. Validate a plan before running probes or changing game
state. Raw addresses are diagnostic data, not stable references.
""".strip()


class MutationApproval(BaseModel):
    approve: bool = Field(
        description="Approve this exact, short-lived mutation request",
    )


def create_server(services: ApplicationServices) -> MCPServer:
    mcp = MCPServer(
        "REFramework-MCP",
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
    )

    @mcp.tool()
    async def runtime_status(probe_bridge: bool = True) -> RuntimeStatusResult:
        """Get bridge, game, export, snapshot, index, epoch and policy status."""

        return cast(
            RuntimeStatusResult,
            await services.with_errors(
                "runtime_status",
                {"probe_bridge": probe_bridge},
                lambda: services.runtime_status(probe_bridge=probe_bridge),
            ),
        )

    @mcp.tool()
    async def run_generate_sdk(
        mode: str,
        ctx: Context,
        policy: str = "reuse_if_fresh",
        activate_snapshot: bool = True,
        index_after_export: bool = True,
        approval_ref: str | None = None,
    ) -> GenerateSdkResult:
        """Run REFramework SDK export as an asynchronous JSON-only or SDK+JSON job."""

        arguments: dict[str, Any] = {
            "mode": mode,
            "policy": policy,
            "activate_snapshot": activate_snapshot,
            "index_after_export": index_after_export,
        }

        async def operation() -> dict[str, Any]:
            effective_approval_ref = approval_ref
            approval = services.policy.check_generate_sdk(
                arguments,
                effective_approval_ref,
                services.bridge.runtime_epoch,
            )
            if approval:
                effective_approval_ref = await _elicit_approval(
                    ctx,
                    approval,
                    summary={
                        **arguments,
                        "writes_local_artifacts": True,
                    },
                )
                if effective_approval_ref is None:
                    return _approval_required(
                        approval,
                        {
                            **arguments,
                            "writes_local_artifacts": True,
                        },
                    )
            return await services.run_generate_sdk(
                mode=mode,
                policy=policy,
                activate_snapshot=activate_snapshot,
                index_after_export=index_after_export,
                approval_ref=effective_approval_ref,
            )

        return cast(
            GenerateSdkResult,
            await services.with_errors(
                "run_generate_sdk",
                arguments,
                operation,
            ),
        )

    @mcp.tool()
    async def search_types(
        query: str,
        snapshot_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> SearchTypesResult:
        """Search types in an imported metadata snapshot."""

        arguments: dict[str, Any] = {
            "query": query,
            "snapshot_id": snapshot_id,
            "limit": limit,
            "offset": offset,
        }
        return cast(
            SearchTypesResult,
            await services.with_errors(
                "search_types",
                arguments,
                lambda: success(services.metadata.search_types(**arguments)),
            ),
        )

    @mcp.tool()
    async def describe_type(
        full_name: str,
        snapshot_id: str | None = None,
        member_limit: int = 500,
    ) -> DescribeTypeResult:
        """Describe one type, its members, dependencies and metadata coverage."""

        arguments: dict[str, Any] = {
            "full_name": full_name,
            "snapshot_id": snapshot_id,
            "member_limit": member_limit,
        }
        return cast(
            DescribeTypeResult,
            await services.with_errors(
                "describe_type",
                arguments,
                lambda: success(services.metadata.describe_type(**arguments)),
            ),
        )

    @mcp.tool()
    async def search_members(
        query: str = "",
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
        limit: int = 50,
        offset: int = 0,
    ) -> SearchMembersResult:
        """Search members with static, MOD usage, reachability and live evidence."""

        arguments: dict[str, Any] = {
            "query": query,
            "snapshot_id": snapshot_id,
            "canonical_signature": canonical_signature,
            "declaring_type": declaring_type,
            "member_kinds": member_kinds,
            "value_type": value_type,
            "field_type": field_type,
            "return_type": return_type,
            "parameter_type": parameter_type,
            "is_static": is_static,
            "is_instance": is_instance,
            "hookable": hookable,
            "metadata_sources": metadata_sources,
            "reachable_from": reachable_from,
            "max_hops": max_hops,
            "current_runtime_only": current_runtime_only,
            "observed_only": observed_only,
            "game_id": game_id,
            "limit": limit,
            "offset": offset,
        }
        search_arguments: dict[str, Any] = {
            **arguments,
            "runtime_epoch": services.bridge.runtime_epoch,
        }
        return cast(
            SearchMembersResult,
            await services.with_errors(
                "search_members",
                arguments,
                lambda: success(services.member_search.search(**search_arguments)),
            ),
        )

    @mcp.tool()
    async def find_type_dependencies(
        type_name: str,
        snapshot_id: str | None = None,
        direction: str = "both",
        max_depth: int = 2,
        edge_kinds: list[str] | None = None,
        max_nodes: int = 500,
    ) -> TypeDependenciesResult:
        """Traverse static type and member dependency edges."""

        arguments: dict[str, Any] = {
            "type_name": type_name,
            "snapshot_id": snapshot_id,
            "direction": direction,
            "max_depth": max_depth,
            "edge_kinds": edge_kinds,
            "max_nodes": max_nodes,
        }
        return cast(
            TypeDependenciesResult,
            await services.with_errors(
                "find_type_dependencies",
                arguments,
                lambda: success(services.metadata.find_type_dependencies(**arguments)),
            ),
        )

    @mcp.tool()
    async def list_singletons(
        kinds: list[str] | None = None,
        type_query: str | None = None,
        limit: int = 500,
    ) -> SingletonListResult:
        """List live managed/native singleton roots and return ObjectRefs."""

        arguments: dict[str, Any] = {
            "kinds": kinds,
            "type_query": type_query,
            "limit": limit,
        }
        return cast(
            SingletonListResult,
            await services.with_errors(
                "list_singletons",
                arguments,
                lambda: _runtime_read(
                    services,
                    "list_singletons",
                    {
                        "kinds": kinds or ["managed", "native"],
                        "type_query": type_query or "",
                        "limit": max(1, min(limit, 5000)),
                    },
                ),
            ),
        )

    @mcp.tool()
    async def inspect_object(
        object_ref: str,
        depth: int = 1,
        member_filter: str | None = None,
        include_properties: bool = False,
        allow_getters: bool = False,
        getter_allowlist: list[str] | None = None,
        collection_offset: int = 0,
        collection_limit: int = 100,
        max_nodes: int = 500,
        max_bytes: int = 1_048_576,
    ) -> ObjectInspectionResult:
        """Inspect a live object with bounded traversal and cycle detection."""

        arguments: dict[str, Any] = {
            "object_ref": object_ref,
            "depth": max(0, min(depth, 4)),
            "member_filter": member_filter or "",
            "include_properties": include_properties,
            "allow_getters": allow_getters,
            "getter_allowlist": getter_allowlist or [],
            "collection_offset": max(0, min(collection_offset, 100_000)),
            "collection_limit": max(1, min(collection_limit, 1000)),
            "max_nodes": max(1, min(max_nodes, 5000)),
            "max_bytes": max(4096, min(max_bytes, 8 * 1024 * 1024)),
        }
        return cast(
            ObjectInspectionResult,
            await services.with_errors(
                "inspect_object",
                arguments,
                lambda: _runtime_read(services, "inspect_object", arguments),
            ),
        )

    @mcp.tool()
    async def search_usage_examples(
        query: str,
        game_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> UsageExamplesResult:
        """Search syntax-aware usage extracted from configured Lua MOD projects."""

        arguments: dict[str, Any] = {
            "query": query,
            "game_id": game_id,
            "limit": limit,
            "offset": offset,
        }
        return cast(
            UsageExamplesResult,
            await services.with_errors(
                "search_usage_examples",
                arguments,
                lambda: success(services.usage.search(**arguments)),
            ),
        )

    @mcp.tool()
    async def find_access_paths(
        target_type: str | None = None,
        target_member_id: int | None = None,
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
    ) -> AccessPathsResult:
        """Fuse four graphs into ranked, typed AccessPlan DAG candidates."""

        arguments: dict[str, Any] = {
            "target_type": target_type,
            "target_member_id": target_member_id,
            "snapshot_id": snapshot_id,
            "root_types": root_types,
            "allowed_root_kinds": allowed_root_kinds,
            "max_depth": max_depth,
            "max_paths": max_paths,
            "allow_getters": allow_getters,
            "allow_static_methods": allow_static_methods,
            "allow_hook_roots": allow_hook_roots,
            "allow_usage_edges": allow_usage_edges,
            "current_runtime_required": current_runtime_required,
        }
        return cast(
            AccessPathsResult,
            await services.with_errors(
                "find_access_paths",
                arguments,
                lambda: success(
                    services.planner.find_paths(
                        target_type=target_type,
                        target_member_pk=target_member_id,
                        snapshot_id=snapshot_id,
                        root_types=root_types,
                        allowed_root_kinds=allowed_root_kinds,
                        max_depth=max_depth,
                        max_paths=max_paths,
                        allow_getters=allow_getters,
                        allow_static_methods=allow_static_methods,
                        allow_hook_roots=allow_hook_roots,
                        allow_usage_edges=allow_usage_edges,
                        current_runtime_required=current_runtime_required,
                        runtime_epoch=services.bridge.runtime_epoch,
                    )
                ),
            ),
        )

    @mcp.tool()
    async def validate_access_plan(
        plan_ref: str | None = None,
        plan: dict[str, Any] | None = None,
        live: bool = True,
        allow_getters: bool = False,
    ) -> PlanValidationResult:
        """Validate an AccessPlan symbolically and, when connected, node by node in game."""

        arguments: dict[str, Any] = {
            "plan_ref": plan_ref,
            "plan": plan,
            "live": live,
            "allow_getters": allow_getters,
        }

        async def operation() -> dict[str, Any]:
            selected = services.load_plan(plan_ref, plan)
            result = await services.plan_validator.validate(
                selected,
                live=live,
                allow_getters=allow_getters,
            )
            return success(result.model_dump(mode="json"))

        return cast(
            PlanValidationResult,
            await services.with_errors("validate_access_plan", arguments, operation),
        )

    @mcp.tool()
    async def draft_lua_probe(
        plan_ref: str | None = None,
        plan: dict[str, Any] | None = None,
        mode: str = "read_only",
        emit_targets: bool = True,
    ) -> LuaDraftResult:
        """Draft Lua from an indexed AccessPlan without executing it."""

        arguments: dict[str, Any] = {
            "plan_ref": plan_ref,
            "plan": plan,
            "mode": mode,
            "emit_targets": emit_targets,
        }
        return cast(
            LuaDraftResult,
            await services.with_errors(
                "draft_lua_probe",
                arguments,
                lambda: success(
                    services.lua_draft.draft(
                        services.load_plan(plan_ref, plan),
                        mode=mode,
                        emit_targets=emit_targets,
                    )
                ),
            ),
        )

    @mcp.tool()
    async def validate_lua_probe(
        code: str,
        mode: str = "oneshot",
        snapshot_id: str | None = None,
        plan_ref: str | None = None,
        live_compile: bool = True,
    ) -> LuaValidationResult:
        """Validate Lua syntax, indexed symbols, risks, lifecycle and live compilation."""

        arguments: dict[str, Any] = {
            "code_sha256": _sha256_text(code),
            "mode": mode,
            "snapshot_id": snapshot_id,
            "plan_ref": plan_ref,
            "live_compile": live_compile,
        }

        async def operation() -> dict[str, Any]:
            result = await services.lua_validation.validate(
                code,
                mode=mode,
                snapshot_id=snapshot_id or services.metadata.active_snapshot_id(),
                plan_ref=plan_ref,
                runtime_epoch=services.bridge.runtime_epoch,
                live_compile=live_compile,
            )
            return success(result)

        return cast(
            LuaValidationResult,
            await services.with_errors(
                "validate_lua_probe",
                arguments,
                operation,
            ),
        )

    @mcp.tool()
    async def invoke_method(
        object_ref: str | None,
        member_ref: dict[str, Any],
        plan_validation_ref: str,
        ctx: Context,
        arguments: list[Any] | None = None,
        execution_phase: str | None = None,
        expected_runtime_type: str | None = None,
        approval_ref: str | None = None,
    ) -> InvokeMethodResult:
        """Invoke an exact member on a validated object after policy approval."""

        payload = {
            "object_ref": object_ref,
            "member_ref": member_ref,
            "plan_validation_ref": plan_validation_ref,
            "arguments": arguments or [],
            "execution_phase": execution_phase,
            "expected_runtime_type": expected_runtime_type or "",
        }
        return cast(
            InvokeMethodResult,
            await services.with_errors(
                "invoke_method",
                payload,
                lambda: _mutation(
                    services,
                    "invoke_method",
                    payload,
                    approval_ref,
                    ctx,
                ),
            ),
        )

    @mcp.tool()
    async def set_field(
        object_ref: str,
        member_ref: dict[str, Any],
        plan_validation_ref: str,
        value: Any,
        ctx: Context,
        expected_old_value: Any = None,
        execution_phase: str | None = None,
        approval_ref: str | None = None,
    ) -> SetFieldResult:
        """Set an exact field after type and old-value checks and policy approval."""

        payload = {
            "object_ref": object_ref,
            "member_ref": member_ref,
            "plan_validation_ref": plan_validation_ref,
            "value": value,
            "expected_old_value": expected_old_value,
            "execution_phase": execution_phase,
        }
        return cast(
            SetFieldResult,
            await services.with_errors(
                "set_field",
                payload,
                lambda: _mutation(
                    services,
                    "set_field",
                    payload,
                    approval_ref,
                    ctx,
                ),
            ),
        )

    @mcp.tool()
    async def run_lua_probe(
        code: str,
        validation_ref: str,
        ctx: Context,
        mode: str = "oneshot",
        timeout_seconds: float = 5.0,
        max_frames: int = 300,
        max_instructions: int = 1_000_000,
        max_events: int = 1000,
        approval_ref: str | None = None,
    ) -> ProbeRunResult:
        """Run code bound to a valid Lua validation token in an isolated probe state."""

        arguments: dict[str, Any] = {
            "code_sha256": _sha256_text(code),
            "validation_ref": validation_ref,
            "mode": mode,
            "timeout_seconds": timeout_seconds,
            "max_frames": max_frames,
            "max_instructions": max_instructions,
            "max_events": max_events,
        }

        async def operation() -> dict[str, Any]:
            services.lua_validation.require_valid(
                validation_ref,
                code,
                runtime_epoch=services.bridge.runtime_epoch,
            )
            payload = {
                "code": code,
                "validation_ref": validation_ref,
                "mode": mode,
                "timeout_seconds": min(max(timeout_seconds, 0.1), 30.0),
                "max_frames": min(max(max_frames, 1), 1800),
                "max_instructions": min(
                    max(max_instructions, 10_000),
                    10_000_000,
                ),
                "max_events": min(max(max_events, 1), 10000),
            }
            if mode == "windowed_hook_test":
                approval = services.policy.require_mutation(
                    "run_lua_probe.windowed_hook_test",
                    arguments,
                    approval_ref,
                    services.bridge.runtime_epoch,
                )
                if approval:
                    elicited_ref = await _elicit_approval(
                        ctx,
                        approval,
                        summary=arguments,
                    )
                    if elicited_ref is None:
                        return _approval_required(approval, arguments)
                    services.policy.require_mutation(
                        "run_lua_probe.windowed_hook_test",
                        arguments,
                        elicited_ref,
                        services.bridge.runtime_epoch,
                    )
            result = await services.bridge.call("run_lua_probe", payload)
            services.runtime_graph.record_probe_payload(
                result,
                runtime_epoch=services.bridge.runtime_epoch,
                validation_ref=validation_ref,
                mode=mode,
            )
            return success(result)

        return cast(
            ProbeRunResult,
            await services.with_errors("run_lua_probe", arguments, operation),
        )

    @mcp.tool()
    async def install_hook(
        member_ref: dict[str, Any],
        ctx: Context,
        capture: list[str] | None = None,
        phase: str = "both",
        sample_rate: float = 1.0,
        max_events: int = 1000,
        ttl_seconds: float = 30.0,
        transform: dict[str, Any] | None = None,
        approval_ref: str | None = None,
    ) -> HookInstallResult:
        """Install a bounded observation hook or an explicitly approved transform hook."""

        payload = {
            "member_ref": member_ref,
            "capture": capture or ["this", "arguments", "return"],
            "phase": phase,
            "sample_rate": min(max(sample_rate, 0.0), 1.0),
            "max_events": min(max(max_events, 1), 100000),
            "ttl_seconds": min(max(ttl_seconds, 0.1), 3600.0),
            "transform": transform,
        }

        async def operation() -> dict[str, Any]:
            if transform is not None:
                approval = services.policy.require_mutation(
                    "install_hook.transform",
                    payload,
                    approval_ref,
                    services.bridge.runtime_epoch,
                )
                if approval:
                    elicited_ref = await _elicit_approval(
                        ctx,
                        approval,
                        summary=payload,
                    )
                    if elicited_ref is None:
                        return _approval_required(approval, payload)
                    services.policy.require_mutation(
                        "install_hook.transform",
                        payload,
                        elicited_ref,
                        services.bridge.runtime_epoch,
                    )
            elif not services.settings.policy.allow_observe_hooks:
                raise ReframeworkMCPError(
                    ErrorCode.POLICY_DENIED,
                    "Observation hooks are disabled by policy",
                )
            result = await services.bridge.call("install_hook", payload)
            services.runtime_graph.record_hook_install(
                result,
                runtime_epoch=services.bridge.runtime_epoch,
            )
            return success(result)

        return cast(
            HookInstallResult,
            await services.with_errors("install_hook", payload, operation),
        )

    @mcp.tool()
    async def remove_hook(hook_ref: str) -> HookRemoveResult:
        """Idempotently remove a hook owned by this server."""

        return cast(
            HookRemoveResult,
            await services.with_errors(
                "remove_hook",
                {"hook_ref": hook_ref},
                lambda: _remove_hook(services, hook_ref),
            ),
        )

    @mcp.resource(
        "reframework://metadata/exports/{job_ref}",
        mime_type="application/json",
    )
    async def export_status_resource(job_ref: str) -> dict[str, Any]:
        """Read current state and progress for a Generate SDK job."""

        if services.bridge.connected:
            with suppress(ReframeworkMCPError):
                await services.exports.refresh(job_ref)
        return services.exports.status(job_ref)

    @mcp.resource(
        "reframework://metadata/snapshots/{snapshot_id}/manifest",
        mime_type="application/json",
    )
    def snapshot_manifest_resource(snapshot_id: str) -> dict[str, Any]:
        """Read one imported snapshot manifest and coverage."""

        with services.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                raise ReframeworkMCPError(
                    ErrorCode.SNAPSHOT_NOT_FOUND,
                    f"Snapshot not found: {snapshot_id}",
                )
            coverage = connection.execute(
                "SELECT section, status, details_json FROM snapshot_coverage WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchall()
        return {
            "snapshot": {str(key): row[index] for index, key in enumerate(row.keys())},
            "coverage": [
                {
                    "section": item["section"],
                    "status": item["status"],
                    "details": json.loads(item["details_json"]),
                }
                for item in coverage
            ],
        }

    @mcp.resource(
        "reframework://metadata/snapshots/{snapshot_id}/coverage",
        mime_type="application/json",
    )
    def snapshot_coverage_resource(snapshot_id: str) -> dict[str, Any]:
        """Read normalized coverage for one imported metadata snapshot."""

        with services.database.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if exists is None:
                raise ReframeworkMCPError(
                    ErrorCode.SNAPSHOT_NOT_FOUND,
                    f"Snapshot not found: {snapshot_id}",
                )
            rows = connection.execute(
                """
                SELECT section, status, details_json
                FROM snapshot_coverage
                WHERE snapshot_id = ?
                ORDER BY section
                """,
                (snapshot_id,),
            ).fetchall()
        return {
            "snapshot_id": snapshot_id,
            "sections": [
                {
                    "section": row["section"],
                    "status": row["status"],
                    "details": json.loads(row["details_json"]),
                }
                for row in rows
            ],
        }

    @mcp.resource(
        "reframework://explorations/{exploration_id}/graph",
        mime_type="application/json",
    )
    def exploration_graph_resource(exploration_id: str) -> dict[str, Any]:
        """Read a bounded runtime graph, using a runtime epoch as exploration id."""

        runtime_epoch = services.bridge.runtime_epoch if exploration_id == "current" else exploration_id
        if not runtime_epoch:
            raise ReframeworkMCPError(
                ErrorCode.INVALID_REQUEST,
                "No current runtime epoch is available",
            )
        services.runtime_graph.prune_expired(runtime_epoch)
        limit = 5000
        with services.database.connect() as connection:
            nodes = connection.execute(
                """
                SELECT * FROM runtime_nodes
                WHERE runtime_epoch = ?
                ORDER BY node_ref
                LIMIT ?
                """,
                (runtime_epoch, limit + 1),
            ).fetchall()
            edges = connection.execute(
                """
                SELECT * FROM runtime_edges
                WHERE runtime_epoch = ?
                ORDER BY edge_pk
                LIMIT ?
                """,
                (runtime_epoch, limit + 1),
            ).fetchall()
        return {
            "exploration_id": exploration_id,
            "runtime_epoch": runtime_epoch,
            "nodes": [
                {
                    "node_ref": row["node_ref"],
                    "scene_epoch": row["scene_epoch"],
                    "save_epoch": row["save_epoch"],
                    "type_name": row["type_name"],
                    "node_kind": row["node_kind"],
                    "value": json.loads(row["value_json"]),
                    "expires_at": row["expires_at"],
                }
                for row in nodes[:limit]
            ],
            "edges": [
                {
                    "source_ref": row["source_ref"],
                    "target_ref": row["target_ref"],
                    "edge_kind": row["edge_kind"],
                    "member_signature": row["member_signature"],
                    "metadata": json.loads(row["metadata_json"]),
                }
                for row in edges[:limit]
            ],
            "truncated": len(nodes) > limit or len(edges) > limit,
        }

    @mcp.resource(
        "reframework://access-plans/{plan_ref}",
        mime_type="application/json",
    )
    def access_plan_resource(plan_ref: str) -> dict[str, Any]:
        """Read a persisted AccessPlan."""

        plan = services.planner.load_plan(plan_ref)
        return plan.model_dump(mode="json")

    @mcp.resource(
        "reframework://access-plans/{plan_ref}/validation",
        mime_type="application/json",
    )
    def access_plan_validation_resource(plan_ref: str) -> dict[str, Any]:
        """Read the latest persisted validation for an AccessPlan."""

        with services.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM plan_validations
                WHERE plan_ref = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (plan_ref,),
            ).fetchone()
        if row is None:
            raise ReframeworkMCPError(
                ErrorCode.PLAN_NOT_VALIDATED,
                f"AccessPlan has no validation: {plan_ref}",
            )
        result = cast(dict[str, Any], json.loads(row["validation_json"]))
        result["validation_ref"] = row["validation_ref"]
        result["plan_ref"] = row["plan_ref"]
        result["runtime_epoch"] = row["runtime_epoch"]
        result["status"] = row["status"]
        result["created_at"] = row["created_at"]
        result["expires_at"] = row["expires_at"]
        return result

    @mcp.resource(
        "reframework://usage/{usage_ref}",
        mime_type="application/json",
    )
    def usage_resource(usage_ref: str) -> dict[str, Any]:
        """Read one indexed usage site or a bounded usage project."""

        normalized = usage_ref.removeprefix("usage:")
        with services.database.connect() as connection:
            if normalized.isdigit():
                row = connection.execute(
                    """
                    SELECT u.*, p.root_path, p.game_id, p.source_hash
                    FROM usage_sites u
                    JOIN usage_projects p ON p.project_id = u.project_id
                    WHERE u.usage_pk = ?
                    """,
                    (int(normalized),),
                ).fetchone()
                if row is None:
                    raise ReframeworkMCPError(
                        ErrorCode.INVALID_REQUEST,
                        f"Usage site not found: {usage_ref}",
                    )
                item = {str(key): row[index] for index, key in enumerate(row.keys())}
                item["metadata"] = json.loads(item.pop("metadata_json"))
                return {"kind": "usage_site", "usage": item}

            project = connection.execute(
                "SELECT * FROM usage_projects WHERE project_id = ?",
                (normalized,),
            ).fetchone()
            if project is None:
                raise ReframeworkMCPError(
                    ErrorCode.INVALID_REQUEST,
                    f"Usage project not found: {usage_ref}",
                )
            limit = 1000
            rows = connection.execute(
                """
                SELECT * FROM usage_sites
                WHERE project_id = ?
                ORDER BY file_path, line, usage_pk
                LIMIT ?
                """,
                (normalized, limit + 1),
            ).fetchall()
        return {
            "kind": "usage_project",
            "project": {str(key): project[index] for index, key in enumerate(project.keys())},
            "sites": [
                {
                    **{
                        str(key): row[index] for index, key in enumerate(row.keys()) if key != "metadata_json"
                    },
                    "metadata": json.loads(row["metadata_json"]),
                }
                for row in rows[:limit]
            ],
            "truncated": len(rows) > limit,
        }

    @mcp.resource(
        "reframework://hooks/{hook_ref}/events",
        mime_type="application/json",
    )
    async def hook_events_resource(hook_ref: str) -> dict[str, Any]:
        """Refresh and read captured Hook events from the dynamic graph."""

        if services.bridge.connected:
            try:
                result = await services.bridge.call(
                    "get_hook_events",
                    {"hook_ref": hook_ref},
                )
                services.runtime_graph.record_hook_payload(
                    result,
                    runtime_epoch=services.bridge.runtime_epoch,
                )
            except ReframeworkMCPError:
                pass
        return services.runtime_graph.hook_events(hook_ref)

    @mcp.resource(
        "reframework://probes/{probe_ref}/events",
        mime_type="application/json",
    )
    async def probe_events_resource(probe_ref: str) -> dict[str, Any]:
        """Refresh and read events emitted by an isolated Lua Probe."""

        if services.bridge.connected:
            try:
                result = await services.bridge.call(
                    "get_probe_status",
                    {"probe_ref": probe_ref},
                )
                services.runtime_graph.record_probe_payload(
                    result,
                    runtime_epoch=services.bridge.runtime_epoch,
                )
            except ReframeworkMCPError:
                pass
        return services.runtime_graph.probe_events(probe_ref)

    return mcp


async def _runtime_read(
    services: ApplicationServices,
    command: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not services.settings.policy.allow_runtime_read:
        raise ReframeworkMCPError(
            ErrorCode.POLICY_DENIED,
            "Runtime reads are disabled by policy",
        )
    result = await services.bridge.call(command, payload)
    if command == "list_singletons":
        services.runtime_graph.record_singletons(result)
    elif command == "inspect_object":
        services.runtime_graph.record_inspection(result)
    return success(result)


async def _mutation(
    services: ApplicationServices,
    command: str,
    payload: dict[str, Any],
    approval_ref: str | None,
    ctx: Context | None,
) -> dict[str, Any]:
    services.require_live_plan_validation(
        str(payload["plan_validation_ref"]),
        action=command,
        payload=payload,
    )
    approval = services.policy.require_mutation(
        command,
        payload,
        approval_ref,
        services.bridge.runtime_epoch,
    )
    if approval:
        elicited_ref = await _elicit_approval(
            ctx,
            approval,
            summary=payload,
        )
        if elicited_ref is None:
            return _approval_required(approval, payload)
        services.policy.require_mutation(
            command,
            payload,
            elicited_ref,
            services.bridge.runtime_epoch,
        )
    result = await services.bridge.call(command, payload)
    services.audit.record(
        command,
        payload,
        outcome="executed",
        runtime_epoch=services.bridge.runtime_epoch,
        snapshot_id=services.metadata.active_snapshot_id(),
    )
    return success(result)


async def _elicit_approval(
    ctx: Context | None,
    proposal: dict[str, Any],
    *,
    summary: dict[str, Any],
) -> str | None:
    if ctx is None:
        return None
    encoded_summary = json.dumps(
        summary,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    if len(encoded_summary) > 1200:
        encoded_summary = encoded_summary[:1197] + "..."
    message = (
        f"Approve REFramework-MCP action {proposal['action']}? "
        f"The approval is bound to the current runtime and exact arguments, "
        f"and expires at {proposal['expires_at']}. Request: {encoded_summary}"
    )
    try:
        result = await ctx.elicit(message, MutationApproval)
    except Exception:
        return None
    if result.action != "accept" or not result.data.approve:
        raise ReframeworkMCPError(
            ErrorCode.POLICY_DENIED,
            f"User declined or cancelled {proposal['action']}",
        )
    return str(proposal["approval_ref"])


def _approval_required(
    proposal: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    return success(
        {
            "state": "approval_required",
            **proposal,
            "summary": summary,
            "elicitation_unavailable": True,
        }
    )


async def _bridge_success(
    services: ApplicationServices,
    command: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return success(await services.bridge.call(command, payload))


async def _remove_hook(
    services: ApplicationServices,
    hook_ref: str,
) -> dict[str, Any]:
    result = await services.bridge.call("remove_hook", {"hook_ref": hook_ref})
    services.runtime_graph.record_hook_payload(
        result,
        runtime_epoch=services.bridge.runtime_epoch,
    )
    return success(result)


def _sha256_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()
