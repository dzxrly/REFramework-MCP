#pragma once

#include "export_adapter.hpp"
#include "hook_manager.hpp"
#include "named_pipe_server.hpp"
#include "object_registry.hpp"
#include "probe_adapter.hpp"

#include <reframework/API.hpp>
#include <nlohmann/json.hpp>

#include <atomic>
#include <deque>
#include <future>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

namespace reframework_mcp {

class Bridge {
public:
    explicit Bridge(const REFrameworkPluginInitializeParam* parameter);
    ~Bridge();

    Bridge(const Bridge&) = delete;
    Bridge& operator=(const Bridge&) = delete;

    void start();
    void pump_game_thread();
    [[nodiscard]] nlohmann::json handle(const nlohmann::json& request);
    [[nodiscard]] const std::string& runtime_epoch() const noexcept;

private:
    struct PendingCommand {
        nlohmann::json request;
        std::promise<nlohmann::json> result;
        std::atomic<bool> cancelled{false};
    };

    class CommandError;

    nlohmann::json dispatch(const std::string& command, const nlohmann::json& payload);
    nlohmann::json dispatch_game_thread(
        const std::string& command,
        const nlohmann::json& payload);
    nlohmann::json submit_game_thread(const nlohmann::json& request);
    nlohmann::json runtime_status();
    nlohmann::json list_singletons(const nlohmann::json& payload);
    nlohmann::json inspect_object(const nlohmann::json& payload);
    nlohmann::json invoke_method(const nlohmann::json& payload);
    nlohmann::json set_field(const nlohmann::json& payload);
    nlohmann::json validate_access_plan(const nlohmann::json& payload);

    nlohmann::json inspect_entry(
        const std::string& object_ref,
        const ObjectEntry& entry,
        const std::string& member_filter,
        std::size_t collection_offset,
        std::size_t collection_limit,
        bool include_properties,
        bool allow_getters,
        const nlohmann::json& getter_allowlist);
    nlohmann::json inspect_collection(
        const ObjectEntry& entry,
        std::size_t collection_offset,
        std::size_t collection_limit);
    nlohmann::json inspect_properties(
        const ObjectEntry& entry,
        const std::string& member_filter,
        std::size_t limit,
        const nlohmann::json& getter_allowlist);
    static reframework::API::Method* find_method_by_arity(
        reframework::API::TypeDefinition* type,
        const std::string& name,
        std::size_t arity);
    static reframework::API::Method* find_method_exact(
        reframework::API::TypeDefinition* type,
        const std::string& selector,
        std::size_t arity);
    nlohmann::json read_field(
        const ObjectEntry& entry,
        reframework::API::Field* field);
    nlohmann::json read_value(
        void* raw,
        reframework::API::TypeDefinition* type);
    void write_value(
        void* raw,
        reframework::API::TypeDefinition* type,
        const nlohmann::json& value);
    std::vector<void*> encode_arguments(
        reframework::API::Method* method,
        const nlohmann::json& arguments);
    nlohmann::json invoke(
        reframework::API::Method* method,
        reframework::API::ManagedObject* object,
        const nlohmann::json& arguments);
    nlohmann::json return_value(
        const reframework::InvokeRet& value,
        reframework::API::TypeDefinition* type);
    ObjectEntry require_object(const std::string& object_ref);
    static std::pair<std::string, std::string> split_member_signature(
        const nlohmann::json& member_ref);
    static std::string field_name(const std::string& selector);
    static std::wstring pipe_name();
    static std::string new_runtime_epoch();
    static std::string tdb_fingerprint(reframework::API::TDB* tdb);
    static nlohmann::json ok_response(
        const std::string& request_id,
        const std::string& runtime_epoch,
        nlohmann::json data);
    static nlohmann::json error_response(
        const std::string& request_id,
        const std::string& runtime_epoch,
        const std::string& code,
        const std::string& message,
        nlohmann::json details = nlohmann::json::object(),
        bool retryable = false);

    const REFrameworkPluginInitializeParam* m_parameter{};
    std::thread::id m_game_thread;
    std::string m_runtime_epoch;
    std::string m_tdb_fingerprint;
    ExportAdapter m_export;
    ObjectRegistry m_objects;
    HookManager m_hooks;
    ProbeAdapter m_probe;
    NamedPipeServer m_pipe;
    std::mutex m_queue_mutex;
    std::deque<std::shared_ptr<PendingCommand>> m_queue;
};

} // namespace reframework_mcp
