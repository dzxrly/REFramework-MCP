#pragma once

#include <cstdint>

#define REFMCP_PROBE_SERVICE_ABI_V1 0x00010000u
#define REFMCP_PROBE_SERVICE_SYMBOL_V1 "reframework_get_probe_service_v1"

enum REFMCPProbeCapabilityV1 : std::uint32_t {
    REFMCP_PROBE_COMPILE = 1u << 0,
    REFMCP_PROBE_ONESHOT = 1u << 1,
    REFMCP_PROBE_WINDOWED = 1u << 2,
    REFMCP_PROBE_INSTRUCTION_LIMIT = 1u << 3,
    REFMCP_PROBE_EMIT = 1u << 4
};

using REFMCPProbeInvokeV1 = std::int32_t (*)(
    const char* command,
    const char* request_json,
    char* response_json,
    std::uint32_t response_capacity,
    std::uint32_t* out_length);

using REFMCPProbeTickV1 = void (*)();
using REFMCPProbeObjectResolverV1 = void* (*)(
    void* context,
    const char* opaque_reference);
using REFMCPProbeSetObjectResolverV1 = void (*)(
    void* context,
    REFMCPProbeObjectResolverV1 resolver);

struct REFMCPProbeServiceV1 {
    std::uint32_t abi_version;
    std::uint32_t struct_size;
    std::uint32_t capabilities;
    const char* provider_version;
    REFMCPProbeInvokeV1 invoke;
    REFMCPProbeTickV1 tick;
    REFMCPProbeSetObjectResolverV1 set_object_resolver;
};

extern "C" __declspec(dllexport) const REFMCPProbeServiceV1*
reframework_get_probe_service_v1();
