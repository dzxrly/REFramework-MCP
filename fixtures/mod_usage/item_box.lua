local save_manager = sdk.get_managed_singleton("app.SaveDataManager")
local user_save = save_manager:call("getCurrentUserSaveData()")
local item_param = user_save:get_field("_Item")
local item_box = item_param:get_field("_BoxItem")
local add_item = user_save:get_method("addSaveItem(System.Int32, System.Int32)")

sdk.hook(add_item, function(args)
    return args
end, function(retval)
    return retval
end)
