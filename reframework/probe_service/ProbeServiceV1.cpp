#include "ProbeServiceV1.hpp"
#include "ProbeServiceHooks.hpp"

#include <Windows.h>
#include <bcrypt.h>

#include <json.hpp>

#include <lua.hpp>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstring>
#include <deque>
#include <iomanip>
#include <memory>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <utility>

namespace {

using json = nlohmann::json;
using Clock = std::chrono::steady_clock;

constexpr auto kProviderVersion = "1.0.1";
constexpr std::size_t kMaxRuns = 64;
constexpr std::size_t kMaximumOutputBytes = 1024u * 1024u;
constexpr std::uint64_t kDefaultInstructionLimit = 1'000'000u;
constexpr int kInstructionHookStep = 1000;

struct ProbeRun {
    std::recursive_mutex mutex;
    std::string probe_ref;
    std::string mode{"oneshot"};
    std::string state{"created"};
    std::string error;
    lua_State* lua{};
    Clock::time_point started{Clock::now()};
    Clock::time_point deadline{Clock::now() + std::chrono::seconds{5}};
    std::uint64_t instruction_limit{kDefaultInstructionLimit};
    std::uint64_t instructions{};
    std::size_t max_frames{300};
    std::size_t frames{};
    std::size_t max_events{1000};
    std::size_t dropped{};
    std::size_t output_bytes{};
    std::size_t cleanup_ticks{};
    bool destruction_requested{};
    std::deque<json> events;
};

std::recursive_mutex g_mutex;
std::unordered_map<std::string, std::shared_ptr<ProbeRun>> g_runs;
std::unordered_map<lua_State*, std::weak_ptr<ProbeRun>> g_by_state;
std::deque<std::string> g_order;
void* g_resolver_context{};
REFMCPProbeObjectResolverV1 g_object_resolver{};

std::string utc_timestamp() {
    const auto now = std::chrono::system_clock::now();
    const auto seconds = std::chrono::time_point_cast<std::chrono::seconds>(now);
    const auto milliseconds = std::chrono::duration_cast<std::chrono::milliseconds>(
        now - seconds).count();
    const auto time = std::chrono::system_clock::to_time_t(now);
    std::tm utc{};
    gmtime_s(&utc, &time);
    std::ostringstream stream;
    stream << std::put_time(&utc, "%Y-%m-%dT%H:%M:%S")
           << '.' << std::setw(3) << std::setfill('0') << milliseconds << 'Z';
    return stream.str();
}

std::string random_ref() {
    std::array<unsigned char, 16> bytes{};
    if (BCryptGenRandom(
            nullptr,
            bytes.data(),
            static_cast<ULONG>(bytes.size()),
            BCRYPT_USE_SYSTEM_PREFERRED_RNG) < 0) {
        throw std::runtime_error("BCryptGenRandom failed while creating ProbeRef");
    }
    std::ostringstream stream;
    stream << "probe:";
    for (const auto byte : bytes) {
        stream << std::hex << std::setw(2) << std::setfill('0')
               << static_cast<unsigned int>(byte);
    }
    return stream.str();
}

std::shared_ptr<ProbeRun> run_for_state(lua_State* lua) {
    std::scoped_lock lock{g_mutex};
    const auto found = g_by_state.find(lua);
    return found == g_by_state.end() ? nullptr : found->second.lock();
}

json lua_value(lua_State* lua, int index, int depth = 0) {
    if (depth > 4) {
        return {{"kind", "truncated"}, {"reason", "max_depth"}};
    }
    const auto absolute = lua_absindex(lua, index);
    switch (lua_type(lua, absolute)) {
    case LUA_TNIL:
        return nullptr;
    case LUA_TBOOLEAN:
        return lua_toboolean(lua, absolute) != 0;
    case LUA_TNUMBER:
        if (lua_isinteger(lua, absolute)) {
            return lua_tointeger(lua, absolute);
        }
        return lua_tonumber(lua, absolute);
    case LUA_TSTRING: {
        std::size_t length{};
        const auto* value = lua_tolstring(lua, absolute, &length);
        return std::string{value != nullptr ? value : "", length};
    }
    case LUA_TTABLE: {
        const auto array_size = std::min<std::size_t>(lua_rawlen(lua, absolute), 100);
        if (array_size > 0) {
            json result = json::array();
            for (std::size_t position = 1; position <= array_size; ++position) {
                lua_rawgeti(lua, absolute, static_cast<lua_Integer>(position));
                result.push_back(lua_value(lua, -1, depth + 1));
                lua_pop(lua, 1);
            }
            return result;
        }
        json result = json::object();
        std::size_t count{};
        lua_pushnil(lua);
        while (count < 100 && lua_next(lua, absolute) != 0) {
            std::string key;
            if (lua_type(lua, -2) == LUA_TSTRING) {
                key = lua_tostring(lua, -2);
            } else if (lua_isinteger(lua, -2)) {
                key = std::to_string(lua_tointeger(lua, -2));
            } else {
                key = "key_" + std::to_string(count);
            }
            result[key] = lua_value(lua, -1, depth + 1);
            lua_pop(lua, 1);
            ++count;
        }
        if (lua_gettop(lua) >= absolute + 1) {
            lua_pop(lua, 1);
        }
        return result;
    }
    case LUA_TUSERDATA:
        return {{"kind", "userdata"}, {"redacted", true}};
    case LUA_TLIGHTUSERDATA:
        return {{"kind", "lightuserdata"}, {"redacted", true}};
    case LUA_TFUNCTION:
        return {{"kind", "function"}};
    case LUA_TTHREAD:
        return {{"kind", "thread"}};
    default:
        return {{"kind", lua_typename(lua, lua_type(lua, absolute))}};
    }
}

void request_destroy(const std::shared_ptr<ProbeRun>& run) {
    std::scoped_lock lock{run->mutex};
    if (run->lua == nullptr || run->destruction_requested) return;
    reframework_destroy_script_state(run->lua);
    run->destruction_requested = true;
    run->cleanup_ticks = 3;
}

void instruction_hook(lua_State* lua, lua_Debug*) {
    const auto run = run_for_state(lua);
    if (!run) {
        luaL_error(lua, "Probe context is unavailable");
        return;
    }
    std::scoped_lock lock{run->mutex};
    run->instructions += kInstructionHookStep;
    if (run->instructions > run->instruction_limit) {
        run->state = "failed";
        run->error = "Lua instruction limit exceeded";
        luaL_error(lua, "%s", run->error.c_str());
        return;
    }
    if (Clock::now() >= run->deadline) {
        run->state = "failed";
        run->error = "Lua Probe timeout exceeded";
        luaL_error(lua, "%s", run->error.c_str());
    }
}

int probe_emit(lua_State* lua) {
    const auto run = run_for_state(lua);
    if (!run) return 0;
    std::scoped_lock lock{run->mutex};
    if (run->events.size() >= run->max_events) {
        ++run->dropped;
        return 0;
    }
    const auto count = lua_gettop(lua);
    json event{
        {"timestamp", utc_timestamp()},
        {"name", count >= 2 && lua_type(lua, 1) == LUA_TSTRING
            ? lua_tostring(lua, 1)
            : "emit"},
        {"value", lua_value(lua, count >= 2 ? 2 : 1)},
    };
    const auto bytes = event.dump().size();
    if (run->output_bytes + bytes > kMaximumOutputBytes) {
        ++run->dropped;
        return 0;
    }
    run->output_bytes += bytes;
    run->events.push_back(std::move(event));
    return 0;
}

int probe_resolve_pointer(lua_State* lua) {
    const auto* reference = luaL_checkstring(lua, 1);
    void* context{};
    REFMCPProbeObjectResolverV1 resolver{};
    {
        std::scoped_lock lock{g_mutex};
        context = g_resolver_context;
        resolver = g_object_resolver;
    }
    auto* object = resolver != nullptr ? resolver(context, reference) : nullptr;
    if (object == nullptr) {
        lua_pushnil(lua);
    } else {
        lua_pushlightuserdata(lua, object);
    }
    return 1;
}

void install_probe_api(const std::shared_ptr<ProbeRun>& run) {
    auto* lua = run->lua;
    for (const auto* name : {
             "io",
             "package",
             "require",
             "loadfile",
             "dofile",
             "debug",
             "fs",
             "imgui",
         }) {
        lua_pushnil(lua);
        lua_setglobal(lua, name);
    }

    lua_newtable(lua);
    lua_pushcfunction(lua, probe_emit);
    lua_setfield(lua, -2, "emit");
    lua_pushcfunction(lua, probe_resolve_pointer);
    lua_setfield(lua, -2, "_resolve_pointer");
    lua_setglobal(lua, "probe");

    constexpr auto bootstrap = R"lua(
        function probe.construct_list(type_name, values)
            local value = sdk.create_instance(type_name)
            if value == nil then error("cannot construct " .. type_name) end
            value:call(".ctor()")
            for _, item in ipairs(values or {}) do value:call("Add", item) end
            return value
        end
        function probe.construct_array(type_name, values)
            local element_type = string.match(type_name, "^System%.Array<(.+)>$")
                or string.match(type_name, "^(.+)%[%]$")
                or type_name
            local items = values or {}
            local value = sdk.create_managed_array(element_type, #items)
            if value == nil then error("cannot construct " .. type_name) end
            for index, item in ipairs(items) do value[index - 1] = item end
            return value
        end
        function probe.construct_dictionary(type_name, values)
            local value = sdk.create_instance(type_name)
            if value == nil then error("cannot construct " .. type_name) end
            value:call(".ctor()")
            for key, item in pairs(values or {}) do value:call("Add", key, item) end
            return value
        end
        function probe.assert_type(value, expected)
            if value == nil then error("expected " .. expected .. ", got nil") end
            return value
        end
        function probe.resolve_object(object_ref)
            local pointer = probe._resolve_pointer(object_ref)
            if pointer == nil then error("ObjectRef is unavailable or expired") end
            return sdk.to_managed_object(pointer)
        end
        function probe.resolve_hook_value(hook_ref, selector)
            return probe.resolve_object(hook_ref .. "|" .. selector)
        end
        function probe.resolve_root(_)
            error("Unsupported root kind")
        end
    )lua";
    if (luaL_dostring(lua, bootstrap) != LUA_OK) {
        const auto* message = lua_tostring(lua, -1);
        throw std::runtime_error(
            message != nullptr ? message : "Unable to initialize probe API");
    }
    lua_sethook(lua, instruction_hook, LUA_MASKCOUNT, kInstructionHookStep);
}

std::shared_ptr<ProbeRun> create_run(const json& request) {
    auto run = std::make_shared<ProbeRun>();
    run->probe_ref = random_ref();
    run->mode = request.value("mode", "oneshot");
    if (
        run->mode != "oneshot"
        && run->mode != "windowed"
        && run->mode != "windowed_hook_test"
    ) {
        throw std::runtime_error("Unsupported Probe mode");
    }
    const auto seconds = std::clamp(request.value("timeout_seconds", 5.0), 0.1, 30.0);
    run->deadline = Clock::now()
        + std::chrono::milliseconds{static_cast<long long>(seconds * 1000.0)};
    run->instruction_limit = std::clamp<std::uint64_t>(
        request.value("max_instructions", kDefaultInstructionLimit),
        10'000u,
        10'000'000u);
    run->max_frames = std::clamp<std::size_t>(
        request.value("max_frames", static_cast<std::size_t>(300)),
        1,
        1800);
    run->max_events = std::clamp<std::size_t>(
        request.value("max_events", static_cast<std::size_t>(1000)),
        1,
        10'000);
    run->lua = reframework_create_script_state();
    if (run->lua == nullptr) {
        throw std::runtime_error("REFramework failed to create an isolated ScriptState");
    }
    {
        std::scoped_lock lock{g_mutex};
        g_runs.emplace(run->probe_ref, run);
        g_by_state.emplace(run->lua, run);
        g_order.push_back(run->probe_ref);
    }
    try {
        install_probe_api(run);
    } catch (...) {
        request_destroy(run);
        throw;
    }
    return run;
}

json status_json(const std::shared_ptr<ProbeRun>& run) {
    std::scoped_lock lock{run->mutex};
    return {
        {"probe_ref", run->probe_ref},
        {"mode", run->mode},
        {"state", run->state},
        {"error", run->error.empty() ? json(nullptr) : json(run->error)},
        {"frames", run->frames},
        {"max_frames", run->max_frames},
        {"instructions", run->instructions},
        {"max_instructions", run->instruction_limit},
        {"instruction_granularity", kInstructionHookStep},
        {"instruction_count_kind", "sampled_lower_bound"},
        {"frames_kind", "execution_frames"},
        {"event_count", run->events.size()},
        {"dropped", run->dropped},
        {"output_bytes", run->output_bytes},
        {"events", run->events},
        {"event_resource", "reframework://probes/" + run->probe_ref + "/events"},
    };
}

json compile_probe(const json& request) {
    const auto code = request.value("code", "");
    if (code.empty()) {
        return {{"valid", false}, {"message", "Probe code is empty"}};
    }
    auto run = create_run(
        {
            {"mode", "oneshot"},
            {"timeout_seconds", 2.0},
            {"max_frames", 1},
            {"max_events", 1},
            {"max_instructions", 100'000},
        });
    const auto result = luaL_loadbuffer(
        run->lua,
        code.data(),
        code.size(),
        "reframework-mcp-probe");
    json response;
    if (result == LUA_OK) {
        lua_pop(run->lua, 1);
        response = {{"valid", true}, {"message", nullptr}};
        run->state = "compiled";
    } else {
        const auto* message = lua_tostring(run->lua, -1);
        response = {
            {"valid", false},
            {"message", message != nullptr ? message : "Lua compilation failed"},
        };
        lua_pop(run->lua, 1);
        run->state = "failed";
    }
    request_destroy(run);
    return response;
}

json run_probe(const json& request) {
    const auto code = request.value("code", "");
    if (code.empty()) throw std::runtime_error("Probe code is empty");
    if (code.size() > kMaximumOutputBytes) {
        throw std::runtime_error("Probe code exceeds 1 MiB");
    }
    auto run = create_run(request);
    run->state = "running";
    auto result = luaL_loadbuffer(
        run->lua,
        code.data(),
        code.size(),
        "reframework-mcp-probe");
    if (result == LUA_OK) {
        run->frames = 1;
        run->instructions = 1;
        result = lua_pcall(run->lua, 0, LUA_MULTRET, 0);
    }
    if (result != LUA_OK) {
        const auto* message = lua_tostring(run->lua, -1);
        run->state = "failed";
        run->error = message != nullptr ? message : "Lua Probe execution failed";
        lua_pop(run->lua, 1);
        request_destroy(run);
    } else if (run->mode == "oneshot") {
        run->state = "completed";
        request_destroy(run);
    }
    return status_json(run);
}

std::shared_ptr<ProbeRun> require_run(const json& request) {
    const auto probe_ref = request.value("probe_ref", "");
    std::scoped_lock lock{g_mutex};
    const auto found = g_runs.find(probe_ref);
    if (found == g_runs.end()) {
        throw std::runtime_error("ProbeRef was not found");
    }
    return found->second;
}

json cancel_probe(const json& request) {
    const auto run = require_run(request);
    {
        std::scoped_lock lock{run->mutex};
        if (run->state == "running") run->state = "cancelled";
    }
    request_destroy(run);
    return status_json(run);
}

void service_tick() {
    std::vector<std::shared_ptr<ProbeRun>> destroy;
    {
        std::scoped_lock lock{g_mutex};
        for (const auto& [_, run] : g_runs) {
            std::scoped_lock run_lock{run->mutex};
            if (run->state == "running") {
                if (
                    run->frames >= run->max_frames
                    || Clock::now() >= run->deadline
                    || run->events.size() >= run->max_events
                ) {
                    run->state = "completed";
                    destroy.push_back(run);
                } else {
                    ++run->frames;
                }
            } else if (
                !run->destruction_requested
                && run->lua != nullptr
                && run->state != "created"
            ) {
                destroy.push_back(run);
            }
            if (run->destruction_requested && run->cleanup_ticks > 0) {
                --run->cleanup_ticks;
                if (run->cleanup_ticks == 0 && run->lua != nullptr) {
                    g_by_state.erase(run->lua);
                    run->lua = nullptr;
                }
            }
        }
    }
    for (const auto& run : destroy) {
        request_destroy(run);
    }

    std::scoped_lock lock{g_mutex};
    while (g_order.size() > kMaxRuns) {
        const auto probe_ref = g_order.front();
        const auto found = g_runs.find(probe_ref);
        if (
            found != g_runs.end()
            && found->second->state == "running"
        ) {
            break;
        }
        if (found != g_runs.end() && found->second->lua != nullptr) {
            break;
        }
        g_order.pop_front();
        g_runs.erase(probe_ref);
    }
}

json shutdown_service() {
    std::vector<std::shared_ptr<ProbeRun>> runs;
    {
        std::scoped_lock lock{g_mutex};
        for (const auto& [_, run] : g_runs) {
            runs.push_back(run);
        }
    }
    for (const auto& run : runs) {
        {
            std::scoped_lock lock{run->mutex};
            if (run->state == "running") run->state = "cancelled";
        }
        request_destroy(run);
    }
    return {{"state", "shutting_down"}, {"probe_count", runs.size()}};
}

void set_object_resolver(
    void* context,
    REFMCPProbeObjectResolverV1 resolver) {
    std::scoped_lock lock{g_mutex};
    g_resolver_context = context;
    g_object_resolver = resolver;
}

json dispatch(const std::string& command, const json& request) {
    if (command == "compile_lua_probe") return compile_probe(request);
    if (command == "run_lua_probe") return run_probe(request);
    if (command == "get_probe_status") return status_json(require_run(request));
    if (command == "cancel_lua_probe") return cancel_probe(request);
    if (command == "shutdown_probe_service") return shutdown_service();
    throw std::runtime_error("Unknown Probe Service command: " + command);
}

std::int32_t write_response(
    const json& response,
    char* output,
    std::uint32_t capacity,
    std::uint32_t* out_length,
    std::int32_t status) {
    const auto encoded = response.dump();
    if (out_length == nullptr) return -1;
    *out_length = static_cast<std::uint32_t>(encoded.size());
    if (output == nullptr || capacity <= encoded.size()) {
        return 1;
    }
    std::memcpy(output, encoded.data(), encoded.size());
    output[encoded.size()] = '\0';
    return status;
}

std::int32_t invoke_service(
    const char* command,
    const char* request_json,
    char* response_json,
    std::uint32_t response_capacity,
    std::uint32_t* out_length) {
    try {
        if (command == nullptr || request_json == nullptr) {
            throw std::runtime_error("Probe Service command and request are required");
        }
        const auto request = json::parse(request_json);
        return write_response(
            dispatch(command, request),
            response_json,
            response_capacity,
            out_length,
            0);
    } catch (const std::exception& error) {
        return write_response(
            {
                {"code", "PROBE_FAILED"},
                {"message", error.what()},
                {"retryable", false},
            },
            response_json,
            response_capacity,
            out_length,
            -1);
    } catch (...) {
        return write_response(
            {
                {"code", "PROBE_FAILED"},
                {"message", "Unknown Probe Service failure"},
                {"retryable", false},
            },
            response_json,
            response_capacity,
            out_length,
            -1);
    }
}

const REFMCPProbeServiceV1 kService{
    REFMCP_PROBE_SERVICE_ABI_V1,
    sizeof(REFMCPProbeServiceV1),
    REFMCP_PROBE_COMPILE
        | REFMCP_PROBE_ONESHOT
        | REFMCP_PROBE_WINDOWED
        | REFMCP_PROBE_INSTRUCTION_LIMIT
        | REFMCP_PROBE_EMIT,
    kProviderVersion,
    invoke_service,
    service_tick,
    set_object_resolver,
};

} // namespace

extern "C" __declspec(dllexport) const REFMCPProbeServiceV1*
reframework_get_probe_service_v1() {
    return &kService;
}
