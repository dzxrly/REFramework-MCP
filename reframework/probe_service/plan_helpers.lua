-- Shared by bounded collection reads and generated AccessPlan probes.
local unbox
local integer_ranges = {
    ["System.SByte"]={-128,127}, ["System.Byte"]={0,255},
    ["System.Int16"]={-32768,32767}, ["System.UInt16"]={0,65535},
    ["System.Int32"]={-2147483648,2147483647}, ["System.UInt32"]={0,4294967295},
    ["System.Int64"]={math.mininteger,math.maxinteger}, ["System.Char"]={0,65535},
}
function probe.assert_type(value, expected, nullable)
    value = unbox(value)
    if value == nil then
        if nullable then return value end
        error("Expected " .. expected .. ", got nil")
    end
    local range = integer_ranges[expected]
    if range then
        if math.type(value) ~= "integer" or value < range[1] or value > range[2] then error("Expected " .. expected) end
    elseif expected == "System.UInt64" then
        if math.type(value) ~= "integer" or value < 0 then error("Expected non-negative integer") end
    elseif expected == "System.Single" or expected == "System.Double" then
        if type(value) ~= "number" or value ~= value or math.abs(value) == math.huge then error("Expected finite number") end
        if expected == "System.Single" and math.abs(value) > 3.4028234663852886e38 then error("Single overflow") end
    elseif expected == "System.String" then
        if type(value) ~= "string" then error("Expected string") end
    elseif expected == "System.Boolean" then
        if type(value) ~= "boolean" then error("Expected boolean") end
    else
        local wanted = sdk.find_type_definition(expected)
        if wanted == nil then error("Unknown expected type: " .. expected) end
        if wanted:is_enum() then
            local storage = wanted:get_field("value__")
            if storage == nil or storage:get_type() == nil then error("Enum underlying type unavailable") end
            probe.assert_type(value, storage:get_type():get_full_name(), false)
        else
            if type(value) ~= "userdata" then error("Expected managed object: " .. expected) end
            local actual = value:get_type_definition()
            if actual == nil or (actual:get_full_name() ~= expected and not actual:is_a(expected)) then
                error("Incompatible object type: " .. expected)
            end
        end
    end
    return value
end
function probe.assert_range(value, minimum, maximum)
    value = unbox(value)
    if type(value) ~= "number" or value ~= value or math.abs(value) == math.huge then error("Expected finite number") end
    if minimum == nil and maximum == nil then error("Range needs a bound") end
    if minimum ~= nil and (type(minimum) ~= "number" or minimum ~= minimum or math.abs(minimum) == math.huge) then error("Invalid minimum") end
    if maximum ~= nil and (type(maximum) ~= "number" or maximum ~= maximum or math.abs(maximum) == math.huge) then error("Invalid maximum") end
    if minimum ~= nil and maximum ~= nil and minimum > maximum then error("Invalid range") end
    if (minimum ~= nil and value < minimum) or (maximum ~= nil and value > maximum) then error("Value outside range") end
    return value
end
local function field(object, names, required)
    local td = object:get_type_definition()
    for _, name in ipairs(names) do
        if td:get_field(name) ~= nil then return object:get_field(name), name end
    end
    if required then error("Unsupported collection layout: " .. table.concat(names, "/")) end
end
unbox = function(value)
    if type(value) ~= "userdata" then return value end
    local td = value:get_type_definition()
    if td:is_primitive() or td:is_enum() then
        return field(value, {"m_value", "mValue", "value__", "_value", "Value"}, true)
    end
    if td:get_full_name() == "System.String" then return value:call("ToString()") end
    return value
end
local function bounds(offset, limit)
    if math.type(offset) ~= "integer" or offset < 0 or offset > 1000000 then error("Invalid collection offset") end
    if math.type(limit) ~= "integer" or limit < 1 or limit > 1000 then error("Invalid collection limit") end
