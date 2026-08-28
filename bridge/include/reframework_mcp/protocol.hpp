#pragma once

#include <cstddef>
#include <string_view>

namespace reframework_mcp {

inline constexpr std::string_view kProtocolVersion{"1.0"};
inline constexpr std::string_view kBridgeVersion{"1.0.0"};
inline constexpr std::wstring_view kDefaultPipeName{LR"(\\.\pipe\reframework-mcp-v1)"};
inline constexpr std::size_t kMaxFrameBytes{16u * 1024u * 1024u};

} // namespace reframework_mcp
