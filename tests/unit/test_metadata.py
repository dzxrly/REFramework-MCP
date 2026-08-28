from __future__ import annotations

from reframework_mcp.metadata import MetadataRepository


def test_type_edges_support_indexed_member_evidence_lookup(
    metadata: MetadataRepository,
) -> None:
    with metadata.database.connect() as connection:
        indexes = {
            str(row["name"]) for row in connection.execute("PRAGMA index_list('type_edges')").fetchall()
        }

    assert "idx_type_edges_member" in indexes


def test_search_members_preserves_overloads(metadata: MetadataRepository) -> None:
    result = metadata.search_members(
        "addSaveItem",
        declaring_type="app.UserSaveData",
    )

    signatures = {item["canonical_signature"] for item in result["items"]}
    assert signatures == {
        "app.UserSaveData::addSaveItem(System.Int32, System.Int32) -> System.Boolean",
        "app.UserSaveData::addSaveItem(app.ItemId, System.Int32, System.Boolean) -> System.Boolean",
    }
    assert all(item["member_ref"]["snapshot_id"] == "snapshot:test" for item in result["items"])


def test_importer_preserves_distinct_tdb_slots_with_identical_signatures(
    metadata: MetadataRepository,
) -> None:
    result = metadata.search_members(
        "CreateInstance",
        declaring_type="System.Activator",
    )

    assert len(result["items"]) == 2
    assert {item["source_member_id"] for item in result["items"]} == {401, 402}
    assert len({item["stable_key"] for item in result["items"]}) == 2
    assert {item["canonical_signature"] for item in result["items"]} == {
        "System.Activator::CreateInstance() -> !!0"
    }


def test_search_members_filters_parameter_type(metadata: MetadataRepository) -> None:
    result = metadata.search_members(
        "addSaveItem",
        parameter_type="app.ItemId",
    )

    assert [item["member_pk"] for item in result["items"]]
    assert len(result["items"]) == 1
    assert "app.ItemId" in result["items"][0]["canonical_signature"]


def test_describe_type_reports_property_reflection_and_rsz(
    metadata: MetadataRepository,
) -> None:
    result = metadata.describe_type("app.UserSaveData")

    kinds = {member["kind"] for member in result["members"]}
    edge_kinds = {edge["edge_kind"] for edge in result["dependencies"]}
    assert {"tdb_property", "reflection_method", "reflection_property"} <= kinds
    assert {"FIELD_TYPE", "RSZ_CONTAINS", "DESERIALIZER_USES"} <= edge_kinds
    assert result["coverage"]["reflection_properties"] == "partial_version_dependent"


def test_type_dependency_chain(metadata: MetadataRepository) -> None:
    result = metadata.find_type_dependencies(
        "app.SaveDataManager",
        direction="outgoing",
        max_depth=4,
    )

    assert "app.ItemSlot" in result["nodes"]
    assert any(edge["edge_kind"] == "GENERIC_ARGUMENT" for edge in result["edges"])


def test_importer_synthesizes_logical_property_from_accessors(
    metadata: MetadataRepository,
) -> None:
    result = metadata.search_members(
        "ItemCount",
        member_kinds=["synthetic_property"],
    )

    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["getter_name"] == "get_ItemCount"
    assert item["setter_name"] == "set_ItemCount"
    assert item["value_type"] == "System.Int32"
    assert item["raw"]["inference_rule"] == "get_X/set_X"
