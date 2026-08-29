cmake_minimum_required(VERSION 3.25)

set(refmcp_project_root "${CMAKE_CURRENT_SOURCE_DIR}/..")
include("${refmcp_project_root}/cmake/ReadProjectVersion.cmake")
if(NOT DEFINED REFMCP_VERSION)
    refmcp_read_project_version(
        REFMCP_VERSION
        "${refmcp_project_root}/src/reframework_mcp/_version.py"
    )
endif()
if(NOT DEFINED REFMCP_GENERATED_INCLUDE_DIR)
    set(REFMCP_GENERATED_INCLUDE_DIR "${CMAKE_CURRENT_BINARY_DIR}/generated/include")
endif()
refmcp_configure_version_header(
    "${REFMCP_VERSION}"
    "${REFMCP_GENERATED_INCLUDE_DIR}"
)
project(reframework_mcp_bridge VERSION "${REFMCP_VERSION}" LANGUAGES CXX)

if(NOT WIN32)
    message(FATAL_ERROR "The REFramework-MCP Bridge only supports Windows")
endif()

set(REFRAMEWORK_ROOT "" CACHE PATH "Path to a REFramework source checkout")
if(NOT REFRAMEWORK_ROOT)
    message(FATAL_ERROR "Set REFRAMEWORK_ROOT to a REFramework source checkout")
endif()
if(NOT EXISTS "${REFRAMEWORK_ROOT}/include/reframework/API.hpp")
    message(FATAL_ERROR "REFRAMEWORK_ROOT does not contain include/reframework/API.hpp")
endif()
if(NOT EXISTS "${REFRAMEWORK_ROOT}/dependencies/nlohmann/json.hpp")
    message(FATAL_ERROR "REFRAMEWORK_ROOT does not contain dependencies/nlohmann/json.hpp")
endif()

add_library(reframework_mcp SHARED
    src/bridge.cpp
    src/export_adapter.cpp
    src/hook_manager.cpp
    src/named_pipe_server.cpp
    src/object_registry.cpp
    src/plugin.cpp
    src/probe_adapter.cpp
)

target_compile_features(reframework_mcp PRIVATE cxx_std_20)
target_compile_definitions(reframework_mcp PRIVATE
    WIN32_LEAN_AND_MEAN
    NOMINMAX
    REFRAMEWORK_API_EXCEPTIONS
)
target_include_directories(reframework_mcp
    PUBLIC
        "${CMAKE_CURRENT_SOURCE_DIR}/include"
        "${REFMCP_GENERATED_INCLUDE_DIR}"
    PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/src"
        "${REFRAMEWORK_ROOT}/include"
        "${REFRAMEWORK_ROOT}/dependencies"
)
target_link_libraries(reframework_mcp PRIVATE advapi32 bcrypt)

if(MSVC)
    target_compile_options(reframework_mcp PRIVATE /W4 /permissive- /EHsc /utf-8)
endif()

set_target_properties(reframework_mcp PROPERTIES
    OUTPUT_NAME "reframework_mcp"
    PREFIX ""
)

install(
    TARGETS reframework_mcp
    RUNTIME DESTINATION reframework/plugins
)
