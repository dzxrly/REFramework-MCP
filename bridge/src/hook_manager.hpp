#pragma once

#include "object_registry.hpp"

#include <reframework/API.hpp>

#include <nlohmann/json.hpp>

#include <array>
#include <chrono>
#include <cstddef>
#include <deque>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_map>

namespace reframework_mcp {

class HookManager {
public:
    HookManager(ObjectRegistry& objects, std::string runtime_epoch);
    ~HookManager();

    HookManager(const HookManager&) = delete;
    HookManager& operator=(const HookManager&) = delete;

    nlohmann::json install(const nlohmann::json& payload);
    nlohmann::json remove(const std::string& hook_ref);
    nlohmann::json events(const std::string& hook_ref) const;
    std::optional<std::string> resolve_object_ref(
        const std::string& hook_ref,
        const std::string& selector) const;
    void clear_expired();
    [[nodiscard]] std::size_t size() const;

private:
    static constexpr std::size_t kSlots = 8;

    struct Entry {
        bool active{};
        std::size_t slot{};
        std::string hook_ref;
        std::string signature;
        reframework::API::Method* method{};
        unsigned int native_hook_id{};
        std::string phase{"both"};
        double sample_rate{1.0};
        std::size_t max_events{1000};
        std::size_t calls{};
        std::size_t dropped{};
        std::chrono::steady_clock::time_point expires_at;
        std::deque<nlohmann::json> captured;
    };

    template <std::size_t Slot>
    static int pre(
        int argc,
        void** argv,
        REFrameworkTypeDefinitionHandle* argument_types,
        unsigned long long return_address) noexcept;

    template <std::size_t Slot>
    static void post(
        void** return_value,
        REFrameworkTypeDefinitionHandle return_type,
        unsigned long long return_address) noexcept;

    static std::pair<REFPreHookFn, REFPostHookFn> callbacks(std::size_t slot);
    int on_pre(
        std::size_t slot,
        int argc,
        void** argv,
        REFrameworkTypeDefinitionHandle* argument_types) noexcept;
    void on_post(
        std::size_t slot,
        void** return_value,
        REFrameworkTypeDefinitionHandle return_type) noexcept;
    nlohmann::json argument_value(
        void* value,
        reframework::API::TypeDefinition* type);
    static std::string random_suffix();
    static std::string utc_timestamp();
    std::optional<std::size_t> slot_for_ref(const std::string& hook_ref) const;
    nlohmann::json remove_slot(std::size_t slot);

    ObjectRegistry& m_objects;
    std::string m_runtime_epoch;
    mutable std::recursive_mutex m_mutex;
    std::array<Entry, kSlots> m_entries{};
    std::unordered_map<std::string, nlohmann::json> m_archived;
    std::deque<std::string> m_archive_order;
    static inline HookManager* s_instance{};
};

} // namespace reframework_mcp
