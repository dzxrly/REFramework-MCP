#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define REFMCP_PROBE_SERVICE_ABI_V1 0x00010000u
#define REFMCP_PROBE_SERVICE_SYMBOL_V1 "reframework_get_probe_service_v1"

enum REFMCPProbeCapabilityV1 {
    REFMCP_PROBE_COMPILE = 1u << 0,
    REFMCP_PROBE_ONESHOT = 1u << 1,
    REFMCP_PROBE_WINDOWED = 1u << 2,
    REFMCP_PROBE_INSTRUCTION_LIMIT = 1u << 3,
    REFMCP_PROBE_EMIT = 1u << 4
};

typedef int32_t (*REFMCPProbeInvokeV1)(
    const char* command,
    const char* request_json,
    char* response_json,
    uint32_t response_capacity,
    uint32_t* out_length);

typedef void (*REFMCPProbeTickV1)(void);
typedef void* (*REFMCPProbeObjectResolverV1)(
    void* context,
    const char* opaque_reference);
typedef void (*REFMCPProbeSetObjectResolverV1)(
    void* context,
    REFMCPProbeObjectResolverV1 resolver);

typedef struct REFMCPProbeServiceV1 {
    uint32_t abi_version;
    uint32_t struct_size;
    uint32_t capabilities;
    const char* provider_version;
    REFMCPProbeInvokeV1 invoke;
    REFMCPProbeTickV1 tick;
    REFMCPProbeSetObjectResolverV1 set_object_resolver;
} REFMCPProbeServiceV1;

typedef const REFMCPProbeServiceV1* (*REFMCPGetProbeServiceV1)(void);

#ifdef __cplusplus
}
#endif
