#pragma once

#include <Windows.h>

#include <nlohmann/json.hpp>

#include <string>

#include <reframework_mcp/export_service_v1.h>

namespace reframework_mcp {

class ExportAdapter {
public:
    explicit ExportAdapter(HMODULE reframework_module);

    [[nodiscard]] bool available() const noexcept;
    [[nodiscard]] nlohmann::json capability_status() const;
    nlohmann::json invoke(const std::string& command, const nlohmann::json& payload) const;

private:
    const REFMCPExportServiceV1* m_service{};
    std::string m_error;
};

} // namespace reframework_mcp
