#include "ExportServiceV1.hpp"
#include "ExportServiceHooks.hpp"

#include <reframework_mcp/version.hpp>

#include <Windows.h>
#include <bcrypt.h>

#include <json.hpp>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cctype>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <mutex>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace {

using json = nlohmann::json;
namespace fs = std::filesystem;

constexpr auto kProviderVersion = reframework_mcp::kProjectVersion.data();

std::wstring widen(const std::string& value) {
    if (value.empty()) return {};
    const auto length = MultiByteToWideChar(
        CP_UTF8, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), nullptr, 0);
    if (length <= 0) throw std::runtime_error("Invalid UTF-8 path");
    std::wstring output(static_cast<std::size_t>(length), L'\0');
    MultiByteToWideChar(
        CP_UTF8,
        MB_ERR_INVALID_CHARS,
        value.data(),
        static_cast<int>(value.size()),
        output.data(),
        length);
    return output;
}

std::string narrow(const fs::path& value) {
    const auto wide = value.wstring();
    if (wide.empty()) return {};
    const auto length = WideCharToMultiByte(
        CP_UTF8, WC_ERR_INVALID_CHARS, wide.data(), static_cast<int>(wide.size()),
        nullptr, 0, nullptr, nullptr);
    if (length <= 0) throw std::runtime_error("Path cannot be encoded as UTF-8");
    std::string output(static_cast<std::size_t>(length), '\0');
    WideCharToMultiByte(
        CP_UTF8,
        WC_ERR_INVALID_CHARS,
        wide.data(),
        static_cast<int>(wide.size()),
        output.data(),
        length,
        nullptr,
        nullptr);
    return output;
}

std::string sha256_bytes(const void* input, std::size_t size) {
    BCRYPT_ALG_HANDLE algorithm{};
    BCRYPT_HASH_HANDLE hash{};
    DWORD object_size{};
    DWORD result_size{};
    if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) < 0
        || BCryptGetProperty(
            algorithm,
            BCRYPT_OBJECT_LENGTH,
            reinterpret_cast<PUCHAR>(&object_size),
            sizeof(object_size),
            &result_size,
            0) < 0) {
        if (algorithm != nullptr) BCryptCloseAlgorithmProvider(algorithm, 0);
        throw std::runtime_error("Unable to initialize SHA-256");
    }
    std::vector<unsigned char> object(object_size);
    std::array<unsigned char, 32> digest{};
    const auto failed = BCryptCreateHash(
            algorithm, &hash, object.data(), static_cast<ULONG>(object.size()),
            nullptr, 0, 0) < 0
        || BCryptHashData(
            hash,
            reinterpret_cast<PUCHAR>(const_cast<void*>(input)),
            static_cast<ULONG>(size),
            0) < 0
        || BCryptFinishHash(hash, digest.data(), static_cast<ULONG>(digest.size()), 0) < 0;
    if (hash != nullptr) BCryptDestroyHash(hash);
    BCryptCloseAlgorithmProvider(algorithm, 0);
    if (failed) throw std::runtime_error("Unable to compute SHA-256");
    std::ostringstream stream;
    for (const auto byte : digest) {
        stream << std::hex << std::setw(2) << std::setfill('0')
               << static_cast<unsigned int>(byte);
    }
    return stream.str();
}

std::string sha256_text(const std::string& value) {
    return sha256_bytes(value.data(), value.size());
}

std::string sha256_file(const fs::path& path) {
    std::ifstream input{path, std::ios::binary};
    if (!input) throw std::runtime_error("Unable to open generated il2cpp_dump.json");
    BCRYPT_ALG_HANDLE algorithm{};
    BCRYPT_HASH_HANDLE hash{};
    DWORD object_size{};
    DWORD result_size{};
    if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) < 0
        || BCryptGetProperty(
            algorithm,
            BCRYPT_OBJECT_LENGTH,
            reinterpret_cast<PUCHAR>(&object_size),
            sizeof(object_size),
            &result_size,
            0) < 0) {
        if (algorithm != nullptr) BCryptCloseAlgorithmProvider(algorithm, 0);
        throw std::runtime_error("Unable to initialize streaming SHA-256");
    }
    std::vector<unsigned char> object(object_size);
    if (BCryptCreateHash(
            algorithm, &hash, object.data(), static_cast<ULONG>(object.size()),
            nullptr, 0, 0) < 0) {
        BCryptCloseAlgorithmProvider(algorithm, 0);
        throw std::runtime_error("Unable to create streaming SHA-256");
    }
    std::array<char, 1024u * 1024u> buffer{};
    while (input) {
        input.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        const auto count = input.gcount();
        if (count > 0
            && BCryptHashData(
                hash,
                reinterpret_cast<PUCHAR>(buffer.data()),
                static_cast<ULONG>(count),
                0) < 0) {
            BCryptDestroyHash(hash);
            BCryptCloseAlgorithmProvider(algorithm, 0);
            throw std::runtime_error("Unable to update streaming SHA-256");
        }
    }
    std::array<unsigned char, 32> digest{};
    const auto failed = BCryptFinishHash(
        hash, digest.data(), static_cast<ULONG>(digest.size()), 0) < 0;
    BCryptDestroyHash(hash);
    BCryptCloseAlgorithmProvider(algorithm, 0);
    if (failed) throw std::runtime_error("Unable to finish streaming SHA-256");
    std::ostringstream stream;
    for (const auto byte : digest) {
        stream << std::hex << std::setw(2) << std::setfill('0')
               << static_cast<unsigned int>(byte);
    }
    return stream.str();
}

