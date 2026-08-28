#include "probe_adapter.hpp"

#include <stdexcept>
#include <vector>

namespace reframework_mcp {

ProbeAdapter::ProbeAdapter(HMODULE reframework_module) {
    if (reframework_module == nullptr) {
        m_error = "REFramework module handle is null";
        return;
    }
    const auto getter = reinterpret_cast<REFMCPGetProbeServiceV1>(
        GetProcAddress(reframework_module, REFMCP_PROBE_SERVICE_SYMBOL_V1));
    if (getter == nullptr) {
        m_error = "Probe Service ABI 1.0 is not exported by this REFramework build";
        return;
    }
    m_service = getter();
    if (m_service == nullptr) {
        m_error = "Probe Service returned null";
        return;
    }
    if (m_service->abi_version != REFMCP_PROBE_SERVICE_ABI_V1) {
        m_error = "Probe Service ABI is incompatible";
        m_service = nullptr;
        return;
    }
    if (
        m_service->struct_size < sizeof(REFMCPProbeServiceV1)
        || m_service->invoke == nullptr
        || m_service->tick == nullptr
        || m_service->set_object_resolver == nullptr
    ) {
        m_error = "Probe Service table is incomplete";
        m_service = nullptr;
    }
}

ProbeAdapter::~ProbeAdapter() {
    if (available()) {
        try {
            invoke("shutdown_probe_service", nlohmann::json::object());
        } catch (...) {
        }
        m_service->set_object_resolver(nullptr, nullptr);
    }
}

bool ProbeAdapter::available() const noexcept {
    return m_service != nullptr;
}

nlohmann::json ProbeAdapter::capability_status() const {
    if (!available()) {
        return {
            {"available", false},
            {"abi", "1.0"},
            {"reason", m_error},
        };
    }
    return {
        {"available", true},
        {"abi", "1.0"},
        {"provider_version", m_service->provider_version != nullptr
            ? m_service->provider_version
            : "unknown"},
        {"compile", (m_service->capabilities & REFMCP_PROBE_COMPILE) != 0},
        {"oneshot", (m_service->capabilities & REFMCP_PROBE_ONESHOT) != 0},
        {"windowed", (m_service->capabilities & REFMCP_PROBE_WINDOWED) != 0},
        {"instruction_limit",
            (m_service->capabilities & REFMCP_PROBE_INSTRUCTION_LIMIT) != 0},
        {"emit", (m_service->capabilities & REFMCP_PROBE_EMIT) != 0},
        {"object_ref_injection", true},
    };
}

nlohmann::json ProbeAdapter::invoke(
    const std::string& command,
    const nlohmann::json& payload) const {
    if (!available()) {
        throw std::runtime_error(m_error);
    }
    const auto request = payload.dump();
    std::vector<char> response(64u * 1024u);
    uint32_t length{};
    auto result = m_service->invoke(
        command.c_str(),
        request.c_str(),
        response.data(),
        static_cast<uint32_t>(response.size()),
        &length);
    if (result == 1) {
        constexpr uint32_t max_response = 16u * 1024u * 1024u;
        if (length == 0 || length > max_response) {
            throw std::runtime_error("Probe Service requested an invalid response size");
        }
        response.resize(length + 1u);
        result = m_service->invoke(
            command.c_str(),
            request.c_str(),
            response.data(),
            static_cast<uint32_t>(response.size()),
            &length);
    }
    if (length >= response.size()) {
        throw std::runtime_error("Probe Service returned an invalid response length");
    }
    response[length] = '\0';
    nlohmann::json decoded;
    try {
        decoded = nlohmann::json::parse(response.data(), response.data() + length);
    } catch (const std::exception& error) {
        throw std::runtime_error(
            std::string{"Probe Service returned invalid JSON: "} + error.what());
    }
    if (result < 0) {
        throw std::runtime_error(decoded.value("message", "Probe Service failed"));
    }
    return decoded;
}

void ProbeAdapter::tick() const noexcept {
    if (!available()) return;
    try {
        m_service->tick();
    } catch (...) {
    }
}

void ProbeAdapter::bind_object_registry(
    ObjectRegistry& objects,
    HookManager& hooks) {
    m_objects = &objects;
    m_hooks = &hooks;
    if (available()) {
        m_service->set_object_resolver(this, &ProbeAdapter::resolve_object);
    }
}

void* ProbeAdapter::resolve_object(
    void* context,
    const char* opaque_reference) noexcept {
    try {
        auto* adapter = static_cast<ProbeAdapter*>(context);
        if (
            adapter == nullptr
            || adapter->m_objects == nullptr
            || opaque_reference == nullptr
        ) {
            return nullptr;
        }
        std::string reference{opaque_reference};
        const auto separator = reference.find('|');
        if (separator != std::string::npos) {
            if (adapter->m_hooks == nullptr) return nullptr;
            const auto hook_ref = reference.substr(0, separator);
            const auto selector = reference.substr(separator + 1);
            const auto resolved = adapter->m_hooks->resolve_object_ref(
                hook_ref,
                selector);
            if (!resolved) return nullptr;
            reference = *resolved;
        }
        const auto entry = adapter->m_objects->get(reference);
        if (!entry || entry->managed == nullptr) return nullptr;
        return entry->managed;
    } catch (...) {
        return nullptr;
    }
}

} // namespace reframework_mcp
