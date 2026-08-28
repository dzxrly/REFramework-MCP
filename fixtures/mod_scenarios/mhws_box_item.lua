local save = sdk.get_managed_singleton("app.SaveDataManager")
local user = save:call("getCurrentUserSaveData()")
local item_param = user:get_field("_Item")
local box = item_param:get_field("_BoxItem")
for _, item in pairs(box) do
    local id = item:get_field("ItemIdFixed")
    local count = item:get_field("Num")
end

local pay_money = sdk.find_type_definition("app.FacilityUtil"):get_method(
    "payMoney(System.Int32)"
)
pay_money(nil, 100)
