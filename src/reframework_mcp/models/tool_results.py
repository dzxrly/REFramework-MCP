"""Common discriminated output envelope used by every MCP Tool."""

from __future__ import annotations

from typing import Any, Generic, Literal, NotRequired, TypeVar

from typing_extensions import TypedDict


class ToolErrorDetail(TypedDict):
    code: str
    message: str
    details: dict[str, Any]
    retryable: bool


TData = TypeVar("TData")


class ToolSuccess(TypedDict, Generic[TData]):
    ok: Literal[True]
    data: TData
    meta: NotRequired[dict[str, Any]]


class ToolFailure(TypedDict):
    ok: Literal[False]
    error: ToolErrorDetail


class RuntimeStatusData(TypedDict):
    server: dict[str, Any]
    bridge: dict[str, Any]
    metadata: dict[str, Any]
    generate_sdk: dict[str, Any]
    graphs: dict[str, Any]
    policy: dict[str, Any]


class GenerateSdkData(TypedDict):
    state: str
    job_ref: NotRequired[str | None]
    runtime_epoch: NotRequired[str | None]
    progress: NotRequired[dict[str, Any]]
    artifacts: NotRequired[dict[str, Any]]
    snapshot_id: NotRequired[str | None]
    reused_snapshot_id: NotRequired[str | None]
    indexed: NotRequired[bool]
    activated: NotRequired[bool]


class SearchTypesData(TypedDict):
    snapshot_id: str
    items: list[dict[str, Any]]
    next_offset: int | None


class DescribeTypeData(TypedDict):
    snapshot_id: str
    type: dict[str, Any]
    members: list[dict[str, Any]]
    dependencies: list[dict[str, Any]]
    coverage: dict[str, str]


class SearchMembersData(TypedDict):
    snapshot_id: str
    game_id: str | None
    query: dict[str, Any]
    items: list[dict[str, Any]]
    total_candidates: int
    next_offset: int | None
    coverage: dict[str, str]
    recommended_actions: list[dict[str, Any]]


class TypeDependenciesData(TypedDict):
    snapshot_id: str
    root: str
    direction: str
    nodes: list[str]
    edges: list[dict[str, Any]]
    truncated: bool


class SingletonListData(TypedDict):
    runtime_epoch: str
    items: list[dict[str, Any]]
    truncated: bool


class ObjectInspectionData(TypedDict):
    runtime_epoch: str
    root_ref: str
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    truncated: bool
    properties_included: NotRequired[bool]
    property_note: NotRequired[str]
    response_bytes: NotRequired[int]
    collection_adapters: NotRequired[dict[str, bool]]


class UsageExamplesData(TypedDict):
    items: list[dict[str, Any]]
    next_offset: int | None


class AccessPathsData(TypedDict):
    snapshot_id: str
    target_type: str
    target_member_pk: int | None
    roots_considered: list[dict[str, Any]]
    plans: list[dict[str, Any]]


class PlanValidationData(TypedDict):
    plan_ref: str
    validation_ref: str
    status: str
    symbolic_valid: bool
    live_valid: bool | None
    runtime_epoch: str | None
    scene_epoch: str | None
    save_epoch: str | None
    steps: list[dict[str, Any]]
    failed_node: str | None
    alternatives: list[dict[str, Any]]


class LuaDraftData(TypedDict):
    code: str
    mode: str
    plan_ref: str
    snapshot_id: str
    assumptions: list[str]
    unresolved_nodes: list[str]


class LuaValidationData(TypedDict):
    valid: bool
    validation_ref: str
    code_sha256: str
    mode: str
    snapshot_id: str | None
    plan_ref: str | None
    runtime_epoch: str | None
    errors: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    live_compile: dict[str, Any] | None
    expires_at: str


class InvokeMethodData(TypedDict):
    member_signature: str
    result: Any


class SetFieldData(TypedDict):
    object_ref: str
    member_signature: str
    old_value: Any
    new_value: Any


class ProbeRunData(TypedDict):
    probe_ref: str
    runtime_epoch: str
    mode: str
    state: str
    error: str | None
    frames: int
    max_frames: int
    instructions: int
    max_instructions: int
    event_count: int
    dropped: int
    output_bytes: int
    events: list[dict[str, Any]]
    event_resource: str


class HookInstallData(TypedDict):
    hook_ref: str
    runtime_epoch: str
    state: str
    member_signature: str
    phase: str
    ttl_seconds: float
    argument_layout: list[dict[str, Any]]
    event_resource: str


class HookRemoveData(TypedDict):
    hook_ref: str
    runtime_epoch: str
    state: str
    already_absent: bool
    calls: NotRequired[int]
    dropped: NotRequired[int]
    event_count: int
    events: NotRequired[list[dict[str, Any]]]


ToolResult = ToolSuccess[Any] | ToolFailure
RuntimeStatusResult = ToolSuccess[RuntimeStatusData] | ToolFailure
GenerateSdkResult = ToolSuccess[GenerateSdkData] | ToolFailure
SearchTypesResult = ToolSuccess[SearchTypesData] | ToolFailure
DescribeTypeResult = ToolSuccess[DescribeTypeData] | ToolFailure
SearchMembersResult = ToolSuccess[SearchMembersData] | ToolFailure
TypeDependenciesResult = ToolSuccess[TypeDependenciesData] | ToolFailure
SingletonListResult = ToolSuccess[SingletonListData] | ToolFailure
ObjectInspectionResult = ToolSuccess[ObjectInspectionData] | ToolFailure
UsageExamplesResult = ToolSuccess[UsageExamplesData] | ToolFailure
AccessPathsResult = ToolSuccess[AccessPathsData] | ToolFailure
PlanValidationResult = ToolSuccess[PlanValidationData] | ToolFailure
LuaDraftResult = ToolSuccess[LuaDraftData] | ToolFailure
LuaValidationResult = ToolSuccess[LuaValidationData] | ToolFailure
InvokeMethodResult = ToolSuccess[InvokeMethodData] | ToolFailure
SetFieldResult = ToolSuccess[SetFieldData] | ToolFailure
ProbeRunResult = ToolSuccess[ProbeRunData] | ToolFailure
HookInstallResult = ToolSuccess[HookInstallData] | ToolFailure
HookRemoveResult = ToolSuccess[HookRemoveData] | ToolFailure
