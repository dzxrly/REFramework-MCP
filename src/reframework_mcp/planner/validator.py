"""Symbolic and live validation for AccessPlan objects."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from reframework_mcp.errors import ErrorCode, ReframeworkMCPError
from reframework_mcp.metadata import MetadataRepository
from reframework_mcp.models import AccessPlan, PlanValidation, ValidationStep
from reframework_mcp.storage import Database


class LivePlanValidator(Protocol):
    async def call(self, command: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    @property
    def connected(self) -> bool: ...


class AccessPlanValidator:
    def __init__(
        self,
        database: Database,
        metadata: MetadataRepository,
        bridge: LivePlanValidator,
    ) -> None:
        self.database = database
        self.metadata = metadata
        self.bridge = bridge

    async def validate(
        self,
        plan: AccessPlan,
        *,
        live: bool = True,
        allow_getters: bool = False,
    ) -> PlanValidation:
        self._ensure_plan(plan)
        symbolic_steps = self._symbolic(plan)
        symbolic_valid = all(step.status == "valid" for step in symbolic_steps)
        if not symbolic_valid:
            validation = self._make_validation(
                plan,
                symbolic_steps,
                status="failed",
                symbolic_valid=False,
                live_valid=None,
            )
            self._save(validation)
            return validation

        if live and self.bridge.connected:
            response = await self.bridge.call(
                "validate_access_plan",
                {
                    "plan": plan.model_dump(mode="json"),
                    "allow_getters": allow_getters,
                },
            )
            live_steps = [ValidationStep.model_validate(step) for step in response.get("steps", [])]
            status = str(response.get("status", "failed"))
            validation = self._make_validation(
                plan,
                live_steps or symbolic_steps,
                status=status,
                symbolic_valid=True,
                live_valid=status == "valid",
                runtime_epoch=response.get("runtime_epoch"),
                scene_epoch=response.get("scene_epoch"),
                save_epoch=response.get("save_epoch"),
                alternatives=response.get("alternatives", []),
            )
        else:
            validation = self._make_validation(
                plan,
                symbolic_steps,
                status="symbolic_valid",
                symbolic_valid=True,
                live_valid=None,
            )
        self._save(validation)
        return validation

    def _ensure_plan(self, plan: AccessPlan) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO access_plans(
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

    def _symbolic(self, plan: AccessPlan) -> list[ValidationStep]:
        if plan.snapshot_id != self.metadata.require_snapshot(plan.snapshot_id):
            raise ReframeworkMCPError(
                ErrorCode.SNAPSHOT_NOT_FOUND,
                f"Snapshot not found: {plan.snapshot_id}",
            )
        steps: list[ValidationStep] = []
        for node in plan.nodes:
            if node.member is not None:
                if node.member.snapshot_id != plan.snapshot_id:
                    steps.append(
                        ValidationStep(
                            node_id=node.node_id,
                            status="invalid",
                            error_code=ErrorCode.PLAN_INVALID.value,
                            message="MemberRef belongs to another snapshot",
                        )
                    )
                    continue
                if node.member.member_id is None:
                    steps.append(
                        ValidationStep(
                            node_id=node.node_id,
                            status="invalid",
                            error_code=ErrorCode.MEMBER_NOT_FOUND.value,
                            message="MemberRef has no indexed member_id",
                        )
                    )
                    continue
                member = self.metadata.member_by_pk(node.member.member_id, plan.snapshot_id)
                if member is None:
                    steps.append(
                        ValidationStep(
                            node_id=node.node_id,
                            status="invalid",
                            error_code=ErrorCode.MEMBER_NOT_FOUND.value,
                            message=node.member.canonical_signature,
                        )
                    )
                    continue
            steps.append(
                ValidationStep(
                    node_id=node.node_id,
                    status="valid",
                    expected_type=node.output_type,
                )
            )
        return steps

    @staticmethod
    def _make_validation(
        plan: AccessPlan,
        steps: list[ValidationStep],
        *,
        status: str,
        symbolic_valid: bool,
        live_valid: bool | None,
        runtime_epoch: str | None = None,
        scene_epoch: str | None = None,
        save_epoch: str | None = None,
        alternatives: list[dict[str, Any]] | None = None,
    ) -> PlanValidation:
        now = datetime.now(UTC)
        material = json.dumps(
            {
                "plan_ref": plan.plan_ref,
                "runtime_epoch": runtime_epoch,
                "steps": [step.model_dump(mode="json") for step in steps],
                "created_at": now.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        failed = next((step.node_id for step in steps if step.status != "valid"), None)
        return PlanValidation(
            plan_ref=plan.plan_ref,
            validation_ref=f"validated_plan:{runtime_epoch or 'offline'}:{digest}",
            status=status,
            symbolic_valid=symbolic_valid,
            live_valid=live_valid,
            runtime_epoch=runtime_epoch,
            scene_epoch=scene_epoch,
            save_epoch=save_epoch,
            steps=steps,
            failed_node=failed,
            alternatives=alternatives or [],
        )

    def _save(self, validation: PlanValidation) -> None:
        now = datetime.now(UTC)
        expires = now + timedelta(minutes=5) if validation.runtime_epoch else None
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO plan_validations(
                    validation_ref, plan_ref, runtime_epoch, status,
                    validation_json, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validation.validation_ref,
                    validation.plan_ref,
                    validation.runtime_epoch,
                    validation.status,
                    validation.model_dump_json(),
                    now.isoformat(),
                    expires.isoformat() if expires else None,
                ),
            )
            connection.commit()
