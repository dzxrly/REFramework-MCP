#include "bridge.hpp"

#include <Windows.h>
#include <bcrypt.h>

#include <algorithm>
#include <array>
#include <bit>
#include <chrono>
#include <cctype>
#include <cstdint>
#include <cstring>
#include <iomanip>
#include <ranges>
#include <sstream>
#include <stdexcept>
#include <utility>
#include <vector>

#include <reframework_mcp/protocol.hpp>

namespace reframework_mcp {
namespace {

using json = nlohmann::json;
constexpr auto kCommandTimeout = std::chrono::seconds{10};

bool contains_case_insensitive(const std::string& value, const std::string& query) {
    if (query.empty()) {
        return true;
    }
    auto lowered_value = value;
    auto lowered_query = query;
    std::ranges::transform(lowered_value, lowered_value.begin(), [](unsigned char character) {
        return static_cast<char>(std::tolower(character));
    });
    std::ranges::transform(lowered_query, lowered_query.begin(), [](unsigned char character) {
        return static_cast<char>(std::tolower(character));
    });
    return lowered_value.find(lowered_query) != std::string::npos;
}

std::string optional_string(
    const json& object,
    const std::string& key,
    std::string fallback = {}) {
    const auto value = object.find(key);
    if (value == object.end() || value->is_null()) {
        return fallback;
    }
    return value->get<std::string>();
}

template <typename T>
T load_value(void* raw) {
    T value{};
    std::memcpy(&value, raw, sizeof(T));
    return value;
}

template <typename T>
void store_value(void* raw, T value) {
    std::memcpy(raw, &value, sizeof(T));
}

std::string version_string(const REFrameworkPluginVersion* version) {
    if (version == nullptr) {
        return "unknown";
    }
    return std::to_string(version->major) + "."
        + std::to_string(version->minor) + "."
        + std::to_string(version->patch);
}

} // namespace

class Bridge::CommandError final : public std::runtime_error {
public:
    CommandError(std::string code, std::string message, json details = json::object())
        : std::runtime_error{message},
          code{std::move(code)},
          details{std::move(details)} {
    }

