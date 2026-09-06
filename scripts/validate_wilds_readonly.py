"""Manual regression for the paired Wilds runtime; reads the save and allocates temporary collections."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from reframework_mcp.bridge import BridgeClient, NamedPipeTransport
from reframework_mcp.errors import ErrorCode, ReframeworkMCPError
from reframework_mcp.lua import LuaDraftService
from reframework_mcp.metadata import MetadataRepository
from reframework_mcp.models import AccessPlan
from reframework_mcp.storage import Database


async def validate(output: Path, settle_seconds: int = 90) -> None:
    bridge = BridgeClient(NamedPipeTransport(r"\\.\pipe\reframework-mcp-v1"))
    results: dict[str, Any] = {"runtime": await bridge.probe()}
    assert results["runtime"]["connected"], results["runtime"]
    output.parent.mkdir(parents=True, exist_ok=True)
    # READ_FIELD drafts do not need metadata lookup; the live TDB resolves their exact signatures.
    draft_service = LuaDraftService(MetadataRepository(Database(output.with_suffix(".unused.db"))))

    async def probe(code: str) -> Any:
        response = await bridge.call("run_lua_probe", {"code": code, "mode": "oneshot", "max_events": 1})
        assert response["state"] == "completed" and response["event_count"] == 1, response
        assert response["dropped"] == 0, response
        return response["events"][0]["value"]

    async def page(reference: str, offset: int, limit: int, max_bytes: int = 1_048_576) -> Any:
        response = await bridge.call(
            "inspect_object",
            {
                "object_ref": reference,
                "depth": 0,
                "collection_offset": offset,
                "collection_limit": limit,
                "max_bytes": max_bytes,
            },
        )
        return response["nodes"][0]["collection"]

    async def compare(plan: dict[str, Any], label: str) -> Any:
        selected = AccessPlan.model_validate(plan)
        native = await bridge.call("validate_access_plan", {"plan": selected.model_dump(mode="json")})
        assert native["status"] == "valid", native
        code = draft_service.draft(selected)["code"]
        lua = await probe(code)
        expected = native["steps"][-1]["value_summary"]
        assert lua == expected, {"case": label, "native": expected, "lua": lua}
        results[label] = expected
        return expected

    try:
        objects = await probe("""
local user=sdk.get_managed_singleton("app.SaveDataManager"):call("getCurrentUserSaveData()")
assert(user~=nil, "Load a Wilds save before running this check")
local prefix="System.Collections.Generic."
local list=probe.construct_list(prefix.."List"..string.char(96).."1<System.Int32>",{10,20,30,40})
list:call("RemoveAt(System.Int32)",1)
local empty=probe.construct_list(prefix.."List"..string.char(96).."1<System.Int32>",{})
local strings=probe.construct_list(prefix.."List"..string.char(96).."1<System.String>",{"first","last"})
local dict=probe.construct_dictionary(prefix.."Dictionary"..string.char(96).."2<System.Int32,System.Int32>",{[11]=110,[22]=220,[33]=330})
dict:call("Remove(System.Int32)",22)
probe.emit({box=probe.export_value(user:get_field("_Item"):get_field("_BoxItem")),
            list=probe.export_value(list),empty=probe.export_value(empty),
            strings=probe.export_value(strings),dictionary=probe.export_value(dict)})
