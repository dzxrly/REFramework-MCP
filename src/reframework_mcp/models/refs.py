"""Stable references used across snapshots and runtime epochs."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MemberKind(StrEnum):
    TDB_METHOD = "tdb_method"
    TDB_FIELD = "tdb_field"
    TDB_PROPERTY = "tdb_property"
    SYNTHETIC_PROPERTY = "synthetic_property"
    REFLECTION_METHOD = "reflection_method"
    REFLECTION_PROPERTY = "reflection_property"
    RSZ_FIELD = "rsz_field"


class RootKind(StrEnum):
    MANAGED_SINGLETON = "managed_singleton"
    NATIVE_SINGLETON = "native_singleton"
    STATIC_TYPE = "static_type"
    HOOK_THIS = "hook_this"
    HOOK_ARGUMENT = "hook_argument"
    HOOK_RETURN = "hook_return"
    PROVIDED_OBJECT = "provided_object_ref"
    SCENE_OBJECT_QUERY = "scene_object_query"
    COMPONENT_QUERY = "component_query"


class TypeRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    full_name: str = Field(min_length=1)
    type_id: int | None = None


class MemberRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    kind: MemberKind
    canonical_signature: str = Field(min_length=1)
    member_id: int | None = None


class ObjectRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    object_ref: str = Field(pattern=r"^obj:")
    runtime_epoch: str
    type_ref: TypeRef | None = None
    scene_epoch: str | None = None
    save_epoch: str | None = None
    expires_at: str | None = None


class RootSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: RootKind
    type_name: str | None = None
    object_ref: str | None = None
    hook_ref: str | None = None
    argument_index: int | None = Field(default=None, ge=0)
    query: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> RootSpec:
        if (
            self.kind
            in {
                RootKind.MANAGED_SINGLETON,
                RootKind.NATIVE_SINGLETON,
                RootKind.STATIC_TYPE,
            }
            and not self.type_name
        ):
            raise ValueError(f"{self.kind.value} requires type_name")
        if self.kind is RootKind.PROVIDED_OBJECT and not self.object_ref:
            raise ValueError("provided_object_ref requires object_ref")
        if (
            self.kind
            in {
                RootKind.HOOK_THIS,
                RootKind.HOOK_ARGUMENT,
                RootKind.HOOK_RETURN,
            }
            and not self.hook_ref
        ):
            raise ValueError(f"{self.kind.value} requires hook_ref")
        if self.kind is RootKind.HOOK_ARGUMENT and self.argument_index is None:
            raise ValueError("hook_argument requires argument_index")
        return self
