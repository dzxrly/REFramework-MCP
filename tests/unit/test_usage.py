from __future__ import annotations

from pathlib import Path

from reframework_mcp.storage import Database
from reframework_mcp.usage import UsageIndexer


def test_usage_indexer_extracts_roots_chains_and_hooks(
    tmp_path: Path,
    fixture_root: Path,
) -> None:
    database = Database(tmp_path / "usage.db")
    database.initialize()
    indexer = UsageIndexer(database)

    result = indexer.index_project(fixture_root / "mod_usage", game_id="mhwilds")
    matches = indexer.search("SaveDataManager", game_id="mhwilds")

    assert result["file_count"] == 1
    assert result["usage_count"] >= 6
    assert matches["items"][0]["usage_kind"] == "managed_singleton"
    with database.connect() as connection:
        root = connection.execute("SELECT root_kind, type_name FROM root_hints").fetchone()
    assert dict(root) == {
        "root_kind": "managed_singleton",
        "type_name": "app.SaveDataManager",
    }
