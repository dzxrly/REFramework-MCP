sdk.hook(
    sdk.find_type_definition("app.NestController"):get_method(
        "setupNest(app.NestDef.TYPE, app.NestDef.RARE)"
    ),
    function(args)
        local nest_type = sdk.to_int64(args[4])
        local rarity = sdk.to_int64(args[5])
        probe.emit("nest", { nest_type = nest_type, rarity = rarity })
    end,
    nil
)

sdk.hook(
    sdk.find_type_definition("app.NestDungeonControllerData"):get_method(
        "setupFesData(app.user_data.NestTableData.cData)"
    ),
    function(args)
        local this = sdk.to_managed_object(args[2])
        this:call("set_FesType(app.NestDef.FES_TYPE)", 1)
    end,
    nil
)
