function(refmcp_read_project_version output_variable version_file)
    if(NOT EXISTS "${version_file}")
        message(FATAL_ERROR "REFramework-MCP version source is missing: ${version_file}")
    endif()

    file(READ "${version_file}" version_source)
    string(
        REGEX MATCH
        "__version__[ ]*=[ ]*\"([0-9]+\\.[0-9]+\\.[0-9]+)\""
        version_match
        "${version_source}"
    )
    if(NOT version_match)
        message(FATAL_ERROR "Unable to read a numeric project version from ${version_file}")
    endif()

    set(${output_variable} "${CMAKE_MATCH_1}" PARENT_SCOPE)
endfunction()

function(refmcp_configure_version_header version generated_include_dir)
    set(REFMCP_VERSION "${version}")
    file(MAKE_DIRECTORY "${generated_include_dir}/reframework_mcp")
    configure_file(
        "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/version.hpp.in"
        "${generated_include_dir}/reframework_mcp/version.hpp"
        @ONLY
    )
endfunction()
