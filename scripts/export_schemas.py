"""Export the frozen C2 1.0.0 Tool, protocol and model JSON Schemas."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from reframework_mcp import __version__
from reframework_mcp.bridge import BridgeClient, InMemoryTransport
from reframework_mcp.bridge.protocol import BridgeRequest, BridgeResponse
from reframework_mcp.config import Settings
from reframework_mcp.models import AccessPlan, MemberRef, ObjectRef, RootSpec, TypeRef
from reframework_mcp.server import create_server
from reframework_mcp.services import ApplicationServices


def _offline_handler(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": "1.0",
        "request_id": request["request_id"],
        "runtime_epoch": "schema-export",
        "ok": True,
        "data": {},
    }


def schema_bundle(data_dir: Path) -> dict[str, Any]:
    services = ApplicationServices(
        Settings(
            data_dir=data_dir,
            database_path=data_dir / "schema.sqlite",
        ),
        BridgeClient(InMemoryTransport(_offline_handler)),
    )
    server = create_server(services)
    tools = {
        name: {
            "description": tool.description,
            "input_schema": tool.parameters,
            "output_schema": tool.output_schema,
        }
        for name, tool in sorted(server._tool_manager._tools.items())
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "contract": "REFramework-MCP C2",
        "version": __version__,
        "bridge_protocol": "1.0",
        "access_plan_schema": "1.0",
        "tools": tools,
        "models": {
            "AccessPlan": AccessPlan.model_json_schema(),
            "TypeRef": TypeRef.model_json_schema(),
            "MemberRef": MemberRef.model_json_schema(),
            "ObjectRef": ObjectRef.model_json_schema(),
            "RootSpec": RootSpec.model_json_schema(),
            "BridgeRequest": BridgeRequest.model_json_schema(),
            "BridgeResponse": BridgeResponse.model_json_schema(),
            "SnapshotManifest": {
                "type": "object",
                "additionalProperties": True,
                "required": [
                    "snapshot_id",
                    "snapshot_schema",
                    "game_id",
                    "tdb_fingerprint",
                ],
                "properties": {
                    "snapshot_id": {"type": "string"},
                    "snapshot_schema": {"const": "1.0"},
                    "game_id": {"type": "string"},
                    "game_version": {"type": ["string", "null"]},
                    "tdb_version": {"type": ["integer", "null"]},
                    "tdb_fingerprint": {"type": "string"},
                    "reframework_version": {"type": ["string", "null"]},
                    "runtime_epoch": {"type": ["string", "null"]},
                    "provider": {"type": "string"},
                    "provider_version": {"type": "string"},
                    "mode": {"enum": ["json_only", "sdk_and_json"]},
                    "artifact_sha256": {"type": "string"},
                    "coverage": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                },
            },
        },
    }


def canonical_bytes(bundle: dict[str, Any]) -> bytes:
    return json.dumps(
        bundle,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def schema_sha256(bundle: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(bundle)).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("dist") / "tool-contracts-v1.json",
    )
    parser.add_argument("--check-digest", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="refmcp-schema-") as temporary:
        bundle = schema_bundle(Path(temporary))
    digest = schema_sha256(bundle)
    if args.check_digest:
        expected = args.check_digest.read_text(encoding="utf-8").strip()
        if expected != digest:
            raise SystemExit(f"Schema digest changed: expected {expected}, generated {digest}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"{args.output} {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
