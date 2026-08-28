#pragma once

#include <reframework/API.hpp>

#include <chrono>
#include <cstddef>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_map>

namespace reframework_mcp {

struct ObjectEntry {
    reframework::API::ManagedObject* managed{};
    void* native{};
    reframework::API::TypeDefinition* type{};
    std::string type_name;
    std::string runtime_epoch;
    std::chrono::steady_clock::time_point expires_at;
};

class ObjectRegistry {
public:
    explicit ObjectRegistry(std::string runtime_epoch);
    ~ObjectRegistry();

    ObjectRegistry(const ObjectRegistry&) = delete;
    ObjectRegistry& operator=(const ObjectRegistry&) = delete;

    std::string put_managed(
        reframework::API::ManagedObject* object,
        reframework::API::TypeDefinition* type,
        std::chrono::seconds ttl = std::chrono::seconds{60});
    std::string put_native(
        void* object,
        reframework::API::TypeDefinition* type,
        std::string type_name,
        std::chrono::seconds ttl = std::chrono::seconds{60});
    std::optional<ObjectEntry> get(const std::string& object_ref);
    void erase(const std::string& object_ref);
    std::size_t clear_expired();
    [[nodiscard]] std::size_t size() const;

private:
    std::string next_ref() const;
    static std::string type_name(reframework::API::TypeDefinition* type);

    std::string m_runtime_epoch;
    mutable std::mutex m_mutex;
    std::unordered_map<std::string, ObjectEntry> m_entries;
};

} // namespace reframework_mcp
