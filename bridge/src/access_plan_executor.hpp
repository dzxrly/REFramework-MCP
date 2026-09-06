#pragma once
#include <nlohmann/json.hpp>
#include <string>
#include <unordered_map>

namespace reframework_mcp {
class Bridge;
class AccessPlanExecutor {
public:
    explicit AccessPlanExecutor(Bridge& bridge) : b{bridge} {}
    nlohmann::json execute(const nlohmann::json& payload);
private:
    friend class Bridge;
    using json = nlohmann::json;
    Bridge& b;
    std::unordered_map<std::string, json> values;
    json evaluate(const json& node, const json& input, const json& payload);
    json resolve_root(const json& root);
    json call(const json& node, const json& input, const json& payload);
    json read_field(const json& member, const json& input);
    void check_type(const json& value, const std::string& expected, bool nullable = false);
    json collection(const json& input, std::size_t offset, std::size_t limit);
};
}
