if(NOT CMAKE_CURRENT_SOURCE_DIR STREQUAL CMAKE_SOURCE_DIR)
    return()
endif()

get_property(refmcp_injection_scheduled GLOBAL PROPERTY REFMCP_INJECTION_SCHEDULED SET)
if(refmcp_injection_scheduled)
    return()
endif()
set_property(GLOBAL PROPERTY REFMCP_INJECTION_SCHEDULED TRUE)

if(NOT DEFINED REFMCP_PROJECT_ROOT)
    message(FATAL_ERROR "REFMCP_PROJECT_ROOT must point to the REFramework-MCP checkout")
endif()
cmake_path(ABSOLUTE_PATH REFMCP_PROJECT_ROOT NORMALIZE OUTPUT_VARIABLE refmcp_project_root)

function(refmcp_inject_services)
    if(NOT TARGET REFramework)
        message(FATAL_ERROR "The upstream project did not create the REFramework target")
    endif()

    include("${refmcp_project_root}/cmake/ReadProjectVersion.cmake")
    refmcp_read_project_version(
        REFMCP_VERSION
        "${refmcp_project_root}/src/reframework_mcp/_version.py"
    )
    set(refmcp_generated_include "${CMAKE_BINARY_DIR}/generated/include")
    refmcp_configure_version_header(
        "${REFMCP_VERSION}"
        "${refmcp_generated_include}"
    )

    target_sources(REFramework PRIVATE
        "${refmcp_project_root}/reframework/export_service/ExportServiceHooks.hpp"
        "${refmcp_project_root}/reframework/export_service/ExportServiceV1.cpp"
        "${refmcp_project_root}/reframework/export_service/ExportServiceV1.hpp"
        "${refmcp_project_root}/reframework/probe_service/ProbeServiceHooks.hpp"
        "${refmcp_project_root}/reframework/probe_service/ProbeServiceV1.cpp"
        "${refmcp_project_root}/reframework/probe_service/ProbeServiceV1.hpp"
    )
    target_include_directories(REFramework PRIVATE
        "${refmcp_project_root}/reframework/export_service"
        "${refmcp_project_root}/reframework/probe_service"
        "${refmcp_generated_include}"
    )
    target_link_libraries(REFramework PRIVATE bcrypt)
endfunction()

cmake_language(DEFER DIRECTORY "${CMAKE_SOURCE_DIR}" CALL refmcp_inject_services)
