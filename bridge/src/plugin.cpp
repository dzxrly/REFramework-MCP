#include "bridge.hpp"

#include <reframework/API.hpp>

#include <memory>
#include <stdexcept>

namespace {

std::unique_ptr<reframework_mcp::Bridge> g_bridge;

void on_present() {
    if (g_bridge != nullptr) {
        g_bridge->pump_game_thread();
    }
}

} // namespace

extern "C" __declspec(dllexport) void reframework_plugin_required_version(
    REFrameworkPluginVersion* version) {
    version->major = REFRAMEWORK_PLUGIN_VERSION_MAJOR;
    version->minor = REFRAMEWORK_PLUGIN_VERSION_MINOR;
    version->patch = REFRAMEWORK_PLUGIN_VERSION_PATCH;
}

extern "C" __declspec(dllexport) bool reframework_plugin_initialize(
    const REFrameworkPluginInitializeParam* parameter) {
    if (parameter == nullptr || parameter->functions == nullptr || parameter->sdk == nullptr) {
        return false;
    }
    try {
        reframework::API::initialize(parameter);
        g_bridge = std::make_unique<reframework_mcp::Bridge>(parameter);
        if (!parameter->functions->on_present(on_present)) {
            throw std::runtime_error("REFramework rejected the on_present callback");
        }
        g_bridge->start();
        parameter->functions->log_info(
            "[REFramework-MCP] Bridge 1.0.0 started on protocol 1.0");
        return true;
    } catch (const std::exception& error) {
        if (parameter->functions->log_error != nullptr) {
            parameter->functions->log_error(
                "[REFramework-MCP] Initialization failed: %s",
                error.what());
        }
        g_bridge.reset();
        return false;
    }
}
