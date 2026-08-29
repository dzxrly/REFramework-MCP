"""Application service layer used by the MCP tools and CLI."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, cast

from reframework_mcp import __version__
from reframework_mcp.audit import AuditLog
from reframework_mcp.bridge import BridgeClient, NamedPipeTransport
from reframework_mcp.config import Settings
from reframework_mcp.errors import (
    ErrorCode,
    ReframeworkMCPError,
    success,
)
from reframework_mcp.graphs import RuntimeGraphStore
from reframework_mcp.jobs import ExportJobCoordinator
from reframework_mcp.lua import LuaDraftService, LuaValidationService
from reframework_mcp.metadata import (
    Il2CppDumpImporter,
    MetadataRepository,
)
from reframework_mcp.models import AccessOperation, AccessPlan, MemberRef
from reframework_mcp.planner import AccessPlanner, AccessPlanValidator
from reframework_mcp.policy import ApprovalManager, PolicyEngine
from reframework_mcp.search import MemberSearchService
from reframework_mcp.storage import Database
from reframework_mcp.usage import UsageIndexer


class ApplicationServices:
    def __init__(self, settings: Settings, bridge: BridgeClient | None = None) -> None:
        self.settings = settings
        self.settings.ensure_directories()
        self.database = Database(settings.database_path)
        self.database.initialize()
        self.metadata = MetadataRepository(self.database)
        self.importer = Il2CppDumpImporter(self.database)
        self.usage = UsageIndexer(self.database)
        self.bridge = bridge or BridgeClient(
            NamedPipeTransport(
                settings.bridge.pipe_name,
                settings.bridge.connect_timeout_seconds,
            ),
            request_timeout=settings.bridge.request_timeout_seconds,
        )
        self.exports = ExportJobCoordinator(
            self.database,
            self.importer,
            self.bridge,
            settings.snapshots_dir,
        )
        self.runtime_graph = RuntimeGraphStore(self.database)
        self.planner = AccessPlanner(
            self.database,
            self.metadata,
            self.runtime_graph,
        )
        self.member_search = MemberSearchService(
            self.database,
            self.metadata,
            self.planner,
            self.runtime_graph,
        )
        self.plan_validator = AccessPlanValidator(
            self.database,
            self.metadata,
            self.bridge,
        )
        self.lua_draft = LuaDraftService(self.metadata)
        self.lua_validation = LuaValidationService(
            self.database,
            self.metadata,
            self.bridge,
        )
        self.approvals = ApprovalManager(settings.data_dir / "approval.secret")
        self.policy = PolicyEngine(settings.policy, self.approvals)
        self.audit = AuditLog(self.database)

    async def runtime_status(self, *, probe_bridge: bool = True) -> dict[str, Any]:
        bridge_status = await self.bridge.probe() if probe_bridge else self.bridge.status()
        if self.bridge.connected:
            await self.exports.reconcile_pending_once()
        return success(
            {
                "server": {
                    "version": __version__,
                    "transport": self.settings.server.transport,
                    "database_path": str(self.settings.database_path),
                    "data_dir": str(self.settings.data_dir),
                },
                "bridge": bridge_status,
                "metadata": self.metadata.snapshot_status(),
                "generate_sdk": self.exports.status(),
                "graphs": self.graph_status(),
                "policy": {
                    "allow_generate_sdk": self.settings.policy.allow_generate_sdk,
                    "runtime_read": self.settings.policy.allow_runtime_read,
                    "mutation_approval": self.settings.policy.require_mutation_approval,
                },
            }
        )

    def graph_status(self) -> dict[str, Any]:
        epoch = self.bridge.runtime_epoch
        self.runtime_graph.prune_expired(epoch)
        with self.database.connect() as connection:
            runtime_nodes = int(
                connection.execute(
                    "SELECT COUNT(*) FROM runtime_nodes WHERE (? IS NULL OR runtime_epoch=?)",
                    (epoch, epoch),
                ).fetchone()[0]
            )
            runtime_edges = int(
                connection.execute(
                    "SELECT COUNT(*) FROM runtime_edges WHERE (? IS NULL OR runtime_epoch=?)",
                    (epoch, epoch),
                ).fetchone()[0]
            )
            hook_events = int(
                connection.execute(
                    "SELECT COUNT(*) FROM hook_events WHERE (? IS NULL OR runtime_epoch=?)",
                    (epoch, epoch),
                ).fetchone()[0]
            )
            usage_edges = int(connection.execute("SELECT COUNT(*) FROM usage_sites").fetchone()[0])
        return {
            "runtime_epoch": epoch,
            "runtime_object_graph": {
                "nodes": runtime_nodes,
                "edges": runtime_edges,
            },
            "dynamic_call_graph": {"hook_events": hook_events},
            "mod_usage_graph": {"usage_sites": usage_edges},
            "static_member_graph": self.metadata.snapshot_status(),
        }

    async def run_generate_sdk(
        self,
        *,
        mode: str,
        policy: str = "reuse_if_fresh",
        activate_snapshot: bool = True,
        index_after_export: bool = True,
        approval_ref: str | None = None,
    ) -> dict[str, Any]:
        if mode not in {"json_only", "sdk_and_json"}:
            raise ReframeworkMCPError(
                ErrorCode.INVALID_REQUEST,
                "mode must be json_only or sdk_and_json",
            )
        if policy not in {"reuse_if_fresh", "force"}:
            raise ReframeworkMCPError(
                ErrorCode.INVALID_REQUEST,
                "policy must be reuse_if_fresh or force",
            )
        approval_arguments = {
            "mode": mode,
            "policy": policy,
            "activate_snapshot": activate_snapshot,
            "index_after_export": index_after_export,
        }
        approval = self.policy.check_generate_sdk(
            approval_arguments,
            approval_ref,
            self.bridge.runtime_epoch,
        )
        if approval:
            return success(
                {
                    "state": "approval_required",
                    **approval,
                    "summary": {
                        **approval_arguments,
                        "writes_local_artifacts": True,
                    },
                }
            )
        data = await self.bridge.call(
            "run_generate_sdk",
            {
                "mode": mode,
                "policy": policy,
                "activate_snapshot": activate_snapshot,
                "index_after_export": index_after_export,
                "snapshot_root": str(self.settings.snapshots_dir),
            },
        )
        data = self.exports.register(
            data,
            mode=mode,
            policy=policy,
            activate_snapshot=activate_snapshot,
            index_after_export=index_after_export,
        )
        self.audit.record(
            "run_generate_sdk",
            {"mode": mode, "policy": policy},
            outcome=str(data.get("state", "accepted")),
            runtime_epoch=self.bridge.runtime_epoch,
        )
        return success(data)

    def import_dump(
        self,
        path: Path,
        *,
        activate: bool = True,
        manifest_path: Path | None = None,
    ) -> dict[str, Any]:
        manifest: dict[str, Any] | None = None
        if manifest_path:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        result = self.importer.import_file(
            path,
            manifest=manifest,
            activate=activate,
        )
        return success(
            {
                "snapshot_id": result.snapshot_id,
                "artifact_sha256": result.artifact_sha256,
                "type_count": result.type_count,
                "member_count": result.member_count,
                "activated": result.activated,
                "coverage": result.coverage,
            }
        )

    def index_usage_project(
        self,
        path: Path,
        *,
        game_id: str | None = None,
    ) -> dict[str, Any]:
        return success(self.usage.index_project(path, game_id=game_id))

    async def with_errors(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        operation: Callable[[], Any | Awaitable[Any]],
    ) -> dict[str, Any]:
        try:
            result = operation()
            if asyncio.iscoroutine(result):
                result = await result
            return result if isinstance(result, dict) and "ok" in result else success(result)
        except ReframeworkMCPError as error:
            self.audit.record(
                tool_name,
                arguments,
                outcome="error",
                runtime_epoch=self.bridge.runtime_epoch,
                snapshot_id=self.metadata.active_snapshot_id(),
                details={"code": error.code.value},
            )
            return error.to_payload()
        except Exception as error:
            wrapped = ReframeworkMCPError(
                ErrorCode.INTERNAL_ERROR,
                f"{tool_name} failed",
                details={"error": str(error)},
            )
            self.audit.record(
                tool_name,
                arguments,
                outcome="error",
                runtime_epoch=self.bridge.runtime_epoch,
                snapshot_id=self.metadata.active_snapshot_id(),
                details={"code": wrapped.code.value},
            )
            return wrapped.to_payload()

    def load_plan(self, plan_ref: str | None, plan: dict[str, Any] | None) -> AccessPlan:
        if plan is not None:
            parsed = AccessPlan.model_validate(plan)
            self.planner.save_plan(parsed)
            return parsed
        if plan_ref:
            return self.planner.load_plan(plan_ref)
        raise ReframeworkMCPError(
            ErrorCode.INVALID_REQUEST,
            "plan_ref or plan is required",
        )

    def require_live_plan_validation(
        self,
        validation_ref: str,
        *,
        action: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM plan_validations WHERE validation_ref=?",
                (validation_ref,),
            ).fetchone()
        if row is None:
            raise ReframeworkMCPError(
                ErrorCode.PLAN_NOT_VALIDATED,
                "plan_validation_ref was not found",
            )
        result = cast(dict[str, Any], json.loads(row["validation_json"]))
        if row["status"] != "valid" or result.get("live_valid") is not True:
            raise ReframeworkMCPError(
                ErrorCode.PLAN_NOT_VALIDATED,
                "Mutation requires a successful live AccessPlan validation",
            )
        if not self.bridge.runtime_epoch or row["runtime_epoch"] != self.bridge.runtime_epoch:
            raise ReframeworkMCPError(
                ErrorCode.PLAN_NOT_VALIDATED,
                "Plan validation belongs to another runtime epoch",
                details={
                    "validated_epoch": row["runtime_epoch"],
                    "current_epoch": self.bridge.runtime_epoch,
                },
            )
        if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC):
            raise ReframeworkMCPError(
                ErrorCode.PLAN_NOT_VALIDATED,
                "Plan validation expired",
            )
        if action is not None:
            if payload is None:
                raise ReframeworkMCPError(
                    ErrorCode.INVALID_REQUEST,
                    "Mutation payload is required when binding a plan validation",
                )
            plan = self.planner.load_plan(str(row["plan_ref"]))
            self._require_plan_target_binding(plan, result, action, payload)
        return result

    @staticmethod
    def _require_plan_target_binding(
        plan: AccessPlan,
        validation: dict[str, Any],
        action: str,
        payload: dict[str, Any],
    ) -> None:
        expected_operations = {
            "invoke_method": {
                AccessOperation.CALL_METHOD,
                AccessOperation.CALL_STATIC,
            },
            "set_field": {AccessOperation.READ_FIELD},
        }.get(action)
        if expected_operations is None:
            raise ReframeworkMCPError(
                ErrorCode.INVALID_REQUEST,
                f"Unsupported plan-bound mutation: {action}",
            )

        try:
            requested_member = MemberRef.model_validate(payload["member_ref"])
        except Exception as error:
            raise ReframeworkMCPError(
                ErrorCode.INVALID_REQUEST,
                "Mutation member_ref is invalid",
                details={"error": str(error)},
            ) from error

        target_ids = set(plan.targets)
        candidates = [
            node
            for node in plan.nodes
            if node.node_id in target_ids
            and node.operation in expected_operations
            and node.member is not None
            and node.member.canonical_signature == requested_member.canonical_signature
        ]
        if len(candidates) != 1:
            ApplicationServices._plan_target_mismatch(
                "The validation is not bound to exactly one matching mutation target",
                action=action,
                requested_member=requested_member.canonical_signature,
                candidate_targets=[
                    node.node_id
                    for node in plan.nodes
                    if node.node_id in target_ids and node.operation in expected_operations
                ],
            )
        target = candidates[0]
        assert target.member is not None
        if (
            target.member.snapshot_id != requested_member.snapshot_id
            or target.member.kind != requested_member.kind
            or (
                target.member.member_id is not None
                and requested_member.member_id is not None
                and target.member.member_id != requested_member.member_id
            )
        ):
            ApplicationServices._plan_target_mismatch(
                "Mutation member_ref differs from the validated target",
                action=action,
                target=target.member.model_dump(mode="json"),
                requested=requested_member.model_dump(mode="json"),
            )

        steps = {
            str(step.get("node_id")): step
            for step in validation.get("steps", [])
            if isinstance(step, dict) and step.get("node_id")
        }
        actual_object_ref = payload.get("object_ref")
        if target.operation is AccessOperation.CALL_STATIC:
            if actual_object_ref is not None:
                ApplicationServices._plan_target_mismatch(
                    "A static validated target cannot be invoked with object_ref",
                    action=action,
                    actual_object_ref=actual_object_ref,
                )
        else:
            if not target.inputs:
                ApplicationServices._plan_target_mismatch(
                    "Validated instance target has no receiver node",
                    action=action,
                    target_node=target.node_id,
                )
            receiver_step = steps.get(target.inputs[0], {})
            summary = receiver_step.get("value_summary")
            expected_object_ref = receiver_step.get("object_ref") or (
                summary.get("object_ref") if isinstance(summary, dict) else None
            )
            if not expected_object_ref or actual_object_ref != expected_object_ref:
                ApplicationServices._plan_target_mismatch(
                    "Mutation object_ref differs from the live-validated receiver",
                    action=action,
                    expected_object_ref=expected_object_ref,
                    actual_object_ref=actual_object_ref,
                    receiver_node=target.inputs[0],
                )

        if action != "invoke_method":
            return
        nodes = {node.node_id: node for node in plan.nodes}
        expected_arguments: list[Any] = []
        for argument_node_id in target.arguments:
            argument_node = nodes[argument_node_id]
            if (
                bool(argument_node.options.get("placeholder"))
                or bool(argument_node.options.get("requires_user_value"))
                or bool(argument_node.options.get("constructor_unresolved"))
            ):
                ApplicationServices._plan_target_mismatch(
                    "Validated target still contains an unresolved argument",
                    action=action,
                    argument_node=argument_node_id,
                )
            argument_step = steps.get(argument_node_id)
            if argument_step is None or argument_step.get("status") != "valid":
                ApplicationServices._plan_target_mismatch(
                    "Mutation argument was not live-validated",
                    action=action,
                    argument_node=argument_node_id,
                )
            expected_arguments.append(argument_step.get("value_summary"))

        actual_arguments = payload.get("arguments", [])
        if not isinstance(actual_arguments, list):
            ApplicationServices._plan_target_mismatch(
                "Mutation arguments must be an array",
                action=action,
            )
        expected_canonical = ApplicationServices._canonical_bound_value(expected_arguments)
        actual_canonical = ApplicationServices._canonical_bound_value(actual_arguments)
        if expected_canonical != actual_canonical:
            ApplicationServices._plan_target_mismatch(
                "Mutation arguments differ from the live-validated plan",
                action=action,
                expected_arguments=expected_arguments,
                actual_arguments=actual_arguments,
            )

    @staticmethod
    def _canonical_bound_value(value: Any) -> str:
        def normalize(item: Any) -> Any:
            if isinstance(item, str) and item.startswith("obj:"):
                return {"object_ref": item}
            if isinstance(item, dict):
                if item.get("object_ref"):
                    return {"object_ref": str(item["object_ref"])}
                return {str(key): normalize(child) for key, child in item.items()}
            if isinstance(item, list):
                return [normalize(child) for child in item]
            return item

        return json.dumps(
            normalize(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _plan_target_mismatch(message: str, **details: Any) -> NoReturn:
        raise ReframeworkMCPError(
            ErrorCode.PLAN_TARGET_MISMATCH,
            message,
            details=details,
        )
