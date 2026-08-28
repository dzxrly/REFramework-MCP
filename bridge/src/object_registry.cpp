#include "object_registry.hpp"

#include <Windows.h>
#include <bcrypt.h>

#include <array>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <utility>
#include <vector>

namespace reframework_mcp {

ObjectRegistry::ObjectRegistry(std::string runtime_epoch)
    : m_runtime_epoch{std::move(runtime_epoch)} {
}

ObjectRegistry::~ObjectRegistry() {
    std::vector<reframework::API::ManagedObject*> managed;
    {
        std::scoped_lock lock{m_mutex};
        for (const auto& [_, entry] : m_entries) {
            if (entry.managed != nullptr) {
                managed.push_back(entry.managed);
            }
        }
        m_entries.clear();
    }
    for (auto* object : managed) {
        object->release();
    }
}

std::string ObjectRegistry::put_managed(
    reframework::API::ManagedObject* object,
    reframework::API::TypeDefinition* type,
    std::chrono::seconds ttl) {
    if (object == nullptr) {
        return {};
    }
    object->add_ref();
    const auto object_ref = next_ref();
    ObjectEntry entry{
        .managed = object,
        .native = object,
        .type = type != nullptr ? type : object->get_type_definition(),
        .type_name = type_name(type != nullptr ? type : object->get_type_definition()),
        .runtime_epoch = m_runtime_epoch,
        .expires_at = std::chrono::steady_clock::now() + ttl,
    };
    std::scoped_lock lock{m_mutex};
    m_entries.emplace(object_ref, std::move(entry));
    return object_ref;
}

std::string ObjectRegistry::put_native(
    void* object,
    reframework::API::TypeDefinition* type,
    std::string name,
    std::chrono::seconds ttl) {
    if (object == nullptr) {
        return {};
    }
    const auto object_ref = next_ref();
    ObjectEntry entry{
        .managed = nullptr,
        .native = object,
        .type = type,
        .type_name = name.empty() ? type_name(type) : std::move(name),
        .runtime_epoch = m_runtime_epoch,
        .expires_at = std::chrono::steady_clock::now() + ttl,
    };
    std::scoped_lock lock{m_mutex};
    m_entries.emplace(object_ref, std::move(entry));
    return object_ref;
}

std::optional<ObjectEntry> ObjectRegistry::get(const std::string& object_ref) {
    std::scoped_lock lock{m_mutex};
    const auto found = m_entries.find(object_ref);
    if (found == m_entries.end()) {
        return std::nullopt;
    }
    if (found->second.expires_at <= std::chrono::steady_clock::now()) {
        return std::nullopt;
    }
    found->second.expires_at = std::chrono::steady_clock::now() + std::chrono::seconds{60};
    return found->second;
}

void ObjectRegistry::erase(const std::string& object_ref) {
    reframework::API::ManagedObject* managed{};
    {
        std::scoped_lock lock{m_mutex};
        const auto found = m_entries.find(object_ref);
        if (found == m_entries.end()) {
            return;
        }
        managed = found->second.managed;
        m_entries.erase(found);
    }
    if (managed != nullptr) {
        managed->release();
    }
}

std::size_t ObjectRegistry::clear_expired() {
    const auto now = std::chrono::steady_clock::now();
    std::vector<reframework::API::ManagedObject*> managed;
    {
        std::scoped_lock lock{m_mutex};
        for (auto it = m_entries.begin(); it != m_entries.end();) {
            if (it->second.expires_at > now) {
                ++it;
                continue;
            }
            if (it->second.managed != nullptr) {
                managed.push_back(it->second.managed);
            }
            it = m_entries.erase(it);
        }
    }
    for (auto* object : managed) {
        object->release();
    }
    return managed.size();
}

std::size_t ObjectRegistry::size() const {
    std::scoped_lock lock{m_mutex};
    return m_entries.size();
}

std::string ObjectRegistry::next_ref() const {
    std::array<unsigned char, 16> bytes{};
    const auto status = BCryptGenRandom(
        nullptr,
        bytes.data(),
        static_cast<ULONG>(bytes.size()),
        BCRYPT_USE_SYSTEM_PREFERRED_RNG);
    if (status < 0) {
        throw std::runtime_error("BCryptGenRandom failed while creating ObjectRef");
    }
    std::ostringstream stream;
    stream << "obj:";
    for (const auto byte : bytes) {
        stream << std::hex << std::setw(2) << std::setfill('0')
               << static_cast<unsigned int>(byte);
    }
    return stream.str();
}

std::string ObjectRegistry::type_name(reframework::API::TypeDefinition* type) {
    return type != nullptr ? type->get_full_name() : std::string{};
}

} // namespace reframework_mcp
