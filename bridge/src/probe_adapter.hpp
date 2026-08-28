#pragma once

#include <Windows.h>

#include <nlohmann/json.hpp>

#include <string>

#include <reframework_mcp/probe_service_v1.h>

#include "hook_manager.hpp"
#include "object_registry.hpp"

namespace reframework_mcp {

class ProbeAdapter {
public:
    explicit ProbeAdapter(HMODULE reframework_module);
    ~ProbeAdapter();

    [[nodiscard]] bool available() const noexcept;
    [[nodiscard]] nlohmann::json capability_status() const;
    nlohmann::json invoke(const std::string& command, const nlohmann::json& payload) const;
    void tick() const noexcept;
    void bind_object_registry(ObjectRegistry& objects, HookManager& hooks);

private:
    static void* resolve_object(void* context, const char* opaque_reference) noexcept;

    const REFMCPProbeServiceV1* m_service{};
    ObjectRegistry* m_objects{};
    HookManager* m_hooks{};
    std::string m_error;
};

} // namespace reframework_mcp