    std::string code;
    json details;
};

Bridge::Bridge(const REFrameworkPluginInitializeParam* parameter)
    : m_parameter{parameter},
      m_game_thread{std::this_thread::get_id()},
      m_runtime_epoch{new_runtime_epoch()},
      m_tdb_fingerprint{tdb_fingerprint(reframework::API::get()->tdb())},
      m_export{reinterpret_cast<HMODULE>(parameter->reframework_module)},
      m_objects{m_runtime_epoch},
      m_hooks{m_objects, m_runtime_epoch},
      m_probe{reinterpret_cast<HMODULE>(parameter->reframework_module)},
      m_pipe{
          pipe_name(),
          [this](const json& request) { return handle(request); }} {
    m_probe.bind_object_registry(m_objects, m_hooks);
}

Bridge::~Bridge() {
    m_pipe.stop();
}

void Bridge::start() {
    m_pipe.start();
}

void Bridge::pump_game_thread() {
    std::deque<std::shared_ptr<PendingCommand>> pending;
    {
        std::scoped_lock lock{m_queue_mutex};
        pending.swap(m_queue);
    }
    for (const auto& task : pending) {
        if (task->cancelled) {
            continue;
        }
        try {
            const auto command = task->request.at("command").get<std::string>();
            const auto payload = task->request.value("payload", json::object());
            task->result.set_value(dispatch_game_thread(command, payload));
        } catch (...) {
            task->result.set_exception(std::current_exception());
        }
    }
    m_hooks.clear_expired();
    m_objects.clear_expired();
    m_probe.tick();
}

json Bridge::handle(const json& request) {
    const auto request_id = request.value("request_id", "");
    try {
        if (!request.is_object()) {
            throw CommandError{"BRIDGE_PROTOCOL_ERROR", "Request must be a JSON object"};
        }
        const auto protocol = request.value("protocol", "");
        if (protocol.empty() || protocol.substr(0, protocol.find('.')) != "1") {
            throw CommandError{
                "BRIDGE_PROTOCOL_ERROR",
                "Bridge protocol major version is incompatible",
                {{"received", protocol}, {"supported", kProtocolVersion}},
            };
        }
        if (request.value("kind", "") != "command") {
            throw CommandError{"BRIDGE_PROTOCOL_ERROR", "Only command envelopes are supported"};
        }
        return ok_response(
            request_id,
            m_runtime_epoch,
            dispatch(
                request.at("command").get<std::string>(),
                request.value("payload", json::object())));
    } catch (const CommandError& error) {
        return error_response(
            request_id,
            m_runtime_epoch,
            error.code,
            error.what(),
            error.details);
    } catch (const std::exception& error) {
        return error_response(
            request_id,
            m_runtime_epoch,
            "INTERNAL_ERROR",
            error.what());
    }
}

const std::string& Bridge::runtime_epoch() const noexcept {
    return m_runtime_epoch;
}

json Bridge::dispatch(const std::string& command, const json& payload) {
    if (command == "runtime_status") {
        return runtime_status();
    }
    if (command == "get_export_status") {
        if (!m_export.available()) {
            throw CommandError{
                "CAPABILITY_UNAVAILABLE",
                "GenerateSdk Export Service ABI 1.0 is unavailable",
                m_export.capability_status(),
            };
        }
        auto result = m_export.invoke(command, payload);
        result["runtime_epoch"] = m_runtime_epoch;
        return result;
    }
    if (command == "get_hook_events") {
        return m_hooks.events(payload.at("hook_ref").get<std::string>());
    }
    if (command == "get_probe_status") {
        if (!m_probe.available()) {
            throw CommandError{
                "CAPABILITY_UNAVAILABLE",
                "Probe Service ABI 1.0 is unavailable",
                m_probe.capability_status(),
            };
        }
        auto result = m_probe.invoke(command, payload);
        result["runtime_epoch"] = m_runtime_epoch;
        return result;
    }
    return submit_game_thread({
        {"command", command},
        {"payload", payload},
    });
}

json Bridge::dispatch_game_thread(const std::string& command, const json& payload) {
    if (command == "run_generate_sdk") {
        if (!m_export.available()) {
            throw CommandError{
                "CAPABILITY_UNAVAILABLE",
                "GenerateSdk Export Service ABI 1.0 is unavailable",
                m_export.capability_status(),
            };
        }
        auto request = payload;
        const auto* version = m_parameter->version;
        request["_runtime_epoch"] = m_runtime_epoch;
        request["_tdb_fingerprint"] = m_tdb_fingerprint;
        request["_game_id"] =
            version != nullptr && version->game_name != nullptr
                ? version->game_name
                : "unknown";
        request["_reframework_version"] = version_string(version);
        auto result = m_export.invoke(command, request);
        result["runtime_epoch"] = m_runtime_epoch;
        return result;
    }
    if (command == "list_singletons") return list_singletons(payload);
    if (command == "inspect_object") return inspect_object(payload);
    if (command == "invoke_method") return invoke_method(payload);
    if (command == "set_field") return set_field(payload);
    if (command == "validate_access_plan") return validate_access_plan(payload);
    if (command == "install_hook") return m_hooks.install(payload);
    if (command == "remove_hook") {
        return m_hooks.remove(payload.at("hook_ref").get<std::string>());
    }
    if (
        command == "compile_lua_probe"
        || command == "run_lua_probe"
        || command == "cancel_lua_probe"
    ) {
        if (!m_probe.available()) {
            throw CommandError{
                "CAPABILITY_UNAVAILABLE",
                "Probe Service ABI 1.0 is unavailable",
                m_probe.capability_status(),
            };
        }
        auto result = m_probe.invoke(command, payload);
        result["runtime_epoch"] = m_runtime_epoch;
        return result;
    }
    throw CommandError{
        "INVALID_REQUEST",
        "Unknown bridge command",
        {{"command", command}},
    };
}

json Bridge::submit_game_thread(const json& request) {
    if (std::this_thread::get_id() == m_game_thread) {
        return dispatch_game_thread(
            request.at("command").get<std::string>(),
            request.value("payload", json::object()));
    }
    auto pending = std::make_shared<PendingCommand>();
    pending->request = request;
    auto future = pending->result.get_future();
    {
        std::scoped_lock lock{m_queue_mutex};
        if (m_queue.size() >= 64) {
            throw CommandError{
                "BRIDGE_TIMEOUT",
                "Game-thread command queue is full",
                {{"limit", 64}},
            };
        }
        m_queue.push_back(pending);
    }
    if (future.wait_for(kCommandTimeout) != std::future_status::ready) {
        pending->cancelled = true;
        throw CommandError{
            "BRIDGE_TIMEOUT",
            "Game thread did not process the command before the deadline",
        };
    }
    return future.get();
}

json Bridge::runtime_status() {
    auto* tdb = reframework::API::get()->tdb();
    const auto* version = m_parameter->version;
    return {
        {"bridge_version", kBridgeVersion},
        {"protocol", kProtocolVersion},
        {"runtime_epoch", m_runtime_epoch},
        {"game_id", version != nullptr && version->game_name != nullptr
            ? version->game_name
            : "unknown"},
        {"reframework_version", version_string(version)},
        {"tdb", {
            {"fingerprint", m_tdb_fingerprint},
            {"types", tdb != nullptr ? tdb->get_num_types() : 0},
            {"methods", tdb != nullptr ? tdb->get_num_methods() : 0},
            {"fields", tdb != nullptr ? tdb->get_num_fields() : 0},
            {"properties", tdb != nullptr ? tdb->get_num_properties() : 0},
        }},
        {"object_registry_size", m_objects.size()},
        {"active_hooks", m_hooks.size()},
        {"capabilities", {
            {"run_generate_sdk", m_export.capability_status()},
            {"list_singletons", true},
            {"inspect_object", true},
            {"invoke_method", true},
            {"set_field", true},
            {"validate_access_plan", true},
            {"install_hook", true},
            {"remove_hook", true},
            {"isolated_lua_probe", m_probe.capability_status()},
        }},
    };
}

json Bridge::list_singletons(const json& payload) {
    const auto query = optional_string(payload, "type_query");
    const auto limit = std::clamp<std::size_t>(
        payload.value("limit", static_cast<std::size_t>(500)),
        1,
        5000);
    const auto kinds = payload.value("kinds", std::vector<std::string>{"managed", "native"});
    const auto managed_enabled = std::ranges::find(kinds, "managed") != kinds.end();
    const auto native_enabled = std::ranges::find(kinds, "native") != kinds.end();
    json items = json::array();
    if (managed_enabled) {
        for (const auto& singleton : reframework::API::get()->get_managed_singletons()) {
            if (items.size() >= limit) break;
            auto* type = reinterpret_cast<reframework::API::TypeDefinition*>(singleton.t);
            auto* object = reinterpret_cast<reframework::API::ManagedObject*>(singleton.instance);
            const auto name = type != nullptr ? type->get_full_name() : std::string{};
            if (object == nullptr || !contains_case_insensitive(name, query)) continue;
            items.push_back({
                {"kind", "managed_singleton"},
                {"type_name", name},
                {"object_ref", m_objects.put_managed(object, type)},
                {"runtime_epoch", m_runtime_epoch},
                {"lease_seconds", 60},
            });
        }
    }
    if (native_enabled && items.size() < limit) {
        for (const auto& singleton : reframework::API::get()->get_native_singletons()) {
            if (items.size() >= limit) break;
            auto* type = reinterpret_cast<reframework::API::TypeDefinition*>(singleton.t);
            const auto name = singleton.name != nullptr
                ? std::string{singleton.name}
                : type != nullptr ? type->get_full_name() : std::string{};
            if (singleton.instance == nullptr || !contains_case_insensitive(name, query)) continue;
            items.push_back({
                {"kind", "native_singleton"},
                {"type_name", name},
                {"object_ref", m_objects.put_native(singleton.instance, type, name)},
                {"runtime_epoch", m_runtime_epoch},
                {"lease_seconds", 60},
            });
        }
    }
    const auto truncated = items.size() >= limit;
    return {
        {"runtime_epoch", m_runtime_epoch},
        {"items", std::move(items)},
        {"truncated", truncated},
    };
}

json Bridge::inspect_object(const json& payload) {
    const auto root_ref = payload.at("object_ref").get<std::string>();
    const auto depth_limit = std::clamp(payload.value("depth", 1), 0, 4);
    const auto max_nodes = std::clamp<std::size_t>(
        payload.value("max_nodes", static_cast<std::size_t>(500)),
        1,
        5000);
    const auto member_filter = payload.value("member_filter", "");
    const auto include_properties = payload.value("include_properties", false);
    const auto allow_getters = payload.value("allow_getters", false);
    const auto getter_allowlist = payload.value("getter_allowlist", json::array());
    const auto collection_offset = std::clamp<std::size_t>(
        payload.value("collection_offset", static_cast<std::size_t>(0)),
        0,
        100000);
    const auto collection_limit = std::clamp<std::size_t>(
        payload.value("collection_limit", static_cast<std::size_t>(100)),
        1,
        1000);
    const auto max_bytes = std::clamp<std::size_t>(
        payload.value("max_bytes", static_cast<std::size_t>(1024 * 1024)),
        4096,
        8 * 1024 * 1024);
    std::deque<std::pair<std::string, int>> queue{{root_ref, 0}};
    std::unordered_map<std::string, bool> seen;
    json nodes = json::array();
    json edges = json::array();
    std::size_t response_bytes{};
    bool byte_truncated{};
    while (!queue.empty() && nodes.size() < max_nodes) {
        auto [object_ref, depth] = queue.front();
        queue.pop_front();
        if (seen.contains(object_ref)) continue;
        seen.emplace(object_ref, true);
        const auto entry = require_object(object_ref);
        auto node = inspect_entry(
            object_ref,
            entry,
            member_filter,
            collection_offset,
            collection_limit,
            include_properties,
            allow_getters,
            getter_allowlist);
        auto node_bytes = node.dump().size();
        if (response_bytes + node_bytes > max_bytes) {
            if (!nodes.empty()) {
                byte_truncated = true;
                break;
            }
            auto trim_one = [&node]() {
                if (node.contains("collection") && node["collection"].is_object()
                    && node["collection"].contains("items")
                    && !node["collection"]["items"].empty()) {
                    node["collection"]["items"].erase(
                        std::prev(node["collection"]["items"].end()));
                    node["collection"]["truncated"] = true;
                    return true;
                }
                if (node.contains("properties") && !node["properties"].empty()) {
                    node["properties"].erase(std::prev(node["properties"].end()));
                    return true;
                }
                if (node.contains("fields") && !node["fields"].empty()) {
                    node["fields"].erase(std::prev(node["fields"].end()));
                    return true;
                }
                return false;
            };
            while (node_bytes > max_bytes && trim_one()) {
                node_bytes = node.dump().size();
            }
            node["truncated_by_bytes"] = true;
            byte_truncated = true;
        }
        json node_edges = json::array();
        auto follow_value = [&](const json& value, const std::string& kind, const json& member) {
            if (!value.is_object() || !value.contains("object_ref")) return;
            const auto target = value["object_ref"].get<std::string>();
            node_edges.push_back({
                {"source_ref", object_ref},
                {"target_ref", target},
                {"edge_kind", kind},
                {"member", member},
            });
            if (depth < depth_limit) queue.emplace_back(target, depth + 1);
        };
        if (depth < depth_limit) {
            for (const auto& field : node["fields"]) {
                if (field.contains("value")) {
                    follow_value(field["value"], "field", field["name"]);
                }
            }
            for (const auto& property : node["properties"]) {
                if (property.contains("value")) {
                    follow_value(property["value"], "property", property["name"]);
                }
            }
            if (node["collection"].is_object()) {
                for (const auto& item : node["collection"]["items"]) {
                    if (item.contains("key")) {
                        follow_value(item["key"], "collection_key", item.value("index", 0));
                    }
                    if (item.contains("value")) {
                        follow_value(item["value"], "collection_item", item.value("index", 0));
                    }
                }
                if (node["collection"].contains("storage")) {
                    follow_value(
                        node["collection"]["storage"],
                        "collection_storage",
                        node["collection"].value("adapter", "collection"));
                }
            }
        }
        for (auto& edge : node_edges) edges.push_back(std::move(edge));
        response_bytes += node_bytes;
        nodes.push_back(std::move(node));
    }
    json result = {
        {"runtime_epoch", m_runtime_epoch},
        {"root_ref", root_ref},
        {"nodes", std::move(nodes)},
        {"edges", std::move(edges)},
        {"truncated", !queue.empty() || byte_truncated},
        {"properties_included",
            include_properties && allow_getters && !getter_allowlist.empty()},
        {"property_note",
            !include_properties
                ? "Properties were not requested"
                : !allow_getters
                    ? "Getter execution requires allow_getters=true"
                    : getter_allowlist.empty()
                        ? "Getter execution requires a non-empty getter_allowlist"
                        : "Only allowlisted zero-argument getters were invoked"},
        {"response_bytes", response_bytes},
        {"collection_adapters", {
            {"system_array", false},
            {"generic_list_storage", true},
            {"generic_dictionary_storage", true},
            {"native_rsz_field_view", true},
        }},
    };
    result["response_bytes"] = result.dump().size();
    return result;
}

json Bridge::invoke_method(const json& payload) {
    const auto [declaring_type, selector] = split_member_signature(payload.at("member_ref"));
    reframework::API::ManagedObject* object{};
    reframework::API::TypeDefinition* type{};
    if (payload.contains("object_ref") && !payload["object_ref"].is_null()) {
        const auto entry = require_object(payload["object_ref"].get<std::string>());
        object = entry.managed;
        type = entry.type;
        const auto expected = optional_string(payload, "expected_runtime_type");
        if (!expected.empty() && entry.type_name != expected) {
            throw CommandError{
                "VALIDATION_FAILED",
                "Runtime object type does not match expected_runtime_type",
                {{"expected", expected}, {"actual", entry.type_name}},
            };
        }
    } else {
        type = reframework::API::get()->tdb()->find_type(declaring_type);
    }
    if (type == nullptr) {
        throw CommandError{"TYPE_NOT_FOUND", "Declaring type was not found"};
    }
    const auto arguments = payload.value("arguments", json::array());
    auto* method = find_method_exact(type, selector, arguments.size());
    if (method == nullptr) {
        throw CommandError{
            "MEMBER_NOT_FOUND",
            "Method was not found",
            {{"selector", selector}, {"declaring_type", declaring_type}},
        };
    }
    if (!method->is_static() && object == nullptr) {
        throw CommandError{"INVALID_REQUEST", "Instance method requires object_ref"};
    }
    return {
        {"member_signature", payload["member_ref"]["canonical_signature"]},
        {"result", invoke(method, object, arguments)},
    };
}

json Bridge::set_field(const json& payload) {
    const auto entry = require_object(payload.at("object_ref").get<std::string>());
    if (entry.type == nullptr || entry.native == nullptr) {
        throw CommandError{"OBJECT_EXPIRED", "ObjectRef has no readable object"};
    }
    const auto member = split_member_signature(payload.at("member_ref"));
    const auto name = field_name(member.second);
    auto* field = entry.type->find_field(name);
    if (field == nullptr) {
        throw CommandError{"MEMBER_NOT_FOUND", "Field was not found", {{"name", name}}};
    }
    auto* raw = field->get_data_raw(entry.native, entry.type->is_valuetype());
    if (raw == nullptr) {
        throw CommandError{"VALIDATION_FAILED", "Field storage is unavailable"};
    }
    auto* value_type = field->get_type();
    const auto old_value = read_value(raw, value_type);
    if (payload.contains("expected_old_value")
        && !payload["expected_old_value"].is_null()
        && old_value != payload["expected_old_value"]) {
        throw CommandError{
            "VALIDATION_FAILED",
            "Field value changed after approval",
            {{"expected", payload["expected_old_value"]}, {"actual", old_value}},
        };
    }
    write_value(raw, value_type, payload.at("value"));
    return {
        {"object_ref", payload["object_ref"]},
        {"member_signature", payload["member_ref"]["canonical_signature"]},
        {"old_value", old_value},
        {"new_value", read_value(raw, value_type)},
    };
}

json Bridge::validate_access_plan(const json& payload) {
    const auto& plan = payload.at("plan");
    const auto allow_getters = payload.value("allow_getters", false);
    const auto targets = plan.value("targets", std::vector<std::string>{});
    std::unordered_map<std::string, json> values;
    json steps = json::array();
    std::string failed_node;
    for (const auto& node : plan.at("nodes")) {
        const auto node_id = node.at("node_id").get<std::string>();
        try {
            const auto operation = node.at("operation").get<std::string>();
            json value;
            if (operation == "resolve_root") {
                const auto& root = node.at("root");
                const auto kind = root.at("kind").get<std::string>();
                if (kind == "provided_object_ref") {
                    const auto object_ref = root.at("object_ref").get<std::string>();
                    require_object(object_ref);
                    value = {{"object_ref", object_ref}};
                } else if (kind == "managed_singleton") {
                    const auto name = root.at("type_name").get<std::string>();
                    auto* object = reframework::API::get()->get_managed_singleton(name);
                    if (object == nullptr) {
                        throw CommandError{"VALIDATION_FAILED", "Managed singleton is null"};
                    }
                    value = {{"object_ref", m_objects.put_managed(
                        object,
                        object->get_type_definition())}, {"lease_seconds", 60}};
                } else if (kind == "native_singleton") {
                    const auto name = root.at("type_name").get<std::string>();
                    bool found{};
                    for (const auto& singleton :
                         reframework::API::get()->get_native_singletons()) {
                        auto* type = reinterpret_cast<
                            reframework::API::TypeDefinition*>(singleton.t);
                        const auto registered_name = singleton.name != nullptr
                            ? std::string{singleton.name}
                            : type != nullptr ? type->get_full_name() : std::string{};
                        const auto type_name = type != nullptr
                            ? type->get_full_name()
                            : std::string{};
                        if (registered_name != name && type_name != name) continue;
                        if (singleton.instance == nullptr) {
                            throw CommandError{
                                "VALIDATION_FAILED",
                                "Native singleton is null",
                            };
                        }
                        value = {
                            {"object_ref", m_objects.put_native(
                                singleton.instance,
                                type,
                                registered_name)},
                            {"lease_seconds", 60},
                        };
                        found = true;
                        break;
                    }
                    if (!found) {
                        throw CommandError{
                            "TYPE_NOT_FOUND",
                            "Native singleton was not found",
                            {{"type_name", name}},
                        };
                    }
                } else if (kind == "static_type") {
                    const auto name = root.at("type_name").get<std::string>();
                    if (reframework::API::get()->tdb()->find_type(name) == nullptr) {
                        throw CommandError{
                            "TYPE_NOT_FOUND",
                            "Static root type was not found",
                            {{"type_name", name}},
                        };
                    }
                    value = {{"type", name}};
                } else {
                    throw CommandError{
                        "CAPABILITY_UNAVAILABLE",
                        "Live validation for this root kind is unavailable",
                        {{"root_kind", kind}},
                    };
                }
            } else if (operation == "bind_constant") {
                value = node.value("value", json{});
            } else if (operation == "read_field") {
                const auto input = node.at("inputs").at(0).get<std::string>();
                const auto object_ref = values.at(input).at("object_ref").get<std::string>();
                const auto entry = require_object(object_ref);
                const auto member = split_member_signature(node.at("member"));
                if (entry.type == nullptr) {
                    throw CommandError{
                        "TYPE_NOT_FOUND",
                        "Plan receiver has no type information",
                    };
                }
                auto* field = entry.type->find_field(field_name(member.second));
                if (field == nullptr) {
                    throw CommandError{"MEMBER_NOT_FOUND", "Plan field was not found"};
                }
                value = read_field(entry, field).at("value");
            } else if (
                operation == "read_property"
                || operation == "call_method"
                || operation == "call_static") {
                json arguments = json::array();
                for (const auto& argument_node : node.value("arguments", json::array())) {
                    const auto argument_id = argument_node.get<std::string>();
                    if (!values.contains(argument_id)) {
                        throw CommandError{
                            "PLAN_INVALID",
                            "Method argument node has no validated value",
                            {{"argument_node", argument_id}},
                        };
                    }
                    arguments.push_back(values.at(argument_id));
                }
                reframework::API::ManagedObject* object{};
                reframework::API::TypeDefinition* type{};
                const auto member = split_member_signature(node.at("member"));
                if (operation == "call_static") {
                    type = reframework::API::get()->tdb()->find_type(member.first);
                } else {
                    const auto inputs = node.value("inputs", json::array());
                    if (inputs.empty()) {
                        throw CommandError{
                            "PLAN_INVALID",
                            "Instance method/property has no receiver node",
                        };
                    }
                    const auto input = inputs.at(0).get<std::string>();
                    if (!values.contains(input)
                        || !values.at(input).is_object()
                        || !values.at(input).contains("object_ref")) {
                        throw CommandError{
                            "PLAN_INVALID",
                            "Method receiver did not resolve to an ObjectRef",
                            {{"receiver_node", input}},
                        };
                    }
                    const auto entry = require_object(
                        values.at(input).at("object_ref").get<std::string>());
                    object = entry.managed;
                    type = entry.type;
                }
                if (type == nullptr) {
                    throw CommandError{
                        "TYPE_NOT_FOUND",
                        "Method/property declaring type was not found",
                        {{"declaring_type", member.first}},
                    };
                }

                auto selector = member.second;
                auto method_name = selector.substr(0, selector.find('('));
                reframework::API::Method* method{};
                if (operation == "read_property") {
                    const auto property_name = field_name(selector);
                    method_name = "get_" + property_name;
                    method = find_method_by_arity(type, method_name, arguments.size());
                    if (method == nullptr) {
                        method_name = "is_" + property_name;
                        method = find_method_by_arity(type, method_name, arguments.size());
                    }
                } else {
                    method = find_method_exact(type, selector, arguments.size());
                }
                if (method == nullptr) {
                    throw CommandError{
                        "MEMBER_NOT_FOUND",
                        "Plan method/property getter was not found",
                        {
                            {"selector", selector},
                            {"declaring_type", member.first},
                            {"argument_count", arguments.size()},
                        },
                    };
                }
                if (operation == "call_static" && !method->is_static()) {
                    throw CommandError{
                        "VALIDATION_FAILED",
                        "call_static selected a non-static method",
                    };
                }
                if (operation != "call_static" && method->is_static()) {
                    throw CommandError{
                        "VALIDATION_FAILED",
                        "Instance operation selected a static method",
                    };
                }
                if (!method->is_static() && object == nullptr) {
                    throw CommandError{
                        "CAPABILITY_UNAVAILABLE",
                        "Native receivers cannot invoke managed methods",
                    };
                }

                static_cast<void>(encode_arguments(method, arguments));
                const auto is_target =
                    std::ranges::find(targets, node_id) != targets.end();
                const auto safe_getter =
                    arguments.empty()
                    && (method_name.starts_with("get_")
                        || method_name.starts_with("is_"));
                if (is_target) {
                    value = {
                        {"validated_call", true},
                        {"member_signature",
                            node.at("member").at("canonical_signature")},
                        {"argument_count", arguments.size()},
                        {"execution", "dry_run"},
                    };
                } else {
                    if (!allow_getters) {
                        throw CommandError{
                            "POLICY_DENIED",
                            "Intermediate getter execution requires allow_getters=true",
                        };
                    }
                    if (!safe_getter) {
                        throw CommandError{
                            "CAPABILITY_UNAVAILABLE",
                            "Only zero-argument getters may execute during live validation",
                            {{"method", method_name}},
                        };
                    }
                    value = invoke(method, object, arguments);
                }
            } else if (
                operation == "assert_non_null"
                || operation == "assert_type"
                || operation == "assert_range"
                || operation == "cast"
                || operation == "emit") {
                const auto inputs = node.value("inputs", json::array());
                if (inputs.empty()) {
                    throw CommandError{
                        "PLAN_INVALID",
                        "Validation operation has no input",
                    };
                }
                value = values.at(inputs.at(0).get<std::string>());
                if (operation == "assert_non_null" && value.is_null()) {
                    throw CommandError{
                        "VALIDATION_FAILED",
                        "assert_non_null received null",
                    };
                }
            } else {
                throw CommandError{
                    "CAPABILITY_UNAVAILABLE",
                    "Live validation for this AccessPlan operation is unavailable",
                    {{"operation", operation}},
                };
            }
            values[node_id] = value;
            steps.push_back({
                {"node_id", node_id},
                {"status", "valid"},
                {"expected_type", node.value("output_type", "")},
                {"actual_type", value.is_object()
                    ? value.value("type", node.value("output_type", ""))
                    : node.value("output_type", "")},
                {"value_summary", value},
                {"object_ref", value.is_object() ? value.value("object_ref", "") : ""},
            });
        } catch (const CommandError& error) {
            failed_node = node_id;
            steps.push_back({
                {"node_id", node_id},
                {"status", "invalid"},
                {"expected_type", node.value("output_type", "")},
                {"error_code", error.code},
                {"message", error.what()},
            });
            break;
        } catch (const std::exception& error) {
            failed_node = node_id;
            steps.push_back({
                {"node_id", node_id},
                {"status", "invalid"},
                {"expected_type", node.value("output_type", "")},
                {"error_code", "PLAN_INVALID"},
                {"message", error.what()},
            });
            break;
        }
    }
    return {
        {"status", failed_node.empty() ? "valid" : "failed"},
        {"runtime_epoch", m_runtime_epoch},
        {"scene_epoch", nullptr},
        {"save_epoch", nullptr},
        {"steps", std::move(steps)},
        {"failed_node", failed_node.empty() ? json(nullptr) : json(failed_node)},
        {"alternatives", json::array()},
    };
}

json Bridge::inspect_entry(
    const std::string& object_ref,
    const ObjectEntry& entry,
    const std::string& member_filter,
    std::size_t collection_offset,
    std::size_t collection_limit,
    bool include_properties,
    bool allow_getters,
    const json& getter_allowlist) {
    json fields = json::array();
    if (entry.type != nullptr) {
        for (auto* field : entry.type->get_fields()) {
            if (field == nullptr || field->is_static()) continue;
            const auto name = std::string{field->get_name()};
            if (!contains_case_insensitive(name, member_filter)) continue;
            fields.push_back(read_field(entry, field));
            if (fields.size() >= collection_limit) break;
        }
    }
    auto properties = json::array();
    if (include_properties && allow_getters && !getter_allowlist.empty()) {
        properties = inspect_properties(
            entry,
            member_filter,
            collection_limit,
            getter_allowlist);
    }
    return {
        {"object_ref", object_ref},
        {"runtime_epoch", m_runtime_epoch},
        {"lease_seconds", 60},
        {"type", entry.type_name},
        {"kind", entry.managed != nullptr ? "managed" : "native"},
        {"fields", std::move(fields)},
        {"properties", std::move(properties)},
        {"collection", inspect_collection(
            entry,
            collection_offset,
            collection_limit)},
    };
}

reframework::API::Method* Bridge::find_method_by_arity(
    reframework::API::TypeDefinition* type,
    const std::string& name,
    std::size_t arity) {
    if (type == nullptr) return nullptr;
    for (auto* method : type->get_methods()) {
        if (method == nullptr || method->get_name() == nullptr) continue;
        if (name == method->get_name() && method->get_num_params() == arity) {
            return method;
        }
    }
    auto* method = type->find_method(name);
    return method != nullptr && method->get_num_params() == arity ? method : nullptr;
}

reframework::API::Method* Bridge::find_method_exact(
    reframework::API::TypeDefinition* type,
    const std::string& selector,
    std::size_t arity) {
    if (type == nullptr) return nullptr;
    if (selector.find('(') == std::string::npos) {
        return find_method_by_arity(type, selector, arity);
    }
    for (auto* method : type->get_methods()) {
        if (method == nullptr || method->get_name() == nullptr
            || method->get_num_params() != arity) {
            continue;
        }
        std::ostringstream candidate;
        candidate << method->get_name() << '(';
        const auto parameters = method->get_params();
        for (std::size_t index = 0; index < parameters.size(); ++index) {
            if (index > 0) candidate << ", ";
            auto* parameter_type = reinterpret_cast<
                reframework::API::TypeDefinition*>(parameters[index].t);
            candidate << (parameter_type != nullptr
                ? parameter_type->get_full_name()
                : std::string{});
        }
        candidate << ')';
        if (candidate.str() == selector) return method;
    }
    return nullptr;
}

json Bridge::inspect_properties(
    const ObjectEntry& entry,
    const std::string& member_filter,
    std::size_t limit,
    const json& getter_allowlist) {
    json properties = json::array();
    if (entry.managed == nullptr || entry.type == nullptr) return properties;
    auto is_allowed = [&getter_allowlist](const std::string& method_name,
                                          const std::string& property_name) {
        for (const auto& allowed : getter_allowlist) {
            if (!allowed.is_string()) continue;
            const auto value = allowed.get<std::string>();
            if (value == "*" || value == method_name || value == property_name) return true;
        }
        return false;
    };
    for (auto* method : entry.type->get_methods()) {
        if (method == nullptr || method->get_name() == nullptr || method->is_static()
            || method->get_num_params() != 0) {
            continue;
        }
        const auto method_name = std::string{method->get_name()};
        std::string property_name;
        if (method_name.starts_with("get_")) {
            property_name = method_name.substr(4);
        } else if (method_name.starts_with("is_")) {
            property_name = method_name.substr(3);
        } else {
            continue;
        }
        if (!contains_case_insensitive(property_name, member_filter)
            || !is_allowed(method_name, property_name)) {
            continue;
        }
        const std::vector<void*> no_arguments;
        const auto result = method->invoke(entry.managed, no_arguments);
        if (result.exception_thrown) {
            properties.push_back({
                {"name", property_name},
                {"getter", method_name},
                {"error", "Managed getter threw an exception"},
            });
        } else {
            properties.push_back({
                {"name", property_name},
                {"getter", method_name},
                {"type", method->get_return_type() != nullptr
                    ? method->get_return_type()->get_full_name()
                    : ""},
                {"value", return_value(result, method->get_return_type())},
            });
        }
        if (properties.size() >= limit) break;
    }
    return properties;
}

json Bridge::inspect_collection(
    const ObjectEntry& entry,
    std::size_t collection_offset,
    std::size_t collection_limit) {
    if (entry.managed == nullptr || entry.type == nullptr) return nullptr;
    const auto type_name = entry.type->get_full_name();
    auto* tdb = reframework::API::get()->tdb();
    auto* system_array = tdb != nullptr ? tdb->find_type("System.Array") : nullptr;
    if (system_array != nullptr
        && (entry.type == system_array || entry.type->is_derived_from(system_array))) {
        return {
            {"adapter", "system_array"},
            {"supported", false},
            {"offset", collection_offset},
            {"limit", collection_limit},
            {"items", json::array()},
            {"reason",
                "REFramework API 1.0 does not expose a bounds-checked array element "
                "reader; no private array layout is assumed."},
        };
    }

    const auto is_list =
        type_name.find("System.Collections.Generic.List") != std::string::npos;
    const auto is_dictionary =
        type_name.find("System.Collections.Generic.Dictionary") != std::string::npos;
    if (!is_list && !is_dictionary) return nullptr;

    json storage = nullptr;
    json logical_count = nullptr;
    for (auto* field : entry.type->get_fields()) {
        if (field == nullptr || field->is_static() || field->get_name() == nullptr) {
            continue;
        }
        auto name = std::string{field->get_name()};
        std::ranges::transform(name, name.begin(), [](unsigned char character) {
            return static_cast<char>(std::tolower(character));
        });
        const auto storage_name = is_list
            ? name == "_items" || name == "items" || name == "mitems"
            : name == "_entries" || name == "entries" || name == "mentries";
        if (storage_name) {
            storage = read_field(entry, field)["value"];
        }
        if (name == "_size" || name == "size" || name == "msize"
            || name == "_count" || name == "count" || name == "mcount") {
            logical_count = read_field(entry, field)["value"];
        }
    }
    return {
        {"adapter", is_list ? "generic_list_storage" : "generic_dictionary_storage"},
        {"supported", !storage.is_null()},
        {"offset", collection_offset},
        {"limit", collection_limit},
        {"logical_count", std::move(logical_count)},
        {"items", json::array()},
        {"storage", std::move(storage)},
        {"next_offset", nullptr},
        {"truncated", !storage.is_null()},
        {"note",
            "Follow storage.object_ref to inspect the managed backing array. "
            "No private collection or array memory layout is read by the bridge."},
    };
}

json Bridge::read_field(const ObjectEntry& entry, reframework::API::Field* field) {
    auto* type = field->get_type();
    auto* raw = field->get_data_raw(
        entry.native,
        entry.type != nullptr && entry.type->is_valuetype());
    return {
        {"name", field->get_name()},
        {"type", type != nullptr ? type->get_full_name() : ""},
        {"offset_from_base", field->get_offset_from_base()},
        {"value", read_value(raw, type)},
    };
}

json Bridge::read_value(void* raw, reframework::API::TypeDefinition* type) {
    if (raw == nullptr) return nullptr;
    const auto name = type != nullptr ? type->get_full_name() : std::string{};
    if (name == "System.Boolean") return load_value<bool>(raw);
    if (name == "System.Byte") return load_value<std::uint8_t>(raw);
    if (name == "System.SByte") return load_value<std::int8_t>(raw);
    if (name == "System.Int16") return load_value<std::int16_t>(raw);
    if (name == "System.UInt16" || name == "System.Char") {
        return load_value<std::uint16_t>(raw);
    }
    if (name == "System.Int32") return load_value<std::int32_t>(raw);
    if (name == "System.UInt32") return load_value<std::uint32_t>(raw);
    if (name == "System.Int64") return load_value<std::int64_t>(raw);
    if (name == "System.UInt64") return load_value<std::uint64_t>(raw);
    if (name == "System.Single") return load_value<float>(raw);
    if (name == "System.Double") return load_value<double>(raw);
    if (type != nullptr && type->is_enum()) {
        const auto size = type->get_valuetype_size();
        if (size <= 1) return load_value<std::uint8_t>(raw);
        if (size <= 2) return load_value<std::uint16_t>(raw);
        if (size <= 4) return load_value<std::uint32_t>(raw);
        return load_value<std::uint64_t>(raw);
    }
    if (type != nullptr && type->is_valuetype()) {
        return {
            {"type", name},
            {"summary", "value_type"},
            {"size", type->get_valuetype_size()},
        };
    }
    auto* object = load_value<reframework::API::ManagedObject*>(raw);
    if (object == nullptr) return nullptr;
    if (!object->is_managed_object()) {
        return {{"type", name}, {"summary", "non-managed reference"}};
    }
    return {
        {"object_ref", m_objects.put_managed(object, type)},
        {"type", name},
        {"lease_seconds", 60},
    };
}

void Bridge::write_value(
    void* raw,
    reframework::API::TypeDefinition* type,
    const json& value) {
    const auto name = type != nullptr ? type->get_full_name() : std::string{};
    if (name == "System.Boolean") return store_value(raw, value.get<bool>());
    if (name == "System.Byte") return store_value(raw, value.get<std::uint8_t>());
    if (name == "System.SByte") return store_value(raw, value.get<std::int8_t>());
    if (name == "System.Int16") return store_value(raw, value.get<std::int16_t>());
    if (name == "System.UInt16" || name == "System.Char") {
        return store_value(raw, value.get<std::uint16_t>());
    }
    if (name == "System.Int32") return store_value(raw, value.get<std::int32_t>());
    if (name == "System.UInt32") return store_value(raw, value.get<std::uint32_t>());
    if (name == "System.Int64") return store_value(raw, value.get<std::int64_t>());
    if (name == "System.UInt64") return store_value(raw, value.get<std::uint64_t>());
    if (name == "System.Single") return store_value(raw, value.get<float>());
    if (name == "System.Double") return store_value(raw, value.get<double>());
    if (type != nullptr && type->is_enum()) {
        const auto encoded = value.get<std::uint64_t>();
        const auto size = type->get_valuetype_size();
        if (size <= 1) return store_value(raw, static_cast<std::uint8_t>(encoded));
        if (size <= 2) return store_value(raw, static_cast<std::uint16_t>(encoded));
        if (size <= 4) return store_value(raw, static_cast<std::uint32_t>(encoded));
        return store_value(raw, encoded);
    }
    if (type != nullptr && type->is_valuetype()) {
        throw CommandError{
            "CAPABILITY_UNAVAILABLE",
            "Arbitrary value-type field writes are not supported",
            {{"type", name}},
        };
    }
    if (value.is_null()) {
        return store_value<reframework::API::ManagedObject*>(raw, nullptr);
    }
    const auto object_ref = value.is_string()
        ? value.get<std::string>()
        : value.at("object_ref").get<std::string>();
    return store_value(raw, require_object(object_ref).managed);
}

std::vector<void*> Bridge::encode_arguments(
    reframework::API::Method* method,
    const json& arguments) {
    const auto parameters = method->get_params();
    if (!arguments.is_array() || arguments.size() != parameters.size()) {
        throw CommandError{
            "INVALID_REQUEST",
            "Method argument count does not match the selected overload",
            {{"expected", parameters.size()}, {"actual", arguments.size()}},
        };
    }
    std::vector<void*> encoded;
    encoded.reserve(parameters.size());
    for (std::size_t index = 0; index < parameters.size(); ++index) {
        auto* type = reinterpret_cast<reframework::API::TypeDefinition*>(parameters[index].t);
        const auto name = type != nullptr ? type->get_full_name() : std::string{};
        try {
            std::uint64_t bits{};
            if (name == "System.Boolean") {
                bits = arguments[index].get<bool>() ? 1u : 0u;
            } else if (name == "System.Single") {
                bits = std::bit_cast<std::uint32_t>(arguments[index].get<float>());
            } else if (name == "System.Double") {
                bits = std::bit_cast<std::uint64_t>(arguments[index].get<double>());
            } else if (
                name == "System.SByte"
                || name == "System.Int16"
                || name == "System.Int32"
                || name == "System.Int64") {
                bits = static_cast<std::uint64_t>(arguments[index].get<std::int64_t>());
            } else if (type != nullptr && (type->is_primitive() || type->is_enum())) {
                bits = arguments[index].get<std::uint64_t>();
            } else if (arguments[index].is_null()) {
                bits = 0;
            } else {
                const auto object_ref = arguments[index].is_string()
                    ? arguments[index].get<std::string>()
                    : arguments[index].at("object_ref").get<std::string>();
                const auto entry = require_object(object_ref);
                if (entry.managed == nullptr) {
                    throw CommandError{
                        "INVALID_REQUEST",
                        "Managed method argument requires a managed ObjectRef",
                        {{"index", index}, {"object_ref", object_ref}},
                    };
                }
                bits = reinterpret_cast<std::uint64_t>(entry.managed);
            }
            encoded.push_back(reinterpret_cast<void*>(bits));
        } catch (const CommandError&) {
            throw;
        } catch (const std::exception& error) {
            throw CommandError{
                "INVALID_REQUEST",
                "Method argument cannot be encoded for the selected overload",
                {
                    {"index", index},
                    {"expected_type", name},
                    {"error", error.what()},
                },
            };
        }
    }
    return encoded;
}

json Bridge::invoke(
    reframework::API::Method* method,
    reframework::API::ManagedObject* object,
    const json& arguments) {
    const auto encoded = encode_arguments(method, arguments);
    const auto result = method->invoke(object, encoded);
    if (result.exception_thrown) {
        throw CommandError{"VALIDATION_FAILED", "Managed method threw an exception"};
    }
    return return_value(result, method->get_return_type());
}

json Bridge::return_value(
    const reframework::InvokeRet& value,
    reframework::API::TypeDefinition* type) {
    if (type == nullptr) return nullptr;
    const auto name = type->get_full_name();
    if (name == "System.Void") return nullptr;
    if (name == "System.Boolean") return value.byte != 0;
    if (name == "System.Byte") return value.byte;
    if (name == "System.SByte") return static_cast<std::int8_t>(value.byte);
    if (name == "System.Int16") return static_cast<std::int16_t>(value.word);
    if (name == "System.UInt16" || name == "System.Char") return value.word;
    if (name == "System.Int32") return static_cast<std::int32_t>(value.dword);
    if (name == "System.UInt32") return value.dword;
    if (name == "System.Int64") return static_cast<std::int64_t>(value.qword);
    if (name == "System.UInt64") return value.qword;
    if (name == "System.Single") return value.f;
    if (name == "System.Double") return value.d;
    if (type->is_enum()) return value.qword;
    if (type->is_valuetype()) {
        return {{"type", name}, {"summary", "value_type_return"}};
    }
    auto* object = reinterpret_cast<reframework::API::ManagedObject*>(value.ptr);
    if (object == nullptr) return nullptr;
    auto* runtime_type = object->is_managed_object()
        ? object->get_type_definition()
        : type;
    return {
        {"object_ref", m_objects.put_managed(object, runtime_type)},
        {"type", runtime_type != nullptr ? runtime_type->get_full_name() : name},
        {"lease_seconds", 60},
    };
}

ObjectEntry Bridge::require_object(const std::string& object_ref) {
    const auto entry = m_objects.get(object_ref);
    if (!entry || entry->runtime_epoch != m_runtime_epoch) {
        throw CommandError{
            "OBJECT_EXPIRED",
            "ObjectRef is absent, expired, or belongs to another runtime epoch",
            {{"object_ref", object_ref}},
        };
    }
    return *entry;
}

std::pair<std::string, std::string> Bridge::split_member_signature(
    const json& member_ref) {
    const auto signature = member_ref.at("canonical_signature").get<std::string>();
    const auto separator = signature.find("::");
    if (separator == std::string::npos) {
        throw CommandError{"INVALID_REQUEST", "Invalid canonical member signature"};
    }
    auto selector = signature.substr(separator + 2);
    const auto arrow = selector.rfind(" -> ");
    if (arrow != std::string::npos) selector.resize(arrow);
    return {signature.substr(0, separator), selector};
}

std::string Bridge::field_name(const std::string& selector) {
    auto end = selector.find(':');
    if (end == std::string::npos) end = selector.find(" {");
    return selector.substr(0, end);
}

std::wstring Bridge::pipe_name() {
    std::array<wchar_t, 1024> buffer{};
    const auto length = GetEnvironmentVariableW(
        L"REFMCP_PIPE_NAME",
        buffer.data(),
        static_cast<DWORD>(buffer.size()));
    if (length > 0 && length < buffer.size()) {
        return std::wstring{buffer.data(), length};
    }
    return std::wstring{kDefaultPipeName};
}

std::string Bridge::new_runtime_epoch() {
    std::array<unsigned char, 16> bytes{};
    if (BCryptGenRandom(
            nullptr,
            bytes.data(),
            static_cast<ULONG>(bytes.size()),
            BCRYPT_USE_SYSTEM_PREFERRED_RNG) < 0) {
        throw std::runtime_error("BCryptGenRandom failed while creating runtime epoch");
    }
    std::ostringstream stream;
    stream << "runtime:";
    for (const auto byte : bytes) {
        stream << std::hex << std::setw(2) << std::setfill('0')
               << static_cast<unsigned int>(byte);
    }
    return stream.str();
}

std::string Bridge::tdb_fingerprint(reframework::API::TDB* tdb) {
    if (tdb == nullptr) return "sha256:unavailable";
    const auto material = std::to_string(tdb->get_num_types()) + ":"
        + std::to_string(tdb->get_num_methods()) + ":"
        + std::to_string(tdb->get_num_fields()) + ":"
        + std::to_string(tdb->get_num_properties()) + ":"
        + std::to_string(tdb->get_strings_size()) + ":"
        + std::to_string(tdb->get_raw_data_size());
    BCRYPT_ALG_HANDLE algorithm{};
    BCRYPT_HASH_HANDLE hash{};
    DWORD object_size{};
    DWORD result_size{};
    if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) < 0
        || BCryptGetProperty(
            algorithm,
            BCRYPT_OBJECT_LENGTH,
            reinterpret_cast<PUCHAR>(&object_size),
            sizeof(object_size),
            &result_size,
            0) < 0) {
        if (algorithm != nullptr) BCryptCloseAlgorithmProvider(algorithm, 0);
        return "sha256:unavailable";
    }
    std::vector<unsigned char> object(object_size);
    std::array<unsigned char, 32> digest{};
    const auto failed = BCryptCreateHash(
            algorithm,
            &hash,
            object.data(),
            static_cast<ULONG>(object.size()),
            nullptr,
            0,
            0) < 0
        || BCryptHashData(
            hash,
            reinterpret_cast<PUCHAR>(const_cast<char*>(material.data())),
            static_cast<ULONG>(material.size()),
            0) < 0
        || BCryptFinishHash(
            hash,
            digest.data(),
            static_cast<ULONG>(digest.size()),
            0) < 0;
    if (hash != nullptr) BCryptDestroyHash(hash);
    BCryptCloseAlgorithmProvider(algorithm, 0);
    if (failed) return "sha256:unavailable";
    std::ostringstream stream;
    stream << "sha256:";
    for (const auto byte : digest) {
        stream << std::hex << std::setw(2) << std::setfill('0')
               << static_cast<unsigned int>(byte);
    }
    return stream.str();
}

json Bridge::ok_response(
    const std::string& request_id,
    const std::string& runtime_epoch,
    json data) {
    return {
        {"protocol", kProtocolVersion},
        {"request_id", request_id},
        {"runtime_epoch", runtime_epoch},
        {"ok", true},
        {"data", std::move(data)},
        {"error", nullptr},
    };
}

json Bridge::error_response(
    const std::string& request_id,
    const std::string& runtime_epoch,
    const std::string& code,
    const std::string& message,
    json details,
    bool retryable) {
    return {
        {"protocol", kProtocolVersion},
        {"request_id", request_id},
        {"runtime_epoch", runtime_epoch},
        {"ok", false},
        {"data", json::object()},
        {"error", {
            {"code", code},
            {"message", message},
            {"details", std::move(details)},
            {"retryable", retryable},
        }},
    };
}

} // namespace reframework_mcp