""")
        references = {name: value["object_ref"] for name, value in objects.items()}
        first = await page(references["box"], 0, 3)
        assert len(first["items"]) == min(3, first["logical_count"])
        count = first["logical_count"]
        assert (await page(references["box"], count, 3))["items"] == []
        tail = await page(references["box"], max(0, count - 3), 10)
        assert tail["next_offset"] is None
        limited = await page(references["box"], 0, 1000, 4096)
        if limited["truncated"]:
            assert limited["next_offset"] == limited["items"][-1]["index"] + 1
        results["box_paging"] = {"logical_count": count, "tail": tail, "byte_limited": limited}

        listing = await page(references["list"], 0, 100)
        assert listing["logical_count"] == 3
        assert [item["value"] for item in listing["items"]] == [10, 30, 40]
        assert (await page(references["empty"], 0, 10))["items"] == []
        assert [item["value"] for item in (await page(references["strings"], 0, 10))["items"]] == [
            "first",
            "last",
        ]
        dictionary = []
        offset = 0
        while True:
            current = await page(references["dictionary"], offset, 1)
            assert current["logical_count"] == 2
            dictionary.extend(current["items"])
            if current["next_offset"] is None:
                break
            assert current["next_offset"] > offset
            offset = current["next_offset"]
        assert {item["key"]: item["value"] for item in dictionary} == {11: 110, 33: 330}
        results["generic_collections"] = {"list": listing, "dictionary": dictionary}

        for limit in (3, min(1000, count)):
            nodes = [
                {
                    "node_id": "box",
                    "operation": "resolve_root",
                    "root": {"kind": "provided_object_ref", "object_ref": references["box"]},
                    "output_type": objects["box"]["type"],
                },
                {"node_id": "slots", "operation": "iterate", "inputs": ["box"], "options": {"limit": limit}},
                {
                    "node_id": "count",
                    "operation": "read_field",
                    "inputs": ["slots"],
                    "member": {
                        "snapshot_id": "manual:wilds",
                        "kind": "tdb_field",
                        "canonical_signature": "app.savedata.cItemWork::Num: System.Int16",
                    },
                    "output_type": "System.Int16",
                },
            ]
            plan = {
                "snapshot_id": "manual:wilds",
                "goal": "Read box counts",
                "nodes": nodes,
                "targets": ["count"],
            }
            sequence = await compare(plan, f"box_counts_{limit}")
            assert len(sequence["items"]) == limit
            filtered = {
                **plan,
                "nodes": [
                    *nodes,
                    {
                        "node_id": "positive",
                        "operation": "filter",
                        "inputs": ["count"],
                        "options": {"operator": "gt", "value": 0},
                    },
                ],
                "targets": ["positive"],
            }
            await compare(filtered, f"positive_counts_{limit}")
            indexed = {
                **plan,
                "nodes": [
                    *nodes,
                    {"node_id": "one", "operation": "index", "inputs": ["count"], "options": {"index": 0}},
                ],
                "targets": ["one"],
            }
            await compare(indexed, f"first_count_{limit}")

        for operation, expected, options in (
            ("assert_range", "System.Int32", {"min": 0, "max": 10}),
            ("assert_type", "System.String", {}),
            ("cast", "System.String", {}),
        ):
            plan = {
                "nodes": [
                    {
                        "node_id": "value",
                        "operation": "bind_constant",
                        "value": 99,
                        "output_type": "System.Int32",
                    },
                    {
                        "node_id": "guard",
                        "operation": operation,
                        "inputs": ["value"],
                        "output_type": expected,
                        "options": options,
                    },
                ],
                "targets": ["guard"],
            }
            response = await bridge.call("validate_access_plan", {"plan": plan})
            assert response["status"] == "failed" and response["failed_node"] == "guard", response
            results[operation] = response

        enum_result = await bridge.call(
            "run_lua_probe",
            {
                "code": 'probe.assert_type(4294967295, "app.ItemDef.ID")',
                "mode": "oneshot",
                "max_events": 1,
            },
        )
        assert enum_result["state"] == "failed", enum_result
        results["enum_width"] = enum_result

        string_member = {
            "snapshot_id": "manual:wilds",
            "kind": "tdb_method",
            "canonical_signature": "System.String::IsNullOrEmpty(System.String) -> System.Boolean",
        }
        before_strings = await bridge.probe()
        string_results = []
        for text in ("", "Wilds", "\u8352\u91ce", "line\nbreak", "\U0001f409"):
            plan = {
                "nodes": [
                    {
                        "node_id": "text",
                        "operation": "bind_constant",
                        "value": text,
                        "output_type": "System.String",
                    },
                    {
                        "node_id": "call",
                        "operation": "call_static",
                        "member": string_member,
                        "arguments": ["text"],
                        "output_type": "System.Boolean",
                    },
                ],
                "targets": ["call"],
            }
            dry_run = await bridge.call("validate_access_plan", {"plan": plan})
            assert dry_run["status"] == "valid", dry_run
            assert dry_run["steps"][-1]["value_summary"]["execution"] == "dry_run", dry_run
            answer = await bridge.call("invoke_method", {"member_ref": string_member, "arguments": [text]})
            assert answer["result"] is (text == ""), answer
            string_results.append({"text": text, "result": answer["result"]})
        equal_member = {
            **string_member,
            "canonical_signature": "System.String::op_Equality(System.String, System.String) -> System.Boolean",
        }
        for left, right, expected in (
            ("\u8352\u91ce", "\u8352\u91ce", True),
            ("\u8352\u91ce", "\u8352\u5ddd", False),
        ):
            answer = await bridge.call(
                "invoke_method", {"member_ref": equal_member, "arguments": [left, right]}
            )
            assert answer["result"] is expected, answer
        after_strings = await bridge.probe()
        assert after_strings["object_registry_size"] <= before_strings["object_registry_size"]
        results["string_arguments"] = {
            "cases": string_results,
            "unicode_equality": True,
            "registry_before": before_strings["object_registry_size"],
            "registry_after": after_strings["object_registry_size"],
        }

        for index in range(100):
            assert await probe(f'probe.emit("sequence", {index})') == index
        results["isolated_probe_cycles"] = 100
        try:
            await bridge.call("inspect_object", {"object_ref": "obj:missing"})
        except ReframeworkMCPError as error:
            assert error.code == ErrorCode.OBJECT_EXPIRED, error
            results["structured_error"] = error.code.value
        else:
            raise AssertionError("Expired ObjectRef was accepted")
        print(
            f"Functional checks passed; waiting {settle_seconds}s for ObjectRef leases to expire", flush=True
        )
        started = asyncio.get_running_loop().time()
        samples = []
        while (elapsed := asyncio.get_running_loop().time() - started) < settle_seconds:
            await asyncio.sleep(min(15, settle_seconds - elapsed))
            status = await bridge.probe()
            assert status["connected"] and status["runtime_epoch"] == results["runtime"]["runtime_epoch"], (
                status
            )
            samples.append(
                {"elapsed_seconds": round(asyncio.get_running_loop().time() - started, 2), **status}
            )
            results["lease_expiry_samples"] = samples
            output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Lease expiry check: {samples[-1]['elapsed_seconds']}s, connected", flush=True)
        for reference in references.values():
            try:
                await bridge.call("inspect_object", {"object_ref": reference})
            except ReframeworkMCPError as error:
                assert error.code == ErrorCode.OBJECT_EXPIRED, error
            else:
                raise AssertionError(f"ObjectRef still valid after expiry: {reference}")
        results["expired_reference_count"] = len(references)
        results["passed"] = True
        print(
            f"Wilds live checks passed: {count} box slots; Array/List/Dictionary; guards; 100 isolated probes"
        )
    finally:
        output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".tmp/wilds-readonly-validation.json"))
    parser.add_argument(
        "--settle-seconds", type=int, default=90, help="Observe lease expiry for at least 65 seconds"
    )
    args = parser.parse_args()
    if args.settle_seconds < 65:
        parser.error("--settle-seconds must be at least 65")
    asyncio.run(validate(args.output, args.settle_seconds))


if __name__ == "__main__":
    main()
