from __future__ import annotations

from pathlib import Path

from scripts.export_schemas import schema_bundle, schema_sha256


def test_c2_v1_schema_digest_is_frozen(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    expected = (root / "schemas" / "tool-contracts-v1.sha256").read_text(encoding="utf-8").strip()

    assert schema_sha256(schema_bundle(tmp_path / "schema")) == expected
