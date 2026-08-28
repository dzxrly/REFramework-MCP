"""Policy checks and short-lived approvals for mutating operations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reframework_mcp.config import PolicySettings
from reframework_mcp.errors import ErrorCode, ReframeworkMCPError


class ApprovalManager:
    def __init__(self, secret_path: Path) -> None:
        self.secret_path = secret_path
        self._secret = self._load_or_create_secret()

    def propose(
        self,
        action: str,
        arguments: dict[str, Any],
        *,
        runtime_epoch: str | None,
        lifetime_seconds: int = 120,
    ) -> dict[str, Any]:
        expires = datetime.now(UTC) + timedelta(seconds=lifetime_seconds)
        body = {
            "action": action,
            "arguments_hash": self.arguments_hash(arguments),
            "runtime_epoch": runtime_epoch,
            "expires_at": expires.isoformat(),
            "nonce": secrets.token_hex(8),
        }
        serialized = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(self._secret, serialized, hashlib.sha256).digest()
        token = (
            base64.urlsafe_b64encode(serialized).decode("ascii").rstrip("=")
            + "."
            + base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
        )
        return {
            "approval_ref": token,
            "action": action,
            "arguments_hash": body["arguments_hash"],
            "runtime_epoch": runtime_epoch,
            "expires_at": body["expires_at"],
        }

    def verify(
        self,
        token: str,
        action: str,
        arguments: dict[str, Any],
        *,
        runtime_epoch: str | None,
    ) -> None:
        try:
            encoded_body, encoded_signature = token.split(".", 1)
            body_bytes = self._decode(encoded_body)
            supplied_signature = self._decode(encoded_signature)
            expected_signature = hmac.new(
                self._secret,
                body_bytes,
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise ValueError("signature mismatch")
            body = json.loads(body_bytes)
            expires = datetime.fromisoformat(body["expires_at"])
        except Exception as error:
            raise ReframeworkMCPError(
                ErrorCode.APPROVAL_INVALID,
                "Invalid approval_ref",
            ) from error

        if expires < datetime.now(UTC):
            raise ReframeworkMCPError(
                ErrorCode.APPROVAL_INVALID,
                "approval_ref has expired",
            )
        if body["action"] != action:
            raise ReframeworkMCPError(
                ErrorCode.APPROVAL_INVALID,
                "approval_ref is bound to another action",
            )
        if body["arguments_hash"] != self.arguments_hash(arguments):
            raise ReframeworkMCPError(
                ErrorCode.APPROVAL_INVALID,
                "approval_ref arguments do not match",
            )
        if body.get("runtime_epoch") != runtime_epoch:
            raise ReframeworkMCPError(
                ErrorCode.APPROVAL_INVALID,
                "approval_ref belongs to another runtime epoch",
            )

    @staticmethod
    def arguments_hash(arguments: dict[str, Any]) -> str:
        encoded = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _load_or_create_secret(self) -> bytes:
        if self.secret_path.exists():
            return self.secret_path.read_bytes()
        self.secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_bytes(32)
        self.secret_path.write_bytes(secret)
        with suppress(OSError):
            self.secret_path.chmod(0o600)
        return secret

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class PolicyEngine:
    def __init__(self, settings: PolicySettings, approvals: ApprovalManager) -> None:
        self.settings = settings
        self.approvals = approvals

    def check_generate_sdk(
        self,
        arguments: dict[str, Any],
        approval_ref: str | None,
        runtime_epoch: str | None,
    ) -> dict[str, Any] | None:
        if not self.settings.allow_generate_sdk:
            raise ReframeworkMCPError(
                ErrorCode.POLICY_DENIED,
                "run_generate_sdk is disabled by policy",
            )
        policy = str(arguments.get("policy") or "")
        if policy == "force" and self.settings.prompt_force_generate_sdk:
            if not approval_ref:
                return self.approvals.propose(
                    "run_generate_sdk.force",
                    arguments,
                    runtime_epoch=runtime_epoch,
                )
            self.approvals.verify(
                approval_ref,
                "run_generate_sdk.force",
                arguments,
                runtime_epoch=runtime_epoch,
            )
        return None

    def require_mutation(
        self,
        action: str,
        arguments: dict[str, Any],
        approval_ref: str | None,
        runtime_epoch: str | None,
    ) -> dict[str, Any] | None:
        if not self.settings.require_mutation_approval:
            return None
        if not approval_ref:
            return self.approvals.propose(
                action,
                arguments,
                runtime_epoch=runtime_epoch,
            )
        self.approvals.verify(
            approval_ref,
            action,
            arguments,
            runtime_epoch=runtime_epoch,
        )
        return None
