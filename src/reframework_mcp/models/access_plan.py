"""Typed data-flow representation for object acquisition and calls."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .refs import MemberRef, RootSpec


class AccessOperation(StrEnum):
    RESOLVE_ROOT = "resolve_root"
    RESOLVE_TYPE = "resolve_type"
    READ_FIELD = "read_field"
    READ_PROPERTY = "read_property"
    CALL_METHOD = "call_method"
    CALL_STATIC = "call_static"
    CAST = "cast"
    INDEX = "index"
    ITERATE = "iterate"
    FILTER = "filter"
    CONSTRUCT_OBJECT = "construct_object"
    CONSTRUCT_ARRAY = "construct_array"
    CONSTRUCT_LIST = "construct_list"
    CONSTRUCT_DICTIONARY = "construct_dictionary"
    BIND_CONSTANT = "bind_constant"
    ASSERT_NON_NULL = "assert_non_null"
    ASSERT_TYPE = "assert_type"
    ASSERT_RANGE = "assert_range"
    EMIT = "emit"


class PlanEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    reference: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    verified: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class AccessNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    operation: AccessOperation
    inputs: list[str] = Field(default_factory=list)
    root: RootSpec | None = None
    member: MemberRef | None = None
    arguments: list[str] = Field(default_factory=list)
    value: Any = None
    output_type: str | None = None
    nullable: bool = True
    side_effect: str = "unknown"
    execution_phase: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    evidence: list[PlanEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_operation_fields(self) -> AccessNode:
        if self.operation is AccessOperation.RESOLVE_ROOT and self.root is None:
            raise ValueError("resolve_root requires root")
        if (
            self.operation
            in {
                AccessOperation.READ_FIELD,
                AccessOperation.READ_PROPERTY,
                AccessOperation.CALL_METHOD,
                AccessOperation.CALL_STATIC,
            }
            and self.member is None
        ):
            raise ValueError(f"{self.operation.value} requires member")
        return self


class AccessPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_schema: str = "1.0"
    game_id: str | None = None
    snapshot_id: str
    goal: str = Field(min_length=1)
    nodes: list[AccessNode]
    targets: list[str]
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_graph(self) -> AccessPlan:
        seen: set[str] = set()
        for node in self.nodes:
            if node.node_id in seen:
                raise ValueError(f"duplicate node_id: {node.node_id}")
            missing = [item for item in [*node.inputs, *node.arguments] if item not in seen]
            if missing:
                raise ValueError(
                    f"node {node.node_id} references nodes that are not defined earlier: {missing}"
                )
            seen.add(node.node_id)
        missing_targets = [target for target in self.targets if target not in seen]
        if missing_targets:
            raise ValueError(f"unknown targets: {missing_targets}")
        if not self.nodes:
            raise ValueError("plan must contain at least one node")
        return self

    @property
    def content_hash(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def plan_ref(self) -> str:
        return f"plan:{self.plan_schema}:{self.content_hash}"


class ValidationStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    status: str
    expected_type: str | None = None
    actual_type: str | None = None
    value_summary: Any = None
    object_ref: str | None = None
    error_code: str | None = None
    message: str | None = None


class PlanValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_ref: str
    validation_ref: str
    status: str
    symbolic_valid: bool
    live_valid: bool | None = None
    runtime_epoch: str | None = None
    scene_epoch: str | None = None
    save_epoch: str | None = None
    steps: list[ValidationStep] = Field(default_factory=list)
    failed_node: str | None = None
    alternatives: list[dict[str, Any]] = Field(default_factory=list)
