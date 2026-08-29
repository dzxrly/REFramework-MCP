from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from reframework_mcp.bridge import BridgeClient, InMemoryTransport
from reframework_mcp.config import PolicySettings, Settings
from reframework_mcp.errors import ErrorCode, ReframeworkMCPError
from reframework_mcp.models import AccessPlan
from reframework_mcp.policy import ApprovalManager, PolicyEngine
from reframework_mcp.server import _elicit_approval
from reframework_mcp.services import ApplicationServices


def _services(tmp_path: Path, runtime_epoch: str = "runtime:test") -> ApplicationServices:
    bridge = BridgeClient(InMemoryTransport(lambda request: {}))
    bridge.runtime_epoch = runtime_epoch
    return ApplicationServices(
        Settings(data_dir=tmp_path, database_path=tmp_path / "metadata.db"),
        bridge,
    )


def _insert_validation(
    services: ApplicationServices,
    *,
    validation_ref: str = "plan-validation:test",
    runtime_epoch: str = "runtime:test",
    status: str = "valid",
    live_valid: bool = True,
    expires_at: datetime | None = None,
) -> str:
    created_at = datetime.now(UTC).isoformat()
    with services.database.connect() as connection:
        connection.execute(
            """
            INSERT INTO access_plans(
                plan_ref, snapshot_id, game_id, goal, plan_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                "plan:test",
                "snapshot:test",
                "mhwilds",
                "test mutation gate",
                "{}",
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO plan_validations(
                validation_ref, plan_ref, runtime_epoch, status,
                validation_json, created_at, expires_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                validation_ref,
                "plan:test",
                runtime_epoch,
                status,
                json.dumps({"live_valid": live_valid}),
                created_at,
                expires_at.isoformat() if expires_at else None,
            ),
        )
        connection.commit()
    return validation_ref


def _insert_bound_validation(
    services: ApplicationServices,
    *,
    action: str,
) -> tuple[str, dict[str, object]]:
    root = {
        "node_id": "root",
        "operation": "resolve_root",
        "root": {
            "kind": "provided_object_ref",
            "object_ref": "obj:validated",
        },
        "output_type": "app.Test",
        "nullable": False,
    }
    if action == "invoke_method":
        member = {
            "snapshot_id": "snapshot:test",
            "kind": "tdb_method",
            "canonical_signature": ("app.Test::set_Value(System.Int32) -> System.Void"),
            "member_id": 1,
        }
        nodes = [
            root,
            {
                "node_id": "argument",
                "operation": "bind_constant",
                "value": 7,
                "output_type": "System.Int32",
                "nullable": False,
            },
            {
                "node_id": "target",
                "operation": "call_method",
                "inputs": ["root"],
                "member": member,
                "arguments": ["argument"],
                "output_type": "System.Void",
            },
        ]
        payload: dict[str, object] = {
            "object_ref": "obj:validated",
            "member_ref": member,
            "plan_validation_ref": "plan-validation:bound:method",
            "arguments": [7],
            "execution_phase": None,
            "expected_runtime_type": "",
        }
        validation_ref = "plan-validation:bound:method"
        steps = [
            {
                "node_id": "root",
                "status": "valid",
                "object_ref": "obj:validated",
                "value_summary": {"object_ref": "obj:validated"},
            },
            {
                "node_id": "argument",
                "status": "valid",
                "value_summary": 7,
            },
            {
                "node_id": "target",
                "status": "valid",
                "value_summary": {"validated_call": True},
            },
        ]
    else:
        member = {
            "snapshot_id": "snapshot:test",
            "kind": "tdb_field",
            "canonical_signature": "app.Test::Value: System.Int32",
            "member_id": 2,
        }
        nodes = [
            root,
            {
                "node_id": "target",
                "operation": "read_field",
                "inputs": ["root"],
                "member": member,
                "output_type": "System.Int32",
            },
        ]
        payload = {
            "object_ref": "obj:validated",
            "member_ref": member,
            "plan_validation_ref": "plan-validation:bound:field",
            "value": 8,
            "expected_old_value": 7,
            "execution_phase": None,
        }
        validation_ref = "plan-validation:bound:field"
        steps = [
            {
                "node_id": "root",
                "status": "valid",
                "object_ref": "obj:validated",
                "value_summary": {"object_ref": "obj:validated"},
            },
            {
                "node_id": "target",
                "status": "valid",
                "value_summary": 7,
            },
        ]
    plan = AccessPlan.model_validate(
        {
            "snapshot_id": "snapshot:test",
            "game_id": "mhwilds",
            "goal": f"validate {action}",
            "nodes": nodes,
            "targets": ["target"],
        }
    )
    created_at = datetime.now(UTC)
    with services.database.connect() as connection:
        connection.execute(
            """
            INSERT INTO access_plans(
                plan_ref, snapshot_id, game_id, goal, plan_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                plan.plan_ref,
                plan.snapshot_id,
                plan.game_id,
                plan.goal,
                plan.model_dump_json(),
                created_at.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO plan_validations(
                validation_ref, plan_ref, runtime_epoch, status,
                validation_json, created_at, expires_at
            ) VALUES(?, ?, ?, 'valid', ?, ?, ?)
            """,
            (
                validation_ref,
                plan.plan_ref,
                "runtime:test",
                json.dumps({"live_valid": True, "steps": steps}),
                created_at.isoformat(),
                (created_at + timedelta(minutes=1)).isoformat(),
            ),
        )
        connection.commit()
    return validation_ref, payload


def test_mutation_gate_accepts_current_live_validation(tmp_path: Path) -> None:
    services = _services(tmp_path)
    validation_ref = _insert_validation(
        services,
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )

    result = services.require_live_plan_validation(validation_ref)

    assert result["live_valid"] is True


@pytest.mark.parametrize(
    ("runtime_epoch", "status", "live_valid", "expires_delta"),
    [
        ("runtime:old", "valid", True, 60),
        ("runtime:test", "invalid", True, 60),
        ("runtime:test", "valid", False, 60),
        ("runtime:test", "valid", True, -1),
    ],
)
def test_mutation_gate_rejects_stale_or_unverified_validation(
    tmp_path: Path,
    runtime_epoch: str,
    status: str,
    live_valid: bool,
    expires_delta: int,
) -> None:
    services = _services(tmp_path)
    validation_ref = _insert_validation(
        services,
        runtime_epoch=runtime_epoch,
        status=status,
        live_valid=live_valid,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_delta),
    )

    with pytest.raises(ReframeworkMCPError) as raised:
        services.require_live_plan_validation(validation_ref)

    assert raised.value.code is ErrorCode.PLAN_NOT_VALIDATED


def test_mutation_gate_rejects_unknown_validation(tmp_path: Path) -> None:
    services = _services(tmp_path)

    with pytest.raises(ReframeworkMCPError) as raised:
        services.require_live_plan_validation("plan-validation:missing")

    assert raised.value.code is ErrorCode.PLAN_NOT_VALIDATED


def test_mutation_gate_binds_receiver_member_and_arguments(tmp_path: Path) -> None:
    services = _services(tmp_path)
    validation_ref, payload = _insert_bound_validation(
        services,
        action="invoke_method",
    )

    result = services.require_live_plan_validation(
        validation_ref,
        action="invoke_method",
        payload=payload,
    )

    assert result["live_valid"] is True


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("object_ref", "obj:other"),
        ("arguments", [9]),
    ],
)
def test_mutation_gate_rejects_changed_receiver_or_arguments(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    services = _services(tmp_path)
    validation_ref, payload = _insert_bound_validation(
        services,
        action="invoke_method",
    )
    changed = {**payload, field: replacement}

    with pytest.raises(ReframeworkMCPError) as raised:
        services.require_live_plan_validation(
            validation_ref,
            action="invoke_method",
            payload=changed,
        )

    assert raised.value.code is ErrorCode.PLAN_TARGET_MISMATCH


def test_field_validation_cannot_authorize_unrelated_method(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    validation_ref, _ = _insert_bound_validation(
        services,
        action="set_field",
    )
    unrelated_method = {
        "object_ref": "obj:validated",
        "member_ref": {
            "snapshot_id": "snapshot:test",
            "kind": "tdb_method",
            "canonical_signature": ("app.Test::set_Value(System.Int32) -> System.Void"),
            "member_id": 1,
        },
        "plan_validation_ref": validation_ref,
        "arguments": [7],
    }

    with pytest.raises(ReframeworkMCPError) as raised:
        services.require_live_plan_validation(
            validation_ref,
            action="invoke_method",
            payload=unrelated_method,
        )

    assert raised.value.code is ErrorCode.PLAN_TARGET_MISMATCH


def test_mutation_approval_is_bound_to_plan_validation(tmp_path: Path) -> None:
    manager = ApprovalManager(tmp_path / "approval.secret")
    original = {
        "object_ref": "object:test",
        "member_ref": {"signature": "app.Type::set_Value(System.Int32)"},
        "plan_validation_ref": "plan-validation:one",
        "arguments": [1],
    }
    proposal = manager.propose(
        "invoke_method",
        original,
        runtime_epoch="runtime:test",
    )

    manager.verify(
        proposal["approval_ref"],
        "invoke_method",
        original,
        runtime_epoch="runtime:test",
    )
    changed = {**original, "plan_validation_ref": "plan-validation:two"}
    with pytest.raises(ReframeworkMCPError) as raised:
        manager.verify(
            proposal["approval_ref"],
            "invoke_method",
            changed,
            runtime_epoch="runtime:test",
        )

    assert raised.value.code is ErrorCode.APPROVAL_INVALID


def test_force_generate_sdk_approval_binds_all_arguments(tmp_path: Path) -> None:
    manager = ApprovalManager(tmp_path / "approval.secret")
    policy = PolicyEngine(PolicySettings(), manager)
    original = {
        "mode": "json_only",
        "policy": "force",
        "activate_snapshot": True,
        "index_after_export": True,
    }
    proposal = policy.check_generate_sdk(original, None, "runtime:test")
    assert proposal is not None

    assert (
        policy.check_generate_sdk(
            original,
            proposal["approval_ref"],
            "runtime:test",
        )
        is None
    )
    changed = {**original, "mode": "sdk_and_json"}
    with pytest.raises(ReframeworkMCPError) as raised:
        policy.check_generate_sdk(
            changed,
            proposal["approval_ref"],
            "runtime:test",
        )

    assert raised.value.code is ErrorCode.APPROVAL_INVALID


@pytest.mark.asyncio
async def test_elicitation_accepts_exact_short_lived_proposal(
    tmp_path: Path,
) -> None:
    manager = ApprovalManager(tmp_path / "approval.secret")
    arguments = {"object_ref": "obj:test", "value": 7}
    proposal = manager.propose(
        "set_field",
        arguments,
        runtime_epoch="runtime:test",
    )

    class AcceptingContext:
        async def elicit(self, _message: str, schema):
            return SimpleNamespace(
                action="accept",
                data=schema(approve=True),
            )

    token = await _elicit_approval(
        cast(Any, AcceptingContext()),
        proposal,
        summary=arguments,
    )

    assert token == proposal["approval_ref"]
    manager.verify(
        str(token),
        "set_field",
        arguments,
        runtime_epoch="runtime:test",
    )
