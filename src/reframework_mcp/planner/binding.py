"""Explicit argument binding and immutable live mutation targets."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from reframework_mcp.errors import ErrorCode, ReframeworkMCPError
from reframework_mcp.metadata import MetadataRepository
from reframework_mcp.models import (
    AccessNode,
    AccessOperation,
    AccessPlan,
    MemberRef,
    RootKind,
    RootSpec,
    ValidationStep,
)
from reframework_mcp.planner.contracts import INTEGER_BITS, node_error, scalar_error


def canonical_value(value: Any) -> str:
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
        normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


class MutationBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["invoke_method", "set_field"]
    member: MemberRef
    object_ref: str | None
    arguments_json: str

    @classmethod
    def requested(cls, action: str, payload: dict[str, Any]) -> MutationBinding:
        if action not in {"invoke_method", "set_field"}:
            raise ValueError("Unsupported mutation action")
        arguments = payload.get("arguments", []) if action == "invoke_method" else []
        if not isinstance(arguments, list):
            raise ValueError("Mutation arguments must be an array")
        return cls(
            action="invoke_method" if action == "invoke_method" else "set_field",
            member=MemberRef.model_validate(payload["member_ref"]),
            object_ref=payload.get("object_ref"),
            arguments_json=canonical_value(arguments),
        )


def validated_bindings(plan: AccessPlan, steps: list[ValidationStep]) -> list[MutationBinding]:
    by_id = {step.node_id: step for step in steps if step.status == "valid"}
    nodes = {node.node_id: node for node in plan.nodes}
    bindings = []
    for target_id in plan.targets:
        node = nodes[target_id]
        if (
            node.member is None
            or node.operation
            not in {
                AccessOperation.CALL_METHOD,
                AccessOperation.CALL_STATIC,
                AccessOperation.READ_FIELD,
            }
            or target_id not in by_id
        ):
            continue
        receiver = None
        if node.operation != AccessOperation.CALL_STATIC:
            if not node.inputs or node.inputs[0] not in by_id:
                continue
            step = by_id[node.inputs[0]]
            receiver = step.object_ref or (
                step.value_summary.get("object_ref") if isinstance(step.value_summary, dict) else None
            )
            if not receiver:
                continue
        if any(arg not in by_id or node_error(nodes[arg]) for arg in node.arguments):
            continue
        bindings.append(
            MutationBinding(
                action="set_field" if node.operation == AccessOperation.READ_FIELD else "invoke_method",
                member=node.member,
                object_ref=receiver,
                arguments_json=canonical_value([by_id[arg].value_summary for arg in node.arguments]),
            )
        )
    return bindings


def bind_plan(
    plan: AccessPlan, bindings: dict[str, Any], metadata: MetadataRepository | None = None
) -> AccessPlan:
    nodes = {node.node_id: node for node in plan.nodes}
    unknown = set(bindings) - nodes.keys()
    if unknown:
        raise ReframeworkMCPError(
            ErrorCode.PLAN_INVALID, "Unknown binding nodes", details={"nodes": sorted(unknown)}
        )
    result: list[AccessNode] = []
    for original in plan.nodes:
        node = original.model_copy(deep=True)
        if node.node_id in bindings:
            value = bindings[node.node_id]
            if node.operation not in {
                AccessOperation.BIND_CONSTANT,
                AccessOperation.CONSTRUCT_OBJECT,
                AccessOperation.CONSTRUCT_ARRAY,
                AccessOperation.CONSTRUCT_LIST,
                AccessOperation.CONSTRUCT_DICTIONARY,
            }:
                raise ReframeworkMCPError(
                    ErrorCode.PLAN_INVALID,
                    "Only constants or object/container inputs can be bound",
                    details={"node_id": node.node_id},
                )
            if node.operation != AccessOperation.BIND_CONSTANT:
                reference = value.get("object_ref") if isinstance(value, dict) else value
                if not isinstance(reference, str) or not reference.startswith("obj:"):
                    raise ReframeworkMCPError(ErrorCode.PLAN_INVALID, "Object binding requires an ObjectRef")
                node.operation = AccessOperation.RESOLVE_ROOT
                node.root = RootSpec(kind=RootKind.PROVIDED_OBJECT, object_ref=reference)
            else:
                scalar_type = node.output_type
                if (
                    metadata
                    and scalar_type
                    and scalar_type not in INTEGER_BITS
                    and scalar_type
                    not in {"System.String", "System.Boolean", "System.Single", "System.Double"}
                ):
                    scalar_type = metadata.enum_underlying_type(scalar_type, plan.snapshot_id) or scalar_type
                error = scalar_error(value, scalar_type, nullable=node.nullable)
                if error:
                    raise ReframeworkMCPError(
                        ErrorCode.PLAN_INVALID, error, details={"node_id": node.node_id}
                    )
                node.value = value
            for key in (
                "placeholder",
                "requires_user_value",
                "requires_user_population",
                "constructor_unresolved",
            ):
                node.options.pop(key, None)
            node.side_effect = "none"
        result.append(node)
    resolved_nodes = {node.node_id: node for node in result}
    for node in result:
        if "requires_argument_binding" in node.options:
            node.options["requires_argument_binding"] = [
                resolved_nodes[argument].output_type
                for argument in node.arguments
                if node_error(resolved_nodes[argument])
            ]
    assumptions = [
        text for text in plan.assumptions if not any(text.startswith(name + " ") for name in bindings)
    ]
    return AccessPlan.model_validate(
        {
            **plan.model_dump(mode="json"),
            "nodes": [node.model_dump(mode="json") for node in result],
            "assumptions": assumptions,
        }
    )
