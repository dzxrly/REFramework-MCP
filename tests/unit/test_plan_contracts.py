from __future__ import annotations

import pytest

from reframework_mcp.errors import ReframeworkMCPError
from reframework_mcp.lua import LuaDraftService
from reframework_mcp.metadata import MetadataRepository
from reframework_mcp.models import AccessPlan
from reframework_mcp.planner import AccessPlanValidator
from reframework_mcp.planner.binding import bind_plan, canonical_value
from reframework_mcp.planner.contracts import node_error, operation_support, scalar_error


def constant_plan(**overrides):
    node = {
        "node_id": "value",
        "operation": "bind_constant",
        "output_type": "System.Int32",
        "nullable": False,
        "value": 7,
        **overrides,
    }
    return AccessPlan.model_validate(
        {"snapshot_id": "snapshot:test", "goal": "check input", "nodes": [node], "targets": ["value"]}
    )


@pytest.mark.parametrize(
    ("type_name", "value"),
    [
        ("System.Int16", -32769),
        ("System.UInt16", 65536),
        ("System.UInt64", -1),
        ("System.Int64", 2**63),
        ("System.Int32", True),
        ("System.Boolean", 1),
        ("System.String", 7),
        ("System.Single", 1e39),
        ("System.Double", float("inf")),
        ("System.Double", float("nan")),
        ("System.Double", 10**400),
    ],
)
def test_scalar_binding_rejects_invalid_values(type_name, value):
    plan = constant_plan(output_type=type_name, options={"placeholder": True})
    with pytest.raises(ReframeworkMCPError, match=r"Expected|range|outside"):
        bind_plan(plan, {"value": value})


@pytest.mark.parametrize(
    ("type_name", "value"),
    [
        ("System.Int16", -32768),
        ("System.UInt16", 65535),
        ("System.UInt64", 2**64 - 1),
        ("System.Int64", -(2**63)),
        ("System.Boolean", False),
        ("System.String", ""),
    ],
)
def test_binding_is_immutable_and_clears_placeholder(type_name, value):
    plan = constant_plan(output_type=type_name, options={"placeholder": True, "requires_user_value": True})
    bound = bind_plan(plan, {"value": value})
    assert bound.plan_ref != plan.plan_ref
    assert bound.nodes[0].value == value
    assert node_error(bound.nodes[0]) is None
    assert plan.nodes[0].options["placeholder"] is True


@pytest.mark.parametrize(
    "operation", ["construct_object", "construct_array", "construct_list", "construct_dictionary"]
)
def test_bind_existing_object_or_container(operation):
    plan = constant_plan(
        operation=operation, output_type="app.Inventory", options={"requires_user_population": True}
    )
    bound = bind_plan(plan, {"value": {"object_ref": "obj:inventory"}})
    assert bound.nodes[0].root.object_ref == "obj:inventory"
    assert bound.nodes[0].operation.value == "resolve_root"
    with pytest.raises(ReframeworkMCPError, match="ObjectRef"):
        bind_plan(plan, {"value": [1, 2]})


def test_unknown_binding_and_unbound_draft_fail(metadata):
    plan = constant_plan(options={"placeholder": True})
    with pytest.raises(ReframeworkMCPError, match="Unknown"):
        bind_plan(plan, {"typo": 1})
    with pytest.raises(ReframeworkMCPError, match="Bind this node"):
        LuaDraftService(metadata).draft(plan)


@pytest.mark.parametrize("options", [{"min": 10, "max": 1}, {}, {"min": float("nan")}, {"min": 10**400}])
def test_invalid_range_contract(options):
    plan = constant_plan(operation="assert_range", inputs=[])
    node = plan.nodes[0].model_copy(update={"inputs": ["upstream"], "options": options})
    assert node_error(node)


def test_scalar_canonicalization_keeps_boolean_distinct():
    assert canonical_value([True]) != canonical_value([1])
    assert canonical_value(["obj:item"]) == canonical_value([{"object_ref": "obj:item", "type": "app.Item"}])
    assert scalar_error(None, "System.Int32", nullable=False)