std::string random_id() {
    std::array<unsigned char, 12> bytes{};
    if (BCryptGenRandom(
            nullptr,
            bytes.data(),
            static_cast<ULONG>(bytes.size()),
            BCRYPT_USE_SYSTEM_PREFERRED_RNG) < 0) {
        throw std::runtime_error("Unable to generate export job id");
    }
    std::ostringstream stream;
    for (const auto byte : bytes) {
        stream << std::hex << std::setw(2) << std::setfill('0')
               << static_cast<unsigned int>(byte);
    }
    return stream.str();
}

std::string utc_now() {
    const auto now = std::chrono::system_clock::now();
    const auto time = std::chrono::system_clock::to_time_t(now);
    std::tm utc{};
    gmtime_s(&utc, &time);
    std::ostringstream stream;
    stream << std::put_time(&utc, "%Y-%m-%dT%H:%M:%SZ");
    return stream.str();
}

std::string safe_component(std::string value) {
    for (auto& character : value) {
        if (!std::isalnum(static_cast<unsigned char>(character))
            && character != '-' && character != '_') {
            character = '_';
        }
    }
    return value.empty() ? "unknown" : value;
}

std::string stage_name(int stage) {
    constexpr std::array names{
        "initialization", "types", "rsz", "methods", "fields", "properties",
        "rsz_name_resolution", "deserializer_chain", "non_tdb_types", "generate_sdk"};
    if (stage < 0 || static_cast<std::size_t>(stage) >= names.size()) return "initialization";
    return names[static_cast<std::size_t>(stage)];
}

double overall_export_progress(int stage, double stage_progress) {
    constexpr double export_weight = 0.90;
    constexpr double stage_count = 10.0;
    const auto bounded_stage = std::clamp(stage, 0, 9);
    const auto bounded_progress = std::clamp(stage_progress, 0.0, 1.0);
    return (std::min)(
        export_weight,
        ((static_cast<double>(bounded_stage) + bounded_progress) / stage_count)
            * export_weight);
}

std::uint64_t total_entities(const REFMCPExportTDBInfo& tdb) {
    return static_cast<std::uint64_t>(tdb.types)
        + static_cast<std::uint64_t>(tdb.methods)
        + static_cast<std::uint64_t>(tdb.fields)
        + static_cast<std::uint64_t>(tdb.properties);
}

std::uint64_t processed_entities(
    const REFMCPExportTDBInfo& tdb,
    int stage,
    double stage_progress) {
    const auto progress = std::clamp(stage_progress, 0.0, 1.0);
    const auto portion = [progress](auto count) {
        return static_cast<std::uint64_t>(
            static_cast<double>(count) * progress);
    };
    if (stage <= 0) return 0;
    if (stage == 1) return portion(tdb.types);
    auto processed = static_cast<std::uint64_t>(tdb.types);
    if (stage == 2) return processed;
    if (stage == 3) return processed + portion(tdb.methods);
    processed += static_cast<std::uint64_t>(tdb.methods);
    if (stage == 4) return processed + portion(tdb.fields);
    processed += static_cast<std::uint64_t>(tdb.fields);
    if (stage == 5) return processed + portion(tdb.properties);
    return total_entities(tdb);
}

class ExportService {
public:
    ~ExportService() {
        if (m_worker.joinable()) m_worker.join();
    }

