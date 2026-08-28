from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reframework_mcp.bridge import BridgeClient, InMemoryTransport
from reframework_mcp.config import PolicySettings, Settings
from reframework_mcp.errors import ErrorCode, ReframeworkMCPError
from reframework_mcp.policy import ApprovalManager, PolicyEngine
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
