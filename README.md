# REFramework-MCP

[English](README.md) | [简体中文](docs/README.zh-CN.md) | [繁體中文](docs/README.zh-TW.md)

REFramework-MCP connects an MCP client to a running RE Engine game through
[REFramework](https://github.com/praydog/REFramework). It indexes REFramework
metadata and existing Lua MODs, searches types and members, builds access paths,
and runs bounded live probes through an in-process bridge.

Version 1.0.0 targets the official REF Nightly
nightly-01397-684ca77369ec1050e844e8651a9b1d5b7c5aa370. REF Nightly is the
only upstream release channel used by this project. The Python host is the main
application. C++ is limited to the game-process bridge and two small,
versioned REFramework service ABIs.

The detailed design is documented in
[C2 Architecture v1.0.0](docs/C2-Architecture-v1.0.0.zh-CN.md).

## Components

- Python MCP host: metadata index, MOD usage index, four-graph search,
  AccessPlan generation, policy, approvals, and audit log.
- REFramework-MCP bridge: a Windows named-pipe plugin that owns opaque
  ObjectRefs and schedules runtime work on the game thread.
- Export Service: adds asynchronous JSON or SDK plus JSON generation to
  REFramework. This is what powers run_generate_sdk.
- Probe Service: compiles and runs isolated, resource-limited Lua probes.

The server never uses a raw address as an object identity. Runtime objects are
returned as short-lived ObjectRefs bound to one runtime epoch.

## Requirements

- Windows 10 or 11
- A supported RE Engine game

Source builds additionally require:

- Python 3.11 through 3.13
- Visual Studio 2022 with Desktop development with C++
- CMake 3.25 or newer
- A recursive checkout of the verified REF Nightly source commit

## Install

For a packaged build, download the single
reframework-mcp-1.0.0-ref-nightly-01397-windows-x64.zip artifact. Extract it,
then copy the contents of its game directory into the actual game directory.
This installs the aligned dinput8.dll and
reframework/plugins/reframework_mcp.dll. Back up an existing dinput8.dll before
replacing it if you need to restore a different REFramework build.

Double-click REFramework-MCP.exe. It opens a console, prints logs, and serves
MCP at http://127.0.0.1:8765/mcp. Closing the window or pressing Ctrl+C stops
the server. Python, a GUI, and a configuration file are not required for the
packaged build. The source-adapter directory is for audit and source builds and
is not copied into the game.

The remaining steps describe a source installation.

### 1. Install the Python host

From this repository:

~~~powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
~~~

No configuration file is required. The default is local stdio, the named pipe
\`\\.\pipe\reframework-mcp-v1\`, and data under
\`%LOCALAPPDATA%\REFramework-MCP\`. Copy \`config.example.toml\` to
\`config.toml\` only when you need to override storage, transport, policy, or
MOD roots.

### 2. Add the Export and Probe services to REFramework

Use a clean checkout at the verified REF Nightly commit. The normal installer
refuses a different commit. The guarded cross-Nightly mode is reserved for the
full REF Nightly Alignment workflow.

~~~powershell
git clone --recursive https://github.com/praydog/REFramework.git C:\Code\REFramework
git -C C:\Code\REFramework checkout 684ca77369ec1050e844e8651a9b1d5b7c5aa370
git -C C:\Code\REFramework submodule update --init --recursive

.\reframework\export_service\install.ps1 -REFrameworkRoot C:\Code\REFramework
cmake -S C:\Code\REFramework -B C:\Code\REFramework\build -G "Visual Studio 17 2022" -A x64
cmake --build C:\Code\REFramework\build --config Release --target REFramework
~~~

Copy C:\Code\REFramework\build\bin\REFramework\dinput8.dll to the game
directory as you would for a normal REFramework installation.

### 3. Build and install the bridge plugin

~~~powershell
cmake -S . -B out\build -G "Visual Studio 17 2022" -A x64 -DREFRAMEWORK_ROOT=C:\Code\REFramework
cmake --build out\build --config Release --target reframework_mcp
cmake --install out\build --config Release --prefix "C:\Games\YourGame"
~~~

The last command installs reframework_mcp.dll under
reframework\plugins in the chosen game directory.

Release archives are built by GitHub Actions. This repository does not use
checked-in or manually assembled release packages as the source of truth.

## Connect an MCP client

For the packaged build, double-click the EXE for local HTTP:

~~~powershell
.\REFramework-MCP.exe
~~~

Start the game, then use a terminal to check the Bridge:

~~~powershell
.\REFramework-MCP.exe doctor
~~~

For a client-owned stdio process, point the MCP client at the same EXE and pass
serve. Use an absolute path:

~~~json
{
  "mcpServers": {
    "reframework": {
      "command": "C:\\Tools\\REFramework-MCP\\REFramework-MCP.exe",
      "args": ["serve"]
    }
  }
}
~~~

Explicit command-line arguments retain the normal CLI behavior; serve defaults
to stdio. To choose local HTTP explicitly:

~~~powershell
.\REFramework-MCP.exe serve --transport streamable-http --host 127.0.0.1 --port 8765
~~~

An optional config.toml next to the packaged EXE is loaded automatically.
Do not expose the HTTP endpoint outside the local machine. Runtime tools can
inspect and, after approval, modify game state.

## Recommended workflow

1. Call runtime_status and check the bridge, game, TDB fingerprint, runtime
   epoch, policies, and active metadata snapshot.
2. Call run_generate_sdk with mode json_only. Poll the returned export resource
   until the snapshot is indexed and active.
3. Index representative Lua MODs with the CLI, then use search_members and
   search_usage_examples. Exact signatures, overloads, parameter and return
   types, known MOD chains, and runtime observations are ranked together.
4. Use find_access_paths to create AccessPlan candidates and
   validate_access_plan to check a chosen plan node by node.
5. Use draft_lua_probe, validate_lua_probe, and run_lua_probe for bounded
   runtime exploration.
6. Use observation hooks to collect missing dynamic edges. invoke_method,
   set_field, transform hooks, and windowed hook tests require policy approval;
   method and field mutations also require a current live plan validation.

run_generate_sdk is asynchronous. The JSON mode is enough for search and graph
construction; sdk_and_json also writes the normal REFramework SDK output.
Using policy force requires a short-lived approval bound to the exact request.

## Tools

| Tool | Purpose |
|---|---|
| runtime_status | Report bridge, runtime, export, snapshot, graph, and policy state |
| run_generate_sdk | Start or reuse an asynchronous REFramework metadata export |
| search_types | Search types in an indexed snapshot |
| describe_type | Return a type, members, inheritance, generic, RSZ, and coverage data |
| search_members | Search exact overloads and fuse static, MOD, bounded reachability, and live evidence |
| find_type_dependencies | Traverse static type and member dependencies |
| list_singletons | List live managed and native singleton roots |
| inspect_object | Inspect bounded fields, allowlisted getters, child ObjectRefs, and storage views |
| search_usage_examples | Search syntax-aware usage extracted from Lua MOD projects |
| find_access_paths | Rank multi-root, typed AccessPlan DAGs from four graphs |
| validate_access_plan | Validate an AccessPlan symbolically and against the current runtime |
| draft_lua_probe | Draft REFramework Lua from a selected AccessPlan |
| validate_lua_probe | Check syntax, symbols, risk, lifecycle, and optional live compilation |
| invoke_method | Invoke an exact method after live-plan validation and approval |
| set_field | Write an exact field after live-plan validation and approval |
| run_lua_probe | Run a validated one-shot or windowed isolated probe |
| install_hook | Install a bounded observation or approved transform hook |
| remove_hook | Idempotently remove an owned hook and archive its events |

## Resources

Large or persistent results are exposed through these URI templates:

- reframework://metadata/exports/{job_ref}
- reframework://metadata/snapshots/{snapshot_id}/manifest
- reframework://metadata/snapshots/{snapshot_id}/coverage
- reframework://explorations/{exploration_id}/graph
- reframework://access-plans/{plan_ref}
- reframework://access-plans/{plan_ref}/validation
- reframework://usage/{usage_ref}
- reframework://hooks/{hook_ref}/events
- reframework://probes/{probe_ref}/events

Use current as the exploration_id for the connected runtime epoch. A usage_ref
can be usage:{usage_pk} or a usage project ID.

## Offline commands

~~~powershell
reframework-mcp import-dump C:\path\to\il2cpp_dump.json --manifest C:\path\to\manifest.json
reframework-mcp index-mod C:\Mods\Example --game-id mhwilds
reframework-mcp serve --index-configured-mods
~~~

The MOD indexer records type lookups, fields, method calls, hooks, bindings, and
chain order. Give it real, working MOD projects when you want access-path
ranking to reflect established game-specific patterns.

## Runtime safety and current limits

- Mutation approvals are short-lived and bound to the action, complete argument
  hash, runtime epoch, and plan validation reference.
- invoke_method and set_field reject missing, offline, expired, or stale-epoch
  AccessPlan validations.
- Getter execution is off by default. inspect_object invokes only zero-argument
  getters named in getter_allowlist.
- Hook and probe queues, lifetimes, event counts, instructions, frames, and
  output bytes are bounded.
- ObjectRefs expire and raw pointers are not sent through MCP, IPC responses,
  probe output, or logs.
- The public REFramework plugin API does not provide a bounds-checked managed
  array reader. Version 1.0.0 therefore reports direct System.Array paging as
  unavailable and exposes field-backed List and Dictionary storage views
  without assuming private layouts. Use a validated Lua probe when direct array
  element values are required.
- REF Nightly is the only production upstream. A newer official Nightly is
  supported only after the scheduled or manually triggered alignment workflow
  passes the patch, full REF build, bridge build, ABI export, and package gates.
- Access-path discovery uses bounded reverse multi-root traversal. Results
  report whether the traversal was truncated; use a narrower root or target
  when a very dense graph reaches the exploration budget.

## Development

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m build

cmake --build out\build --config Release --target reframework_mcp reframework_export_service_syntax reframework_probe_service_syntax
~~~

The 18 input and output contracts are generated by scripts\export_schemas.py
and protected by schemas\tool-contracts-v1.sha256. Tests include representative
MHST3 and MHWS MOD access-chain fixtures.

REF Nightly Alignment runs on a schedule and can also be started manually from
GitHub Actions. Manual runs may target the latest official Nightly or a specific
official nightly tag, and can force a cached target to rebuild.

## Live validation

On 2026-08-28, the complete 18-tool and 9-resource contract was exercised
against local REFramework installations for Monster Hunter Stories 3 and
Monster Hunter Wilds. Real \`json_only\` exports produced:

- MHST3: 158,211 types, 1,574,747 members, and 2,808,263 type edges.
- MHWilds: 322,054 types, 3,428,218 members, and 5,915,472 type edges.

The live checks covered bridge negotiation, singleton enumeration, object
inspection, isolated Lua compilation/execution, SDK export/reuse, type/member
search, and dependency traversal. On the 8.36 GB MHWilds index, three complete
stdio \`search_members\` calls for \`app.GA\` completed in 1.54-2.44 seconds on
the test machine. Mutating method/field operations and transform hooks were not
executed against user game state; their approval and validation gates are
covered by automated tests.

## License

MIT. REFramework is a separate project with its own license.
