#include "access_plan_executor.hpp"
#include "bridge.hpp"
#include "command_error.hpp"
#include <algorithm>
#include <cmath>
#include <limits>
#include <vector>

namespace reframework_mcp {
using json = nlohmann::json;
using Type = reframework::API::TypeDefinition;
namespace {
bool sequence(const json& value) {
    return value.is_object() && value.value("__refmcp_sequence", false);
}
std::string string_option(const json& node, const char* key, const std::string& fallback = {}) {
    const auto it = node.find(key);
    return it == node.end() || it->is_null() ? fallback : it->get<std::string>();
}
void invalid(const std::string& message) { throw BridgeError{"VALIDATION_FAILED", message}; }
std::size_t bounded(const json& options, const char* key, std::size_t fallback, std::size_t maximum, std::size_t minimum = 0) {
    const auto value = options.value(key, json(fallback));
    if (!value.is_number_integer() || value.get<long double>() < minimum || value.get<long double>() > maximum)
        throw BridgeError{"PLAN_INVALID", std::string{"Invalid "} + key};
    return value.get<std::size_t>();
}
}

void AccessPlanExecutor::check_type(const json& value, const std::string& expected, bool nullable) {
    if (value.is_null()) {
        if (nullable) return;
        invalid("Expected non-null " + expected);
    }
    if (expected.empty()) throw BridgeError{"PLAN_INVALID", "Expected type is missing"};
    auto* target = reframework::API::get()->tdb()->find_type(expected);
    auto name = expected;
    if (target && target->is_enum()) {
        auto* underlying = target->get_underlying_type();
        if (underlying == nullptr) throw BridgeError{"CAPABILITY_UNAVAILABLE", "Enum underlying type unavailable"};
        name = underlying->get_full_name();
    }
    if (name == "System.String") {
        if (value.is_string()) return;
        if (value.is_object() && value.contains("object_ref")) {
            const auto entry = b.require_object(value["object_ref"]);
            if (entry.type == target) return;
        }
        invalid("Expected String");
    }
    if (name == "System.Boolean") { if (!value.is_boolean()) invalid("Expected Boolean"); return; }
    if (name == "System.Single" || name == "System.Double") {
        if (!value.is_number() || !std::isfinite(value.get<double>())) invalid("Expected finite number");
        if (name == "System.Single" && std::abs(value.get<double>()) > std::numeric_limits<float>::max()) invalid("Single overflow");
        return;
    }
    struct Integer { const char* name; int bits; bool sign; };
    for (const auto& integer : {
        Integer{"System.SByte", 8, true}, Integer{"System.Byte", 8, false},
        Integer{"System.Int16", 16, true}, Integer{"System.UInt16", 16, false},
        Integer{"System.Int32", 32, true}, Integer{"System.UInt32", 32, false},
        Integer{"System.Int64", 64, true}, Integer{"System.UInt64", 64, false},
        Integer{"System.Char", 16, false}}) {
        if (name != integer.name) continue;
        if (!value.is_number_integer()) invalid("Expected integer " + name);
        if (integer.bits == 64) {
            if (integer.sign && value.is_number_unsigned() && value.get<std::uint64_t>() > INT64_MAX) invalid("Int64 overflow");
            if (!integer.sign && !value.is_number_unsigned() && value.get<std::int64_t>() < 0) invalid("UInt64 underflow");
        } else {
            const auto lower = integer.sign ? -std::ldexp(1.0, integer.bits-1) : 0.0;
            const auto upper = std::ldexp(1.0, integer.bits - (integer.sign ? 1 : 0))-1.0;
            const auto actual = value.get<double>();
            if (actual < lower || actual > upper) invalid("Integer outside " + name + " range");
        }
        return;
    }
    if (!target) throw BridgeError{"TYPE_NOT_FOUND", "Unknown expected type: " + expected};
    std::string reference;
    if (value.is_string() && value.get<std::string>().starts_with("obj:")) reference=value.get<std::string>();
    else if (value.is_object()) reference=value.value("object_ref", "");
    if (reference.empty()) invalid("Expected ObjectRef of type " + expected);
    const auto entry = b.require_object(reference);
    if (!entry.type || (entry.type != target && !entry.type->is_derived_from(target))) invalid("Incompatible object type: " + expected);
}

json AccessPlanExecutor::resolve_root(const json& root) {
    const auto kind = root.at("kind").get<std::string>();
    const auto name = string_option(root, "type_name");
    if (kind == "provided_object_ref") {
        const auto reference = root.at("object_ref").get<std::string>();
        const auto entry = b.require_object(reference);
        return {{"object_ref", reference}, {"type", entry.type ? entry.type->get_full_name() : ""}};
    }
    if (kind == "managed_singleton") {
        auto* object = reframework::API::get()->get_managed_singleton(name);
        if (!object) invalid("Managed singleton is null");
        return {{"object_ref", b.m_objects.put_managed(object, object->get_type_definition())},
            {"type", object->get_type_definition()->get_full_name()}, {"lease_seconds", 60}};
    }
    if (kind == "native_singleton") {
        for (const auto& singleton : reframework::API::get()->get_native_singletons()) {
            auto* type = reinterpret_cast<Type*>(singleton.t);
            const auto registered = singleton.name ? std::string{singleton.name} : type ? type->get_full_name() : "";
            if (registered != name && (!type || type->get_full_name() != name)) continue;
            if (!singleton.instance) invalid("Native singleton is null");
            return {{"object_ref", b.m_objects.put_native(singleton.instance, type, registered)},
                {"type", type ? type->get_full_name() : ""}, {"lease_seconds", 60}};
        }
        throw BridgeError{"TYPE_NOT_FOUND", "Native singleton not found"};
    }
    if (kind == "static_type") {
        if (!reframework::API::get()->tdb()->find_type(name)) throw BridgeError{"TYPE_NOT_FOUND", "Static type not found"};
        return {{"type", name}};
    }
    throw BridgeError{"CAPABILITY_UNAVAILABLE", "Unsupported live root kind: " + kind};
}

json AccessPlanExecutor::read_field(const json& member, const json& input) {
    const auto entry = b.require_object(input.at("object_ref").get<std::string>());
    const auto [declaring, selector] = b.split_member_signature(member);
    check_type(input, declaring);
    auto* field = entry.type->find_field(b.field_name(selector));
    if (!field) throw BridgeError{"MEMBER_NOT_FOUND", "Field not found"};
    return b.read_field(entry, field).at("value");
}

json AccessPlanExecutor::call(const json& node, const json& input, const json& payload) {
    const auto operation = node.at("operation").get<std::string>();
    const auto [declaring, selector] = b.split_member_signature(node.at("member"));
    Type* type = reframework::API::get()->tdb()->find_type(declaring);
    reframework::API::ManagedObject* object{};
    if (operation != "call_static") {
        check_type(input, declaring);
        const auto entry = b.require_object(input.at("object_ref").get<std::string>());
        object = entry.managed;
        if (!object) throw BridgeError{"CAPABILITY_UNAVAILABLE", "Native method receivers are unsupported"};
    }
    if (!type) throw BridgeError{"TYPE_NOT_FOUND", "Method declaring type not found"};
    json arguments = json::array();
    for (const auto& id : node.value("arguments", json::array())) arguments.push_back(values.at(id.get<std::string>()));
    auto method_selector = operation == "read_property" ? "get_" + b.field_name(selector) + "()" : selector;
    auto* method = b.find_method_exact(type, method_selector, arguments.size());
    if (!method && operation == "read_property") method = b.find_method_exact(type, "is_" + b.field_name(selector) + "()", arguments.size());
    if (!method) throw BridgeError{"MEMBER_NOT_FOUND", "Exact method or getter not found"};
    if (method->is_static() != (operation == "call_static")) invalid("Static/instance method mismatch");
    const auto parameters = method->get_params();
    for (std::size_t index=0; index<parameters.size(); ++index) {
        auto* parameter_type = reinterpret_cast<Type*>(parameters[index].t);
        if (!parameter_type) throw BridgeError{"TYPE_NOT_FOUND", "Parameter type missing"};
        check_type(arguments[index], parameter_type->get_full_name(), !parameter_type->is_valuetype());
    }
    // Check the codec without allocating VM strings during a dry run.
    static_cast<void>(b.encode_arguments(method, arguments, false));
    const auto targets = payload.at("plan").value("targets", std::vector<std::string>{});
    if (std::ranges::find(targets, node.at("node_id").get<std::string>()) != targets.end())
        return {{"validated_call", true}, {"member_signature", node.at("member").at("canonical_signature")},
            {"argument_count", arguments.size()}, {"execution", "dry_run"}};
    const auto allowlist = payload.value("getter_allowlist", json::array());
    const auto signature = node.at("member").at("canonical_signature");
    if (!payload.value("allow_getters", false) || !arguments.empty() ||
        std::ranges::find(allowlist, signature) == allowlist.end())
        throw BridgeError{"POLICY_DENIED", "Intermediate calls require an explicit, exact getter_allowlist entry"};
    return b.invoke(method, object, arguments);
}

json AccessPlanExecutor::collection(const json& input, std::size_t offset, std::size_t limit) {
    const auto reference = input.at("object_ref").get<std::string>();
    const auto entry = b.require_object(reference);
    auto page = b.inspect_collection(reference, entry, offset, limit);
    if (!page.is_object() || !page.value("supported", false))
        throw BridgeError{"CAPABILITY_UNAVAILABLE", "Collection adapter unavailable"};
    page["__refmcp_sequence"] = true;
    return page;
}

json AccessPlanExecutor::evaluate(const json& node, const json& input, const json& payload) {
    const auto operation = node.at("operation").get<std::string>();
    const auto options = node.value("options", json::object());
    if (options.value("placeholder", false) || options.value("requires_user_value", false) || options.value("requires_user_population", false))
        throw BridgeError{"PLAN_INVALID", "Unbound plan node"};
    if (sequence(input) && operation != "iterate" && operation != "filter" && operation != "index" && operation != "emit") {
        auto result = input;
        for (auto& item : result["items"]) {
            item["value"] = evaluate(node, item.value("value", json{}), payload);
            if (item["value"].is_null() && !node.value("nullable", true)) invalid("Non-null sequence element required");
        }
        return result;
    }
    if (operation == "resolve_root") {
        auto value = resolve_root(node.at("root"));
        if (value.contains("object_ref") && !string_option(node, "output_type").empty()) check_type(value, node["output_type"]);
        return value;
    }
    if (operation == "resolve_type") return resolve_root({{"kind", "static_type"}, {"type_name", node.at("output_type")}});
    if (operation == "bind_constant") {
        auto value = node.value("value", json{});
        check_type(value, string_option(node, "output_type"), node.value("nullable", true));
        return value;
    }
    if (operation == "read_field") return read_field(node.at("member"), input);
    if (operation == "read_property" || operation == "call_method" || operation == "call_static") return call(node, input, payload);
    if (operation == "cast" || operation == "assert_type") {
        check_type(input, string_option(node, "output_type", string_option(options, "expected_type")));
        return input;
    }
    if (operation == "assert_non_null") { if (input.is_null()) invalid("Expected non-null input"); return input; }
    if (operation == "assert_range") {
        if (!input.is_number() || !std::isfinite(input.get<double>())) invalid("Expected finite range input");
        if (!options.contains("min") && !options.contains("max")) throw BridgeError{"PLAN_INVALID", "Range needs a bound"};
        for (const auto* key : {"min", "max"})
            if (options.contains(key) && (!options[key].is_number() || !std::isfinite(options[key].get<double>())))
                throw BridgeError{"PLAN_INVALID", "Range bounds must be finite numbers"};
        if (options.contains("min") && options.contains("max") && options["min"] > options["max"]) throw BridgeError{"PLAN_INVALID", "Invalid range"};
        if ((options.contains("min") && input < options["min"]) || (options.contains("max") && input > options["max"])) invalid("Value outside range");
        return input;
    }
    if (operation == "iterate") return collection(input, bounded(options,"offset",0,1000000), bounded(options,"limit",100,1000,1));
    if (operation == "index") {
        const auto index = bounded(options,"index",0,1000000);
        if (sequence(input)) {
            if (index >= input.at("items").size()) invalid("Sequence index out of bounds");
            return input.at("items").at(index).value("value", json{});
        }
        auto page = collection(input,index,1);
        if (page.value("adapter", "") == "generic_dictionary") throw BridgeError{"CAPABILITY_UNAVAILABLE", "Dictionary index lookup unsupported"};
        if (page["items"].size() != 1) invalid("Collection index out of bounds");
        return page["items"][0].value("value", json{});
    }
    if (operation == "filter") {
        auto page = sequence(input) ? input : collection(input, bounded(options,"offset",0,1000000), bounded(options,"limit",100,1000,1));
        json selected = json::array();
        const auto op = options.value("operator", "eq");
        const auto expected = options.at("value");
        for (const auto& item : page.at("items")) {
            auto value = item.value("value", json{});
            const auto name = options.value("field", "");
            if (!name.empty()) {
                const auto entry = b.require_object(value.at("object_ref").get<std::string>());
                auto* field = entry.type ? entry.type->find_field(name) : nullptr;
                if (!field) throw BridgeError{"MEMBER_NOT_FOUND", "Filter field not found"};
                value = b.read_field(entry, field).at("value");
            }
            if (op != "eq" && op != "ne" && !(value.is_number() && expected.is_number()))
                invalid("Ordered filters require numeric operands");
            bool keep{};
            if (op=="eq") keep=value==expected; else if (op=="ne") keep=value!=expected;
            else if (op=="lt") keep=value<expected; else if (op=="le") keep=value<=expected;
            else if (op=="gt") keep=value>expected; else if (op=="ge") keep=value>=expected;
            else throw BridgeError{"PLAN_INVALID", "Unknown filter operator"};
            if (keep) selected.push_back(item);
        }
        page["items"] = std::move(selected);
        return page;
    }
    if (operation == "emit") return input;
    throw BridgeError{"CAPABILITY_UNAVAILABLE", "Unsupported live operation: " + operation};
}

json AccessPlanExecutor::execute(const json& payload) {
    json steps=json::array();
    std::string failed;
    values.clear();
    for (const auto& node : payload.at("plan").at("nodes")) {
        const auto id=node.at("node_id").get<std::string>();
        try {
            if (values.contains(id)) throw BridgeError{"PLAN_INVALID", "Duplicate node"};
            json input;
            const auto inputs=node.value("inputs", json::array());
            if (!inputs.empty()) input=values.at(inputs.at(0).get<std::string>());
            auto value=evaluate(node,input,payload);
            if (value.is_null() && !node.value("nullable", true)) invalid("Non-null node value required");
            values.emplace(id,value);
            const auto expected=string_option(node,"output_type");
            const auto actual=value.is_object() ? value.value("type", expected) : expected;
            if (sequence(value)) value.erase("__refmcp_sequence");
            steps.push_back({{"node_id",id},{"status","valid"},{"expected_type",expected},
                {"actual_type",actual},{"value_summary",value},
                {"object_ref",value.is_object()?value.value("object_ref",""):""}});
        } catch (const BridgeError& error) {
            failed=id;
            steps.push_back({{"node_id",id},{"status","invalid"},{"error_code",error.code},{"message",error.what()}});
            break;
        } catch (const std::exception& error) {
            failed=id;
            steps.push_back({{"node_id",id},{"status","invalid"},{"error_code","PLAN_INVALID"},{"message",error.what()}});
            break;
        }
    }
    return {{"status",failed.empty()?"valid":"failed"},{"steps",std::move(steps)},{"failed_node",failed},
        {"runtime_epoch",b.m_runtime_epoch},{"scene_epoch",nullptr},{"save_epoch",nullptr},{"alternatives",json::array()}};
}
}
