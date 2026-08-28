local save = sdk.get_managed_singleton("app.SaveDataManager"):call("get_UserSaveData()")
local helper = sdk.get_managed_singleton("app.SaveDataManager"):get_field("_Helper")
local item = helper:get_field("_Item")
local setting = sdk.get_managed_singleton("app.VariousDataManager"):get_field("_Setting")
local item_data = setting:get_field("_ItemData"):get_field("_ItemData")
local current = item:call("getSaveItemStock(app.ItemID.ID_Fixed)", 1)
local changed = item:call(
    "addSaveItem(app.ItemID.ID_Fixed, System.UInt32)",
    1,
    10
)