    std::int32_t invoke(
        const char* command,
        const char* request_json,
        char* response_json,
        std::uint32_t response_capacity,
        std::uint32_t* out_length) noexcept {
        json response;
        std::int32_t result{};
        try {
            const auto request = request_json != nullptr && request_json[0] != '\0'
                ? json::parse(request_json)
                : json::object();
            const auto name = command != nullptr ? std::string{command} : std::string{};
            if (name == "run_generate_sdk") response = start(request);
            else if (name == "get_export_status") response = status(request);
            else throw std::runtime_error("Unknown Export Service command");
        } catch (const std::exception& error) {
            result = -1;
            response = {
                {"code", "EXPORT_FAILED"},
                {"message", error.what()},
                {"retryable", false},
            };
        }
        const auto encoded = response.dump();
        if (out_length != nullptr) *out_length = static_cast<std::uint32_t>(encoded.size());
        if (response_json == nullptr || response_capacity <= encoded.size()) return 1;
        std::memcpy(response_json, encoded.data(), encoded.size());
        response_json[encoded.size()] = '\0';
        return result;
    }

private:
    json start(const json& request);
    json status(const json& request);
    void run(json request, std::string job_ref);
    json find_reusable(const fs::path& base, const std::string& fingerprint, const std::string& mode);
    static std::string fingerprint();
    static void validate_dump(const fs::path& dump);
    void update(json values);

    std::mutex m_mutex;
    std::thread m_worker;
    json m_status{{"state", "idle"}};
    bool m_running{};
};

ExportService g_service;

std::int32_t invoke_service(
    const char* command,
    const char* request_json,
    char* response_json,
    std::uint32_t response_capacity,
    std::uint32_t* out_length) {
    return g_service.invoke(
        command,
        request_json,
        response_json,
        response_capacity,
        out_length);
}

json ExportService::start(const json& request) {
    const auto mode = request.at("mode").get<std::string>();
    if (mode != "json_only" && mode != "sdk_and_json") {
        throw std::runtime_error("mode must be json_only or sdk_and_json");
    }
    const auto policy = request.value("policy", "reuse_if_fresh");
    if (policy != "reuse_if_fresh" && policy != "force") {
        throw std::runtime_error("policy must be reuse_if_fresh or force");
    }
    const fs::path snapshot_root{widen(request.at("snapshot_root").get<std::string>())};
    if (!snapshot_root.is_absolute()) {
        throw std::runtime_error("snapshot_root must be absolute");
    }
    const auto current_fingerprint = request.value(
        "_tdb_fingerprint",
        fingerprint());
    const auto game_id = safe_component(request.value(
        "_game_id",
        std::string{reframework_export_game_name()}));
    const auto tdb = reframework_export_tdb_info();
    const auto base = snapshot_root / game_id / current_fingerprint.substr(7, 24);
    if (policy == "reuse_if_fresh") {
        const auto reusable = find_reusable(base, current_fingerprint, mode);
        if (!reusable.is_null()) return reusable;
    }

    std::unique_lock lock{m_mutex};
    if (m_running) {
        if (m_status.value("mode", "") == mode
            && m_status.value("tdb_fingerprint", "") == current_fingerprint) {
            return m_status;
        }
        throw std::runtime_error("EXPORT_ALREADY_RUNNING");
    }
    lock.unlock();
    if (m_worker.joinable()) m_worker.join();
    lock.lock();
    const auto job_ref = "export:" + random_id();
    m_running = true;
    m_status = {
        {"job_ref", job_ref},
        {"state", "queued"},
        {"mode", mode},
        {"runtime_epoch", request.value("_runtime_epoch", std::string{})},
        {"reframework_version",
            request.value("_reframework_version", std::string{"unknown"})},
        {"tdb_fingerprint", current_fingerprint},
        {"stage", "initialization"},
        {"stage_progress", 0.0},
        {"overall_progress", 0.0},
        {"processed_entities", 0},
        {"total_entities", total_entities(tdb)},
        {"processed_entities_kind", "estimated_from_completed_tdb_stages"},
        {"overall_progress_kind", "weighted_monotonic"},
        {"started_at", utc_now()},
        {"last_progress_at", utc_now()},
        {"provider_version", kProviderVersion},
    };
    auto worker_request = request;
    worker_request["_fingerprint"] = current_fingerprint;
    worker_request["_game_id"] = game_id;
    worker_request["_base"] = narrow(base);
    m_worker = std::thread{
        [this, worker_request = std::move(worker_request), job_ref] {
            run(worker_request, job_ref);
        }};
    return m_status;
}

