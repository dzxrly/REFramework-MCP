#include "export_adapter.hpp"

#include <algorithm>
#include <stdexcept>
#include <vector>

namespace reframework_mcp {

ExportAdapter::ExportAdapter(HMODULE reframework_module) {
    if (reframework_module == nullptr) {
        m_error = "REFramework module handle is null";
        return;
    }
    const auto getter = reinterpret_cast<REFMCPGetExportServiceV1>(
        GetProcAddress(reframework_module, REFMCP_EXPORT_SERVICE_SYMBOL_V1));
    if (getter == nullptr) {
        m_error = "GenerateSdk Export Service ABI 1.0 is not exported by this REFramework build";
        return;
    }
    m_service = getter();
    if (m_service == nullptr) {
        m_error = "GenerateSdk Export Service returned null";
        return;
    }
    if (m_service->abi_version != REFMCP_EXPORT_SERVICE_ABI_V1) {
        m_error = "GenerateSdk Export Service ABI is incompatible";
        m_service = nullptr;
        return;
    }
    if (m_service->struct_size < sizeof(REFMCPExportServiceV1) || m_service->invoke == nullptr) {
        m_error = "GenerateSdk Export Service table is incomplete";
        m_service = nullptr;
    }
}

bool ExportAdapter::available() const noexcept {
    return m_service != nullptr;
}

nlohmann::json ExportAdapter::capability_status() const {
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
        {"json_only", (m_service->capabilities & REFMCP_EXPORT_JSON) != 0},
        {"sdk_and_json", (m_service->capabilities & REFMCP_EXPORT_SDK) != 0},
        {"progress", (m_service->capabilities & REFMCP_EXPORT_PROGRESS) != 0},
        {"cancel", (m_service->capabilities & REFMCP_EXPORT_CANCEL) != 0},
    };
}

nlohmann::json ExportAdapter::invoke(
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
            throw std::runtime_error("GenerateSdk Export Service requested an invalid response size");
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
        throw std::runtime_error("GenerateSdk Export Service returned an invalid response length");
    }
    response[length] = '\0';
    nlohmann::json decoded;
    try {
        decoded = nlohmann::json::parse(response.data(), response.data() + length);
    } catch (const std::exception& error) {
        throw std::runtime_error(
            std::string{"GenerateSdk Export Service returned invalid JSON: "} + error.what());
    }
    if (result < 0) {
        const auto message = decoded.value("message", "GenerateSdk Export Service failed");
        throw std::runtime_error(message);
    }
    return decoded;
}

} // namespace reframework_mcp
