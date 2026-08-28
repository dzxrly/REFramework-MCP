#pragma once

#include <cstdint>
#include <string>

struct REFMCPExportTDBInfo {
    std::uint32_t version;
    std::uint32_t types;
    std::uint32_t methods;
    std::uint32_t fields;
    std::uint32_t properties;
};

bool reframework_generate_sdk(bool skip_sdkgenny);
float reframework_sdk_dump_progress() noexcept;
int reframework_sdk_dump_stage() noexcept;
const char* reframework_export_game_name() noexcept;
std::wstring reframework_export_persistent_file(const char* name);
REFMCPExportTDBInfo reframework_export_tdb_info() noexcept;