end
function probe.collection_page(object, offset, limit)
    offset, limit = offset or 0, limit or 100
    bounds(offset, limit)
    if object == nil then error("Collection is nil") end
    local name = object:get_type_definition():get_full_name()
    local page = {supported=true, offset=offset, limit=limit, items={}}
    local count, storage, adapter
    if string.sub(name, -2) == "[]" or name == "System.Array" or object:get_type_definition():is_a("System.Array") then
        count, storage, adapter = object:get_size(), object, "system_array"
    elseif string.match(name, "^System%.Collections%.Generic%.List`1<") or string.match(name, "^System%.Collections%.Generic%.List<") then
        count = field(object, {"_size", "size", "mSize"}, true)
        storage = field(object, {"_items", "items", "mItems"}, true)
        adapter = "generic_list"
    elseif string.match(name, "^System%.Collections%.Generic%.Dictionary`2<") or string.match(name, "^System%.Collections%.Generic%.Dictionary<") then
        local allocated = field(object, {"_count", "count", "mCount"}, true)
        local free = field(object, {"_freeCount", "freeCount", "mFreeCount"}, true)
        count = allocated - free
        storage = field(object, {"_entries", "entries", "mEntries"}, true)
        page.adapter, page.logical_count = "generic_dictionary", count
        local cursor, scanned = offset, 0
        local stop = math.min(allocated, storage and storage:get_size() or 0)
        while cursor < stop and #page.items < limit and scanned < 1000 do
            local entry = storage:get_element(cursor)
            local hash, hash_name = field(entry, {"hashCode", "_hashCode"}, false)
            local next_slot, next_name = field(entry, {"next", "_next"}, false)
            if hash_name == nil or next_name == nil then error("Unsupported dictionary entry layout") end
            if hash >= 0 and next_slot >= -1 then
                local key = field(entry, {"key", "_key"}, true)
                local value = field(entry, {"value", "_value"}, true)
                page.items[#page.items+1] = {index=cursor, key=unbox(key), value=unbox(value)}
            end
            cursor, scanned = cursor+1, scanned+1
        end
        page.next_offset = cursor < stop and cursor or nil
        page.truncated = cursor < stop
        return page
    else
        error("Unsupported collection type: " .. name)
    end
    page.adapter, page.logical_count = adapter, count
    if count < 0 or (storage == nil and count ~= 0) or (storage and count > storage:get_size()) then error("Collection changed or has invalid bounds") end
    local stop = math.min(count, offset+limit)
    for index=offset,stop-1 do
        page.items[#page.items+1] = {index=index, value=unbox(storage:get_element(index))}
    end
    page.next_offset = stop < count and stop or nil
    page.truncated = stop < count
    return page
end
function probe.iterate(object, offset, limit)
    local page = probe.collection_page(object, offset, limit)
    page.__refmcp_sequence = true
    return page
end
function probe.map_sequence(sequence, fn)
    local result = {}
    for key,value in pairs(sequence) do result[key]=value end
    result.items={}
    for _,item in ipairs(sequence.items) do
        result.items[#result.items+1] = {index=item.index, key=item.key, value=fn(item.value)}
    end
    return result
end
function probe.filter_sequence(sequence, field_name, operator, expected, offset, limit)
    if not (type(sequence) == "table" and sequence.__refmcp_sequence) then
        sequence = probe.iterate(sequence, offset, limit)
    end
    local result = probe.map_sequence(sequence, function(value) return value end)
    result.items={}
    for _,item in ipairs(sequence.items) do
        local value = item.value
        if field_name ~= nil and field_name ~= "" then value=value:get_field(field_name) end
        if operator ~= "eq" and operator ~= "ne" and (type(value) ~= "number" or type(expected) ~= "number") then error("Ordered filters require numeric operands") end
        local matched
        if operator == "eq" then matched=value==expected
        elseif operator == "ne" then matched=value~=expected
        elseif operator == "lt" then matched=value<expected
        elseif operator == "le" then matched=value<=expected
        elseif operator == "gt" then matched=value>expected
        elseif operator == "ge" then matched=value>=expected
        else error("Unsupported filter operator") end
        if matched then result.items[#result.items+1]=item end
    end
    return result
end
function probe.index_collection(object, index)
    bounds(index, 1)
    if type(object) == "table" and object.__refmcp_sequence then
        local item = object.items[index+1]
        if item == nil then error("Sequence index out of bounds") end
        return item.value
    end
    local page=probe.collection_page(object,index,1)
    if page.adapter == "generic_dictionary" then error("Dictionary index lookup is not supported; use paging") end
    if #page.items ~= 1 then error("Collection index out of range") end
    return page.items[1].value
end
function probe.export_value(value, depth)
    depth=depth or 0
    if depth > 8 then error("Value exceeds serialization depth") end
    value = unbox(value)
    if type(value) == "userdata" then
        local td=value:get_type_definition()
        return {object_ref=probe._store_object(sdk.to_ptr(value)), type=td:get_full_name(), lease_seconds=60}
    elseif type(value) == "table" then
        local result={}
        for key,child in pairs(value) do
            if key ~= "__refmcp_sequence" then result[key]=probe.export_value(child, depth+1) end
        end
        return result
    end
    return value
end
