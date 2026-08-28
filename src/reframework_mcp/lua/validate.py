"""Static and optional live compilation checks for Lua probes."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Protocol, cast

from reframework_mcp.metadata import MetadataRepository
from reframework_mcp.storage import Database
from reframework_mcp.usage.extractor import LuaLexer


class LuaCompilerBridge(Protocol):
    @property
    def connected(self) -> bool: ...

    async def call(self, command: str, payload: dict[str, Any]) -> dict[str, Any]: ...


_TYPE_LITERAL = re.compile(
    r"sdk\.(?:find_type_definition|get_managed_singleton|get_native_singleton)"
    r"\(\s*['\"]([^'\"]+)['\"]\s*\)"
)
_METHOD_LITERAL = re.compile(r"(?:get_method|call)\(\s*['\"]([^'\"]+)['\"]")
_FIELD_LITERAL = re.compile(r"(?:get_field|set_field)\(\s*['\"]([^'\"]+)['\"]")


class LuaValidationService:
    FORBIDDEN_PATTERNS: ClassVar[dict[str, str]] = {
        r"\bos\.": "os library is unavailable",
        r"\bio\.": "io library is unavailable",
        r"\bpackage\.": "package library is unavailable",
        r"\bdebug\.": "debug library is unavailable",
        r"\bffi\.": "ffi is unavailable in probes",
        r"\bsdk\.write_": "raw memory writes are unavailable",
        r"\bwhile\s+true\s+do\b": "unbounded loops are unavailable",
    }

    def __init__(
        self,
        database: Database,
        metadata: MetadataRepository,
        bridge: LuaCompilerBridge,
    ) -> None:
        self.database = database
        self.metadata = metadata
        self.bridge = bridge
        self.lexer = LuaLexer()

    async def validate(
        self,
        code: str,
        *,
        mode: str,
        snapshot_id: str | None,
        plan_ref: str | None,
        runtime_epoch: str | None,
        live_compile: bool = True,
    ) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        self._balanced(code, errors)
        for pattern, message in self.FORBIDDEN_PATTERNS.items():
            if re.search(pattern, code):
                errors.append({"kind": "risk", "message": message, "pattern": pattern})
        if mode != "windowed_hook_test" and re.search(r"\bsdk\.hook\s*\(", code):
            errors.append(
                {
                    "kind": "lifecycle",
                    "message": "sdk.hook requires windowed_hook_test mode",
                }
            )

        selected = snapshot_id
        if selected:
            self._validate_symbols(code, selected, errors, warnings)
        live_result: dict[str, Any] | None = None
        if not errors and live_compile and self.bridge.connected:
            live_result = await self.bridge.call(
                "compile_lua_probe",
                {"code": code, "mode": mode},
            )
            if not live_result.get("valid", False):
                errors.append(
                    {
                        "kind": "live_compile",
                        "message": live_result.get("message", "Lua compilation failed"),
                    }
                )

        code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
        now = datetime.now(UTC)
        expires = now + timedelta(minutes=10)
        material = json.dumps(
            {
                "code_sha256": code_hash,
                "plan_ref": plan_ref,
                "snapshot_id": selected,
                "runtime_epoch": runtime_epoch,
                "mode": mode,
                "created_at": now.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        validation_ref = f"lua_validation:{hashlib.sha256(material.encode()).hexdigest()}"
        result = {
            "valid": not errors,
            "validation_ref": validation_ref,
            "code_sha256": code_hash,
            "mode": mode,
            "snapshot_id": selected,
            "plan_ref": plan_ref,
            "runtime_epoch": runtime_epoch,
            "errors": errors,
            "warnings": warnings,
            "live_compile": live_result,
            "expires_at": expires.isoformat(),
        }
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO lua_validations(
                    validation_ref, code_sha256, plan_ref, snapshot_id,
                    runtime_epoch, mode, validation_json, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validation_ref,
                    code_hash,
                    plan_ref,
                    selected,
                    runtime_epoch,
                    mode,
                    json.dumps(result, ensure_ascii=False),
                    now.isoformat(),
                    expires.isoformat(),
                ),
            )
            connection.commit()
        return result

    def require_valid(
        self,
        validation_ref: str,
        code: str,
        *,
        runtime_epoch: str | None,
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM lua_validations WHERE validation_ref = ?",
                (validation_ref,),
            ).fetchone()
        if row is None:
            raise ValueError("validation_ref not found")
        result = cast(dict[str, Any], json.loads(row["validation_json"]))
        if not result.get("valid"):
            raise ValueError("Lua validation failed")
        if row["code_sha256"] != hashlib.sha256(code.encode("utf-8")).hexdigest():
            raise ValueError("Lua code changed after validation")
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC):
            raise ValueError("Lua validation expired")
        bound_epoch = row["runtime_epoch"]
        if bound_epoch is not None and bound_epoch != runtime_epoch:
            raise ValueError("Lua validation belongs to another runtime epoch")
        return result

    def _balanced(self, code: str, errors: list[dict[str, Any]]) -> None:
        tokens = list(self.lexer.tokenize(code))
        stack: list[tuple[str, int, int]] = []
        pairs = {")": "(", "]": "[", "}": "{"}
        for token in tokens:
            if token.value in {"(", "[", "{"}:
                stack.append((token.value, token.line, token.column))
            elif token.value in pairs:
                if not stack or stack[-1][0] != pairs[token.value]:
                    errors.append(
                        {
                            "kind": "syntax",
                            "message": f"unmatched {token.value}",
                            "line": token.line,
                            "column": token.column,
                        }
                    )
                    return
                stack.pop()
        if stack:
            value, line, column = stack[-1]
            errors.append(
                {
                    "kind": "syntax",
                    "message": f"unclosed {value}",
                    "line": line,
                    "column": column,
                }
            )

    def _validate_symbols(
        self,
        code: str,
        snapshot_id: str,
        errors: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
    ) -> None:
        for type_name in _TYPE_LITERAL.findall(code):
            result = self.metadata.search_types(
                type_name,
                snapshot_id=snapshot_id,
                limit=10,
            )
            if not any(item["full_name"] == type_name for item in result["items"]):
                errors.append({"kind": "symbol", "message": f"type not found: {type_name}"})
        for selector in _METHOD_LITERAL.findall(code):
            method_name = selector.split("(", 1)[0]
            result = self.metadata.search_members(
                method_name,
                snapshot_id=snapshot_id,
                member_kinds=["tdb_method", "reflection_method"],
                limit=20,
            )
            if not result["items"]:
                warnings.append(
                    {
                        "kind": "symbol",
                        "message": f"method selector not resolved globally: {selector}",
                    }
                )
        for field_name in _FIELD_LITERAL.findall(code):
            result = self.metadata.search_members(
                field_name,
                snapshot_id=snapshot_id,
                member_kinds=[
                    "tdb_field",
                    "tdb_property",
                    "reflection_property",
                    "rsz_field",
                ],
                limit=20,
            )
            if not result["items"]:
                warnings.append(
                    {
                        "kind": "symbol",
                        "message": f"field not resolved globally: {field_name}",
                    }
                )
