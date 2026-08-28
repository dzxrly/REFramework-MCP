#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define REFMCP_EXPORT_SERVICE_ABI_V1 0x00010000u
#define REFMCP_EXPORT_SERVICE_SYMBOL_V1 "reframework_get_export_service_v1"

enum REFMCPExportCapabilityV1 {
    REFMCP_EXPORT_JSON = 1u << 0,
    REFMCP_EXPORT_SDK = 1u << 1,
    REFMCP_EXPORT_PROGRESS = 1u << 2,
    REFMCP_EXPORT_CANCEL = 1u << 3
};

/*
 * The JSON request/response boundary intentionally keeps REFramework-internal
 * C++ types out of the plugin ABI. The service owns all returned state and
 * copies UTF-8 JSON into the caller-provided buffer.
 *
 * Return values:
 *   0  success
 *   1  output buffer too small; out_length contains required bytes
 *  <0  service error; response contains a structured error when possible
 */
typedef int32_t (*REFMCPExportInvokeV1)(
    const char* command,
    const char* request_json,
    char* response_json,
    uint32_t response_capacity,
    uint32_t* out_length);

typedef struct REFMCPExportServiceV1 {
    uint32_t abi_version;
    uint32_t struct_size;
    uint32_t capabilities;
    const char* provider_version;
    REFMCPExportInvokeV1 invoke;
} REFMCPExportServiceV1;

typedef const REFMCPExportServiceV1* (*REFMCPGetExportServiceV1)(void);

#ifdef __cplusplus
}
#endif
