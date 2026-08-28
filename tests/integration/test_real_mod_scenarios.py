from __future__ import annotations

from pathlib import Path

import pytest

from reframework_mcp.usage import LuaUsageExtractor


@pytest.mark.parametrize(
    ("fixture_name", "required_kinds", "required_symbols"),
    [
        (
            "mhst3_area_eco.lua",
            {"managed_singleton", "field_read", "method_call", "hook_install"},
            {"app.EcoManager", "_Area", "getEggAreaList(app.OtomonDef.ID_Fixed)"},
        ),
        (
            "mhst3_item_box.lua",
            {"managed_singleton", "field_read", "method_call"},
            {"_Item", "_ItemData", "addSaveItem(app.ItemID.ID_Fixed, System.UInt32)"},
        ),
        (
            "mhst3_lock_nest.lua",
            {"type_lookup", "method_lookup", "hook_install", "hook_argument"},
            {
                "setupNest(app.NestDef.TYPE, app.NestDef.RARE)",
                "setupFesData(app.user_data.NestTableData.cData)",
                "args[5]",
            },
        ),
        (
            "mhws_box_item.lua",
            {"managed_singleton", "field_read", "method_call", "collection_iteration"},
            {"getCurrentUserSaveData()", "_BoxItem", "ItemIdFixed", "box"},
        ),
    ],
)
def test_real_mod_pattern_regressions(
    fixture_root: Path,
    fixture_name: str,
    required_kinds: set[str],
    required_symbols: set[str],
) -> None:
    path = fixture_root / "mod_scenarios" / fixture_name
    sites = LuaUsageExtractor().extract(
        path.read_text(encoding="utf-8"),
        fixture_name,
    )

    kinds = {site.kind for site in sites}
    symbols = {site.symbol for site in sites}
    assert required_kinds <= kinds
    assert required_symbols <= symbols