json ExportService::status(const json& request) {
    std::scoped_lock lock{m_mutex};
    const auto requested = request.value("job_ref", "");
    if (!requested.empty() && requested != m_status.value("job_ref", "")) {
        throw std::runtime_error("EXPORT_JOB_NOT_FOUND");
    }
    if (
        m_running
        && (m_status.value("state", "") == "queued"
            || m_status.value("state", "") == "exporting")) {
        const auto stage = reframework_sdk_dump_stage();
        const auto stage_progress = std::clamp(
            static_cast<double>(reframework_sdk_dump_progress()),
            0.0,
            1.0);
        const auto weighted = overall_export_progress(stage, stage_progress);
        const auto previous = m_status.value("overall_progress", 0.0);
        const auto tdb = reframework_export_tdb_info();
        m_status["state"] = "exporting";
        m_status["stage"] = stage_name(stage);
        m_status["stage_progress"] = stage_progress;
        m_status["overall_progress"] = (std::max)(previous, weighted);
        m_status["processed_entities"] =
            processed_entities(tdb, stage, stage_progress);
        m_status["total_entities"] = total_entities(tdb);
        m_status["last_progress_at"] = utc_now();
    }
    return m_status;
}

json ExportService::find_reusable(
    const fs::path& base,
    const std::string& current_fingerprint,
    const std::string& mode) {
    std::error_code error;
    if (!fs::is_directory(base, error)) return nullptr;
    for (const auto& directory : fs::directory_iterator{base, error}) {
        if (error || !directory.is_directory()) continue;
        const auto manifest_path = directory.path() / "manifest.json";
        std::ifstream input{manifest_path};
        if (!input) continue;
        try {
            json manifest;
            input >> manifest;
            if (manifest.value("tdb_fingerprint", "") != current_fingerprint
                || manifest.value("mode", "") != mode
                || manifest.value("provider_version", "") != kProviderVersion) {
                continue;
            }
            const auto dump = directory.path() / "il2cpp_dump.json";
            if (!fs::is_regular_file(dump)) continue;
            return {
                {"job_ref", nullptr},
                {"state", "reused"},
                {"mode", mode},
                {"tdb_fingerprint", current_fingerprint},
                {"reused_snapshot_id", manifest.value("snapshot_id", "")},
                {"runtime_epoch", manifest.value("runtime_epoch", "")},
                {"reframework_version",
                    manifest.value("reframework_version", "unknown")},
                {"entity_counts", manifest.value("entity_counts", json::object())},
                {"artifacts", {
                    {"il2cpp_dump", narrow(dump)},
                    {"manifest", narrow(manifest_path)},
                }},
                {"provider_version", kProviderVersion},
            };
        } catch (...) {
        }
    }
    return nullptr;
}

std::string ExportService::fingerprint() {
    const auto tdb = reframework_export_tdb_info();
    const auto material = std::string{reframework_export_game_name()} + ":"
        + std::to_string(tdb.version) + ":"
        + std::to_string(tdb.types) + ":"
        + std::to_string(tdb.methods) + ":"
        + std::to_string(tdb.fields) + ":"
        + std::to_string(tdb.properties) + ":"
        + kProviderVersion;
    return "sha256:" + sha256_text(material);
}

