cmake_minimum_required(VERSION 3.25)
project(REFrameworkMCP VERSION 1.0.1 LANGUAGES CXX)

add_subdirectory(bridge)

option(
    REFMCP_VALIDATE_EXPORT_SERVICE
    "Compile the REFramework-side Export Service overlay as an object target"
    ON
)
if(REFMCP_VALIDATE_EXPORT_SERVICE)
    add_library(reframework_export_service_syntax OBJECT EXCLUDE_FROM_ALL
        reframework/export_service/ExportServiceV1.cpp
    )
    target_compile_features(reframework_export_service_syntax PRIVATE cxx_std_23)
    target_compile_definitions(reframework_export_service_syntax PRIVATE
        WIN32_LEAN_AND_MEAN
        NOMINMAX
        REFRAMEWORK_UNIVERSAL
        TDB_DUMP_ALLOWED
    )
    target_include_directories(reframework_export_service_syntax PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/reframework/export_service"
        "${REFRAMEWORK_ROOT}/dependencies/nlohmann"
    )
    if(MSVC)
        target_compile_options(
            reframework_export_service_syntax
            PRIVATE /W4 /permissive- /EHa /utf-8 /bigobj
        )
    endif()
endif()

option(
    REFMCP_VALIDATE_PROBE_SERVICE
    "Compile the REFramework-side Probe Service overlay as an object target"
    ON
)
if(REFMCP_VALIDATE_PROBE_SERVICE)
    add_library(reframework_probe_service_syntax OBJECT EXCLUDE_FROM_ALL
        reframework/probe_service/ProbeServiceV1.cpp
    )
    target_compile_features(reframework_probe_service_syntax PRIVATE cxx_std_23)
    target_compile_definitions(reframework_probe_service_syntax PRIVATE
        WIN32_LEAN_AND_MEAN
        NOMINMAX
    )
    target_include_directories(reframework_probe_service_syntax PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/reframework/probe_service"
        "${REFRAMEWORK_ROOT}/dependencies/nlohmann"
        "${REFRAMEWORK_ROOT}/dependencies/lua/src"
    )
    if(MSVC)
        target_compile_options(
            reframework_probe_service_syntax
            PRIVATE /W4 /permissive- /EHsc /utf-8
        )
    endif()
endif()
