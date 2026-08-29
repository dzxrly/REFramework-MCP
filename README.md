# REFramework-MCP

[English](README.md) | [简体中文](docs/README.zh-CN.md) | [繁體中文](docs/README.zh-TW.md)

REFramework-MCP lets an MCP client search RE Engine metadata, inspect live game
objects, and test Lua code through
[REFramework](https://github.com/praydog/REFramework). It is intended for MOD
authors who would otherwise spend a lot of time tracing types and calls in
Object Explorer.

REF Nightly is the only REFramework release channel used by this project.

## Install

1. Download the newest Windows ZIP from
   [Releases](https://github.com/dzxrly/REFramework-MCP/releases).
2. Extract it and copy everything inside the game directory into the game
   directory. Back up an existing dinput8.dll first.
3. Double-click REFramework-MCP.exe.

The EXE opens a console and serves MCP at http://127.0.0.1:9966/mcp. Close the
window or press Ctrl+C to stop it. The packaged build does not need Python, a
GUI, or a config file.

Each release name includes its MCP version, REF Nightly number, and source
commit.

## Connect an MCP client

Use http://127.0.0.1:9966/mcp when the client supports Streamable HTTP.

For a client-owned stdio process, point it to the same EXE:

~~~json
{
  "mcpServers": {
    "reframework": {
      "command": "<absolute-path-to-REFramework-MCP.exe>",
      "args": ["serve"]
    }
  }
}
~~~

Start the game before using live tools. This command checks the game-side
bridge:

~~~powershell
.\REFramework-MCP.exe doctor
~~~

An optional config.toml next to the EXE can override the port, storage paths,
policies, and MOD roots. Keep the HTTP listener on 127.0.0.1.

## Typical MOD workflow

1. Call runtime_status.
2. Call run_generate_sdk with mode json_only and wait for the snapshot to
   finish indexing.
3. Use search_types, describe_type, search_members, and
   find_type_dependencies to find the APIs you need.
4. Index working Lua MODs, then use search_usage_examples and find_access_paths
   to reuse known call chains.
5. Draft, validate, and run a Lua probe when metadata alone is not enough.

invoke_method, set_field, and transforming hooks require approval. Runtime
object references expire when the game session changes.

## MCP tools

- Metadata and objects: runtime_status, run_generate_sdk, search_types,
  describe_type, search_members, find_type_dependencies, list_singletons,
  inspect_object
- Call chains: search_usage_examples, find_access_paths, validate_access_plan
- Lua and runtime actions: draft_lua_probe, validate_lua_probe, run_lua_probe,
  invoke_method, set_field, install_hook, remove_hook

## Offline indexing

~~~powershell
.\REFramework-MCP.exe import-dump <dump.json> --manifest <manifest.json>
.\REFramework-MCP.exe index-mod <mod-directory> --game-id <game-id>
~~~

Index real MOD projects when you want call-chain ranking to reflect code that
already works for a game.

## Build from source

Source builds require Python 3.11, Visual Studio 2022 with C++ desktop tools,
CMake 3.25 or newer, and a recursive REFramework checkout.

~~~powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,bundle]"

$projectRoot = (Resolve-Path ".").Path
$refSource = (Resolve-Path "..\REFramework").Path
$nightly = Invoke-RestMethod "https://api.github.com/repos/praydog/REFramework-nightly/releases/latest"
$match = [regex]::Match([string] $nightly.tag_name, "^nightly-[0-9]+-(?<sha>[0-9a-f]{40})$")
if (-not $match.Success) { throw "Unexpected REF Nightly tag: $($nightly.tag_name)" }
$refCommit = $match.Groups["sha"].Value
git -C $refSource fetch origin $refCommit
git -C $refSource checkout $refCommit
git -C $refSource submodule update --init --recursive

cmake -S $projectRoot -B out\build -G "Visual Studio 17 2022" -A x64 "-DREFRAMEWORK_ROOT=$refSource"
cmake --build out\build --config Release --target reframework_export_service_hostile_host_syntax

.\reframework\export_service\install.ps1 -REFrameworkRoot $refSource
$injection = Join-Path $projectRoot "reframework\cmake\InjectServices.cmake"
cmake -S $refSource -B .tmp\ref-build -G "Visual Studio 17 2022" -A x64 "-DREFMCP_PROJECT_ROOT=$projectRoot" "-DCMAKE_PROJECT_INCLUDE=$injection"
cmake --build .tmp\ref-build --config Release --target REFramework

cmake --build out\build --config Release --target reframework_mcp reframework_export_service_syntax reframework_probe_service_syntax
cmake --install out\build --config Release --prefix "<game-directory>"
~~~

Run the project checks with:

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m build
~~~

## Releases

GitHub Actions checks the latest official REF Nightly once per day at 08:30
Beijing time. The Windows compatibility job adapts and compiles REF before the
separate Python test and packaging job starts. A compatibility failure leaves
the target unpublished and it is retried on the next scheduled run. Manual
runs can select a Nightly tag or force a rebuild without creating a duplicate
release.

## License

MIT. REFramework is a separate project with its own license.
