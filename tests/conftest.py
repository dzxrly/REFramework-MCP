from __future__ import annotations

from pathlib import Path

import pytest

from reframework_mcp.metadata import Il2CppDumpImporter, MetadataRepository
from reframework_mcp.storage import Database


@pytest.fixture
def fixture_root() -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def imported_database(tmp_path: Path, fixture_root: Path) -> tuple[Database, str]:
    database = Database(tmp_path / "metadata.db")
    database.initialize()
    result = Il2CppDumpImporter(database).import_file(
        fixture_root / "il2cpp_dump" / "minimal_chain.json",
        manifest={
            "snapshot_id": "snapshot:test",
            "game_id": "mhwilds",
            "tdb_version": 81,
            "tdb_fingerprint": "sha256:test",
        },
    )
    return database, result.snapshot_id


@pytest.fixture
def metadata(imported_database: tuple[Database, str]) -> MetadataRepository:
    return MetadataRepository(imported_database[0])
