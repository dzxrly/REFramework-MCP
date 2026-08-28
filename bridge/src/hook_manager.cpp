#include "hook_manager.hpp"

#include <Windows.h>
#include <bcrypt.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <utility>

namespace reframework_mcp {
namespace {

std::pair<std::string, std::string> split_signature(const std::string& signature) {
    const auto separator = signature.find("::");
    if (separator == std::string::npos) {
        throw std::runtime_error("MemberRef canonical_signature is missing declaring type");
    }
    auto selector = signature.substr(separator + 2);
    const auto arrow = selector.rfind(" -> ");
    if (arrow != std::string::npos) {
        selector.resize(arrow);
    }
    return {signature.substr(0, separator), selector};
}

} // namespace

HookManager::HookManager(ObjectRegistry& objects, std::string runtime_epoch)
    : m_objects{objects}, m_runtime_epoch{std::move(runtime_epoch)} {
    if (s_instance != nullptr) {
        throw std::runtime_error("Only one HookManager instance is supported");
    }
    s_instance = this;
}

HookManager::~HookManager() {
    for (std::size_t slot = 0; slot < kSlots; ++slot) {
        if (m_entries[slot].active) {
            remove_slot(slot);
        }
    }
    s_instance = nullptr;
}

nlohmann::json HookManager::install(const nlohmann::json& payload) {
    if (payload.contains("transform") && !payload["transform"].is_null()) {
        throw std::runtime_error(
            "Transform hooks are not supported by the public API adapter; use an observation hook");
    }
    const auto& member_ref = payload.at("member_ref");
    const auto signature = member_ref.at("canonical_signature").get<std::string>();
    const auto [declaring_type, selector] = split_signature(signature);
    auto* type = reframework::API::get()->tdb()->find_type(declaring_type);
    if (type == nullptr) {
        throw std::runtime_error("Hook declaring type was not found: " + declaring_type);
    }
    auto* method = type->find_method(selector);
    if (method == nullptr) {
        const auto parenthesis = selector.find('(');
        method = type->find_method(selector.substr(0, parenthesis));
    }
    if (method == nullptr) {
        throw std::runtime_error("Hook method was not found: " + signature);
    }

    std::scoped_lock lock{m_mutex};
    const auto free = std::find_if(
        m_entries.begin(),
        m_entries.end(),
        [](const Entry& entry) { return !entry.active; });
    if (free == m_entries.end()) {
        throw std::runtime_error("Observation hook slot limit reached");
    }
    const auto slot = static_cast<std::size_t>(std::distance(m_entries.begin(), free));
    const auto [pre_callback, post_callback] = callbacks(slot);
    const auto native_hook_id = method->add_hook(pre_callback, post_callback, false);
    if (native_hook_id == 0) {
        throw std::runtime_error("REFramework rejected the hook");
    }

    const auto ttl = std::clamp(payload.value("ttl_seconds", 30.0), 0.1, 3600.0);
    Entry entry;
    entry.active = true;
    entry.slot = slot;
    entry.hook_ref = "hook:" + m_runtime_epoch + ":" + random_suffix();
    entry.signature = signature;
    entry.method = method;
    entry.native_hook_id = native_hook_id;
    entry.phase = payload.value("phase", "both");
    entry.sample_rate = std::clamp(payload.value("sample_rate", 1.0), 0.0, 1.0);
    entry.max_events = std::clamp<std::size_t>(
        payload.value("max_events", static_cast<std::size_t>(1000)),
        1,
        100000);
    entry.expires_at = std::chrono::steady_clock::now()
        + std::chrono::milliseconds{static_cast<long long>(ttl * 1000.0)};
    *free = std::move(entry);

    nlohmann::json parameters = nlohmann::json::array();
    const auto raw_parameters = method->get_params();
    if (!method->is_static()) {
        parameters.push_back({
            {"argument_index", 1},
            {"role", "this"},
            {"type", declaring_type},
        });
    }
    for (std::size_t index = 0; index < raw_parameters.size(); ++index) {
        const auto* parameter_type = reinterpret_cast<reframework::API::TypeDefinition*>(
            raw_parameters[index].t);
        parameters.push_back({
            {"argument_index", method->is_static() ? index + 1 : index + 2},
            {"role", "parameter"},
            {"name", raw_parameters[index].name != nullptr
                ? raw_parameters[index].name
                : ""},
            {"type", parameter_type != nullptr ? parameter_type->get_full_name() : ""},
        });
    }
    return {
        {"hook_ref", free->hook_ref},
        {"runtime_epoch", m_runtime_epoch},
        {"state", "installed"},
        {"member_signature", signature},
        {"phase", free->phase},
        {"ttl_seconds", ttl},
        {"argument_layout", std::move(parameters)},
        {"event_resource", "reframework://hooks/" + free->hook_ref + "/events"},
    };
}

nlohmann::json HookManager::remove(const std::string& hook_ref) {
    std::scoped_lock lock{m_mutex};
    const auto slot = slot_for_ref(hook_ref);
    if (!slot) {
        const auto archived = m_archived.find(hook_ref);
        if (archived != m_archived.end()) {
            auto result = archived->second;
            result["already_absent"] = true;
            return result;
        }
        return {
            {"hook_ref", hook_ref},
            {"runtime_epoch", m_runtime_epoch},
            {"state", "removed"},
            {"already_absent", true},
            {"event_count", 0},
        };
    }
    return remove_slot(*slot);
}

nlohmann::json HookManager::events(const std::string& hook_ref) const {
    std::scoped_lock lock{m_mutex};
    const auto slot = slot_for_ref(hook_ref);
    if (!slot) {
        const auto archived = m_archived.find(hook_ref);
        if (archived != m_archived.end()) {
            return archived->second;
        }
        return {
            {"hook_ref", hook_ref},
            {"runtime_epoch", m_runtime_epoch},
            {"state", "not_found"},
            {"events", nlohmann::json::array()},
        };
    }
    const auto& entry = m_entries[*slot];
    return {
        {"hook_ref", hook_ref},
        {"runtime_epoch", m_runtime_epoch},
        {"state", entry.active ? "installed" : "removed"},
        {"member_signature", entry.signature},
        {"calls", entry.calls},
        {"dropped", entry.dropped},
        {"events", entry.captured},
    };
}

std::optional<std::string> HookManager::resolve_object_ref(
    const std::string& hook_ref,
    const std::string& selector) const {
    std::scoped_lock lock{m_mutex};
    const auto slot = slot_for_ref(hook_ref);
    if (!slot) return std::nullopt;
    const auto& entry = m_entries[*slot];
    for (auto event = entry.captured.rbegin(); event != entry.captured.rend(); ++event) {
        if (selector == "return") {
            const auto value = event->find("return_value");
            if (
                value != event->end()
                && value->is_object()
                && value->contains("object_ref")
            ) {
                return value->at("object_ref").get<std::string>();
            }
            continue;
        }
        if (!event->contains("arguments") || !event->at("arguments").is_array()) {
            continue;
        }
        int requested_index{};
        if (selector == "this") {
            if (entry.method == nullptr || entry.method->is_static()) continue;
            requested_index = 0;
        } else if (selector.starts_with("argument:")) {
            try {
                const auto logical = std::stoi(selector.substr(9));
                requested_index = logical
                    + (entry.method != nullptr && !entry.method->is_static() ? 1 : 0);
            } catch (...) {
                return std::nullopt;
            }
        } else {
            return std::nullopt;
        }
        for (const auto& argument : event->at("arguments")) {
            if (argument.value("index", -1) != requested_index) continue;
            const auto value = argument.find("value");
            if (
                value != argument.end()
                && value->is_object()
                && value->contains("object_ref")
            ) {
                return value->at("object_ref").get<std::string>();
            }
        }
    }
    return std::nullopt;
}

void HookManager::clear_expired() {
    std::scoped_lock lock{m_mutex};
    const auto now = std::chrono::steady_clock::now();
    for (std::size_t slot = 0; slot < kSlots; ++slot) {
        if (m_entries[slot].active && m_entries[slot].expires_at <= now) {
            remove_slot(slot);
        }
    }
}

std::size_t HookManager::size() const {
    std::scoped_lock lock{m_mutex};
    return static_cast<std::size_t>(std::count_if(
        m_entries.begin(),
        m_entries.end(),
        [](const Entry& entry) { return entry.active; }));
}

template <std::size_t Slot>
int HookManager::pre(
    int argc,
    void** argv,
    REFrameworkTypeDefinitionHandle* argument_types,
    unsigned long long) noexcept {
    if (s_instance == nullptr) {
        return REFRAMEWORK_HOOK_CALL_ORIGINAL;
    }
    return s_instance->on_pre(Slot, argc, argv, argument_types);
}

template <std::size_t Slot>
void HookManager::post(
    void** return_value,
    REFrameworkTypeDefinitionHandle return_type,
    unsigned long long) noexcept {
    if (s_instance != nullptr) {
        s_instance->on_post(Slot, return_value, return_type);
    }
}

std::pair<REFPreHookFn, REFPostHookFn> HookManager::callbacks(std::size_t slot) {
    switch (slot) {
    case 0: return {&pre<0>, &post<0>};
    case 1: return {&pre<1>, &post<1>};
    case 2: return {&pre<2>, &post<2>};
    case 3: return {&pre<3>, &post<3>};
    case 4: return {&pre<4>, &post<4>};
    case 5: return {&pre<5>, &post<5>};
    case 6: return {&pre<6>, &post<6>};
    case 7: return {&pre<7>, &post<7>};
    default: throw std::runtime_error("Invalid hook slot");
    }
}

int HookManager::on_pre(
    std::size_t slot,
    int argc,
    void** argv,
    REFrameworkTypeDefinitionHandle* argument_types) noexcept {
    try {
        std::scoped_lock lock{m_mutex};
        auto& entry = m_entries.at(slot);
        if (!entry.active) {
            return REFRAMEWORK_HOOK_CALL_ORIGINAL;
        }
        ++entry.calls;
        if (entry.phase == "post"
            || static_cast<double>((entry.calls * 2654435761u) % 10000u) / 10000.0
                > entry.sample_rate) {
            return REFRAMEWORK_HOOK_CALL_ORIGINAL;
        }
        if (entry.captured.size() >= entry.max_events) {
            ++entry.dropped;
            return REFRAMEWORK_HOOK_CALL_ORIGINAL;
        }
        nlohmann::json arguments = nlohmann::json::array();
        for (int index = 0; index < argc; ++index) {
            auto* type = argument_types != nullptr
                ? reinterpret_cast<reframework::API::TypeDefinition*>(argument_types[index])
                : nullptr;
            arguments.push_back({
                {"index", index},
                {"type", type != nullptr ? type->get_full_name() : ""},
                {"value", argument_value(argv != nullptr ? argv[index] : nullptr, type)},
            });
        }
        entry.captured.push_back({
            {"timestamp", utc_timestamp()},
            {"phase", "pre"},
            {"member_signature", entry.signature},
            {"arguments", std::move(arguments)},
        });
    } catch (...) {
    }
    return REFRAMEWORK_HOOK_CALL_ORIGINAL;
}

void HookManager::on_post(
    std::size_t slot,
    void** return_value,
    REFrameworkTypeDefinitionHandle return_type) noexcept {
    try {
        std::scoped_lock lock{m_mutex};
        auto& entry = m_entries.at(slot);
        if (!entry.active || entry.phase == "pre") {
            return;
        }
        if (entry.captured.size() >= entry.max_events) {
            ++entry.dropped;
            return;
        }
        auto* type = reinterpret_cast<reframework::API::TypeDefinition*>(return_type);
        entry.captured.push_back({
            {"timestamp", utc_timestamp()},
            {"phase", "post"},
            {"member_signature", entry.signature},
            {"return_type", type != nullptr ? type->get_full_name() : ""},
            {"return_value", argument_value(
                return_value != nullptr ? *return_value : nullptr,
                type)},
        });
    } catch (...) {
    }
}

nlohmann::json HookManager::argument_value(
    void* value,
    reframework::API::TypeDefinition* type) {
    if (value == nullptr) {
        return nullptr;
    }
    if (type == nullptr) {
        return {{"summary", "opaque"}};
    }
    const auto name = type->get_full_name();
    if (type->is_primitive() || type->is_enum() || type->is_valuetype()) {
        return {{"summary", "value"}, {"type", name}};
    }
    auto* object = reinterpret_cast<reframework::API::ManagedObject*>(value);
    if (!object->is_managed_object()) {
        return {{"summary", "non-managed reference"}, {"type", name}};
    }
    return {
        {"object_ref", m_objects.put_managed(object, type, std::chrono::seconds{30})},
        {"type", name},
    };
}

std::string HookManager::random_suffix() {
    std::array<unsigned char, 12> bytes{};
    if (BCryptGenRandom(
            nullptr,
            bytes.data(),
            static_cast<ULONG>(bytes.size()),
            BCRYPT_USE_SYSTEM_PREFERRED_RNG) < 0) {
        throw std::runtime_error("BCryptGenRandom failed while creating HookRef");
    }
    std::ostringstream stream;
    for (const auto byte : bytes) {
        stream << std::hex << std::setw(2) << std::setfill('0')
               << static_cast<unsigned int>(byte);
    }
    return stream.str();
}

std::string HookManager::utc_timestamp() {
    const auto now = std::chrono::system_clock::now();
    const auto seconds = std::chrono::time_point_cast<std::chrono::seconds>(now);
    const auto milliseconds = std::chrono::duration_cast<std::chrono::milliseconds>(
        now - seconds).count();
    const auto time = std::chrono::system_clock::to_time_t(now);
    std::tm utc{};
    gmtime_s(&utc, &time);
    std::ostringstream stream;
    stream << std::put_time(&utc, "%Y-%m-%dT%H:%M:%S")
           << '.' << std::setw(3) << std::setfill('0') << milliseconds << 'Z';
    return stream.str();
}

std::optional<std::size_t> HookManager::slot_for_ref(
    const std::string& hook_ref) const {
    for (std::size_t slot = 0; slot < kSlots; ++slot) {
        if (m_entries[slot].active && m_entries[slot].hook_ref == hook_ref) {
            return slot;
        }
    }
    return std::nullopt;
}

nlohmann::json HookManager::remove_slot(std::size_t slot) {
    auto entry = std::move(m_entries.at(slot));
    m_entries[slot] = {};
    if (entry.active && entry.method != nullptr && entry.native_hook_id != 0) {
        entry.method->remove_hook(entry.native_hook_id);
    }
    nlohmann::json result{
        {"hook_ref", entry.hook_ref},
        {"runtime_epoch", m_runtime_epoch},
        {"state", "removed"},
        {"already_absent", !entry.active},
        {"calls", entry.calls},
        {"dropped", entry.dropped},
        {"event_count", entry.captured.size()},
        {"events", entry.captured},
    };
    if (!entry.hook_ref.empty()) {
        m_archived[entry.hook_ref] = result;
        m_archive_order.push_back(entry.hook_ref);
        while (m_archive_order.size() > 32) {
            m_archived.erase(m_archive_order.front());
            m_archive_order.pop_front();
        }
    }
    return result;
}

} // namespace reframework_mcp