@pytest.mark.asyncio
async def test_incomplete_live_steps_cannot_authorize_mutation(imported_database):
    database, _snapshot_id = imported_database

    class Bridge:
        connected = True

        async def call(self, command, payload):
            assert command == "validate_access_plan"
            assert payload["getter_allowlist"] == ["app.Test::get_Value() -> System.Int32"]
            return {"status": "valid", "steps": [], "runtime_epoch": "runtime:test"}

    result = await AccessPlanValidator(database, MetadataRepository(database), Bridge()).validate(
        constant_plan(), allow_getters=True, getter_allowlist=["app.Test::get_Value() -> System.Int32"]
    )
    assert result.live_valid is False
    assert result.failed_node == "value"
    assert result.mutation_bindings == []
    assert "incomplete" in result.steps[-1].message


@pytest.mark.asyncio
async def test_forged_member_signature_rejected(imported_database):
    database, snapshot_id = imported_database
    metadata = MetadataRepository(database)
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM members WHERE kind='tdb_field' LIMIT 1").fetchone()
    plan = AccessPlan.model_validate(
        {
            "snapshot_id": snapshot_id,
            "goal": "reject forged member",
            "nodes": [
                {
                    "node_id": "root",
                    "operation": "resolve_root",
                    "root": {"kind": "provided_object_ref", "object_ref": "obj:test"},
                },
                {
                    "node_id": "field",
                    "operation": "read_field",
                    "inputs": ["root"],
                    "member": {
                        "snapshot_id": snapshot_id,
                        "kind": "tdb_field",
                        "member_id": row["member_pk"],
                        "canonical_signature": "app.Forged::Value: System.Int32",
                    },
                },
            ],
            "targets": ["field"],
        }
    )

    class Bridge:
        connected = True

        async def call(self, command, payload):
            raise AssertionError("Invalid MemberRef reached the live bridge")

    result = await AccessPlanValidator(database, metadata, Bridge()).validate(plan)
    assert result.symbolic_valid is False
    assert result.failed_node == "field"


def test_enum_binding_uses_parent_and_storage_field(imported_database):
    from reframework_mcp.planner import AccessPlanner

    database, snapshot_id = imported_database
    with database.connect() as connection:
        connection.execute("UPDATE types SET parent_name='System.Enum' WHERE full_name='app.ItemId'")
        member = connection.execute(
            "SELECT member_pk FROM members WHERE kind='tdb_field' LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            "UPDATE members SET declaring_type='app.ItemId', name='value__', value_type='System.Byte' WHERE member_pk=?",
            (member,),
        )
        connection.commit()
    metadata = MetadataRepository(database)
    assert AccessPlanner(database, metadata)._type_is_enum(snapshot_id, "app.ItemId")
    assert metadata.enum_underlying_type("app.ItemId", snapshot_id) == "System.Byte"
    plan = constant_plan(output_type="app.ItemId", options={"placeholder": True})
    assert bind_plan(plan, {"value": 255}, metadata).nodes[0].value == 255
    with pytest.raises(ReframeworkMCPError, match=r"System\.Byte"):
        bind_plan(plan, {"value": 256}, metadata)


def test_clr_generic_names_are_recognized_as_container_inputs():
    from reframework_mcp.planner import AccessPlanner

    assert (
        AccessPlanner._container_operation("System.Collections.Generic.List`1<System.Int32>").value
        == "construct_list"
    )
    assert (
        AccessPlanner._container_operation(
            "System.Collections.Generic.Dictionary`2<System.Int32,System.Int32>"
        ).value
        == "construct_dictionary"
    )


def test_lua_draft_rejects_integer_precision_loss(metadata):
    with pytest.raises(ReframeworkMCPError, match="signed 64-bit"):
        LuaDraftService(metadata).draft(constant_plan(output_type="System.UInt64", value=2**64 - 1))


def test_operation_support_does_not_claim_live_allocation():
    supported = operation_support()
    assert supported["iterate"] == {"symbolic": True, "live": True, "draft": True}
    assert supported["construct_list"]["live"] is False
    assert supported["construct_object"]["draft"] is False
