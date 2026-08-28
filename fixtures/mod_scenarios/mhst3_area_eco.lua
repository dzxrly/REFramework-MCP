local helper = sdk.get_managed_singleton("app.SaveDataManager"):get_field("_Helper")
local area = helper:get_field("_Area")
local eco = sdk.get_managed_singleton("app.EcoManager")
local list = eco:call("getEggAreaList(app.OtomonDef.ID_Fixed)", 1)
local count = list:call("get_Count()")

sdk.hook(
    sdk.find_type_definition("app.SaveDataManager"):get_method("getTitleText()"),
    function(args)
        probe.emit("save", sdk.to_managed_object(args[2]))
    end,
    nil
)