void ExportService::run(json request, std::string job_ref) {
    fs::path temporary;
    try {
        update({
            {"state", "exporting"},
            {"stage", "initialization"},
            {"last_progress_at", utc_now()},
        });
        const auto mode = request.at("mode").get<std::string>();
        if (!reframework_generate_sdk(mode == "json_only")) {
            throw std::runtime_error("EXPORT_ALREADY_RUNNING");
        }

        update({
            {"state", "finalizing"},
            {"stage", "write_json"},
            {"stage_progress", 1.0},
            {"overall_progress", 0.92},
            {"last_progress_at", utc_now()},
        });
        const fs::path source_dump{reframework_export_persistent_file("il2cpp_dump.json")};
        validate_dump(source_dump);
        const auto artifact_sha256 = sha256_file(source_dump);
        const auto safe_id = random_id();
        const auto snapshot_id = "snapshot:"
            + request.at("_fingerprint").get<std::string>().substr(7, 16)
            + ":" + safe_id;
        const fs::path base{widen(request.at("_base").get<std::string>())};
        fs::create_directories(base);
        temporary = base / (safe_id + ".partial");
        const auto destination = base / safe_id;
        if (!fs::create_directory(temporary)) {
            throw std::runtime_error("Unable to create export temporary directory");
        }
        fs::copy_file(
            source_dump,
            temporary / "il2cpp_dump.json",
            fs::copy_options::none);

        if (mode == "sdk_and_json") {
            const auto source_sdk = fs::current_path() / "sdk_ida";
            if (!fs::is_directory(source_sdk)) {
                throw std::runtime_error("SDK generation completed without sdk_ida directory");
            }
            fs::copy(
                source_sdk,
                temporary / "sdk_ida",
                fs::copy_options::recursive);
        }

        const auto tdb = reframework_export_tdb_info();
        const json coverage = {
            {"types", "complete"},
            {"methods", "complete"},
            {"method_parameters", "complete"},
            {"method_returns", "complete"},
            {"fields", "complete"},
            {"properties", "present_if_exported"},
            {"rsz", "present_if_exported"},
            {"reflection_methods", "partial_version_dependent"},
            {"reflection_properties", "partial_version_dependent"},
            {"deserializer_chain", "present_if_exported"},
        };
        const json manifest = {
            {"snapshot_schema", "1.0"},
            {"snapshot_id", snapshot_id},
            {"game_id", request.at("_game_id")},
            {"tdb_version", tdb.version},
            {"tdb_fingerprint", request.at("_fingerprint")},
            {"runtime_epoch", request.value("_runtime_epoch", std::string{})},
            {"reframework_version",
                request.value("_reframework_version", std::string{"unknown"})},
            {"entity_counts", {
                {"types", tdb.types},
                {"methods", tdb.methods},
                {"fields", tdb.fields},
                {"properties", tdb.properties},
                {"total", total_entities(tdb)},
            }},
            {"provider", "reframework_export_service"},
            {"provider_version", kProviderVersion},
            {"mode", mode},
            {"artifact_sha256", artifact_sha256},
            {"created_at", utc_now()},
            {"job_ref", job_ref},
        };
        {
            std::ofstream output{temporary / "manifest.json"};
            output << manifest.dump(2) << '\n';
            if (!output.good()) throw std::runtime_error("Unable to write manifest.json");
        }
        {
            std::ofstream output{temporary / "coverage.json"};
            output << coverage.dump(2) << '\n';
            if (!output.good()) throw std::runtime_error("Unable to write coverage.json");
        }
        {
            std::ofstream output{temporary / "import-report.json"};
            output << json{
                {"state", "pending_host_import"},
                {"artifact_sha256", artifact_sha256},
                {"created_at", utc_now()},
            }.dump(2) << '\n';
            if (!output.good()) throw std::runtime_error("Unable to write import-report.json");
        }
        fs::rename(temporary, destination);
        temporary.clear();

        update({
            {"state", "completed"},
            {"stage", "completed"},
            {"stage_progress", 1.0},
            {"overall_progress", 1.0},
            {"processed_entities", total_entities(tdb)},
            {"total_entities", total_entities(tdb)},
            {"snapshot_id", snapshot_id},
            {"artifact_sha256", artifact_sha256},
            {"completed_at", utc_now()},
            {"last_progress_at", utc_now()},
            {"artifacts", {
                {"il2cpp_dump", narrow(destination / "il2cpp_dump.json")},
                {"manifest", narrow(destination / "manifest.json")},
                {"coverage", narrow(destination / "coverage.json")},
                {"sdk_ida", mode == "sdk_and_json"
                    ? json(narrow(destination / "sdk_ida"))
                    : json(nullptr)},
            }},
        });
    } catch (const std::exception& error) {
        if (!temporary.empty()) {
            std::error_code ignored;
            fs::remove_all(temporary, ignored);
        }
        update({
            {"state", "failed"},
            {"stage", "failed"},
            {"error", error.what()},
            {"failed_at", utc_now()},
            {"last_progress_at", utc_now()},
        });
    }
    std::scoped_lock lock{m_mutex};
    m_running = false;
}

void ExportService::validate_dump(const fs::path& dump) {
    std::error_code error;
    const auto size = fs::file_size(dump, error);
    if (error || size < 2) {
        throw std::runtime_error("Generated il2cpp_dump.json is missing or empty");
    }
    std::ifstream input{dump, std::ios::binary};
    char first{};
    while (input.get(first) && std::isspace(static_cast<unsigned char>(first))) {
    }
    if (first != '{') {
        throw std::runtime_error("Generated il2cpp_dump.json is not a JSON object");
    }
}

void ExportService::update(json values) {
    std::scoped_lock lock{m_mutex};
    for (auto& [key, value] : values.items()) {
        m_status[key] = std::move(value);
    }
}

} // namespace

extern "C" __declspec(dllexport) const REFMCPExportServiceV1*
reframework_get_export_service_v1() {
    static const REFMCPExportServiceV1 service{
        .abi_version = REFMCP_EXPORT_SERVICE_ABI_V1,
        .struct_size = sizeof(REFMCPExportServiceV1),
        .capabilities = REFMCP_EXPORT_JSON
            | REFMCP_EXPORT_SDK
            | REFMCP_EXPORT_PROGRESS,
        .provider_version = kProviderVersion,
        .invoke = invoke_service,
    };
    return &service;
}
