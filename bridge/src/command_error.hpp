#pragma once
#include <nlohmann/json.hpp>
#include <stdexcept>
#include <string>
#include <utility>

namespace reframework_mcp {
class BridgeError final : public std::runtime_error {
public:
    BridgeError(std::string error_code, std::string message,
        nlohmann::json error_details = nlohmann::json::object())
        : std::runtime_error{message}, code{std::move(error_code)}, details(std::move(error_details)) {}
    std::string code;
    nlohmann::json details;
};
}
