#pragma once

#include <cstdint>

#define REFMCP_EXPORT_SERVICE_ABI_V1 0x00010000u
#define REFMCP_EXPORT_SERVICE_SYMBOL_V1 "reframework_get_export_service_v1"

enum REFMCPExportCapabilityV1 : std::uint32_t {
    REFMCP_EXPORT_JSON = 1u << 0,
    REFMCP_EXPORT_SDK = 1u << 1,
    REFMCP_EXPORT_PROGRESS = 1u << 2,
    REFMCP_EXPORT_CANCEL = 1u << 3
};

using REFMCPExportInvokeV1 = std::int32_t (*)(
    const char* command,
    const char* request_json,
    char* response_json,
    std::uint32_t response_capacity,
    std::uint32_t* out_length);

struct REFMCPExportServiceV1 {
    std::uint32_t abi_version;
    std::uint32_t struct_size;
    std::uint32_t capabilities;
    const char* provider_version;
    REFMCPExportInvokeV1 invoke;
};

extern "C" __declspec(dllexport) const REFMCPExportServiceV1*
reframework_get_export_service_v1();
