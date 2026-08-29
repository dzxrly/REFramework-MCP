# REFramework-MCP

[English](../README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md)

REFramework-MCP 让 MCP 客户端通过
[REFramework](https://github.com/praydog/REFramework) 查询 RE Engine
元数据、检查游戏内对象并测试 Lua 代码。它主要服务于 MOD 作者，减少在 Object
Explorer 中反复翻找类型、成员和调用链的时间。

本项目只跟随 REF Nightly。

## 安装

1. 从 [Releases](https://github.com/dzxrly/REFramework-MCP/releases)
   下载最新的 Windows ZIP。
2. 解压后，将 game 目录内的文件复制到游戏目录。请先备份已有的
   dinput8.dll。
3. 双击 REFramework-MCP.exe。

EXE 会打开控制台，并在 http://127.0.0.1:9966/mcp 提供 MCP。关闭窗口或按
Ctrl+C 即可停止。打包版不需要另装 Python，也没有 GUI 或必填配置文件。

每个 Release 的名称都带有 REF Nightly 编号、源码提交和 8 位构建 ID。

## 连接 MCP 客户端

支持 Streamable HTTP 的客户端可直接连接 http://127.0.0.1:9966/mcp 。

如果客户端通过 stdio 启动服务器，让它指向同一个 EXE：

~~~json
{
  "mcpServers": {
    "reframework": {
      "command": "<REFramework-MCP.exe 的绝对路径>",
      "args": ["serve"]
    }
  }
}
~~~

使用实时工具前先启动游戏。以下命令可检查游戏内 Bridge：

~~~powershell
.\REFramework-MCP.exe doctor
~~~

如需修改端口、数据目录、权限或 MOD 目录，可在 EXE 同目录放置
config.toml。HTTP 监听地址请保持为 127.0.0.1。

## 常用 MOD 工作流

1. 调用 runtime_status。
2. 以 json_only 模式调用 run_generate_sdk，等待快照完成索引。
3. 使用 search_types、describe_type、search_members 和
   find_type_dependencies 找到所需 API。
4. 索引已经能工作的 Lua MOD，再使用 search_usage_examples 和
   find_access_paths 复用现有调用链。
5. 元数据不足时，生成、验证并运行 Lua Probe。

invoke_method、set_field 和修改行为的 Hook 需要审批。游戏会话变化后，旧的
运行时对象引用会失效。

## MCP 工具

- 元数据与对象：runtime_status、run_generate_sdk、search_types、
  describe_type、search_members、find_type_dependencies、list_singletons、
  inspect_object
- 调用链：search_usage_examples、find_access_paths、validate_access_plan
- Lua 与运行时操作：draft_lua_probe、validate_lua_probe、run_lua_probe、
  invoke_method、set_field、install_hook、remove_hook

## 离线索引

~~~powershell
.\REFramework-MCP.exe import-dump <dump.json> --manifest <manifest.json>
.\REFramework-MCP.exe index-mod <MOD目录> --game-id <游戏ID>
~~~

需要让调用链排序贴近某款游戏时，请索引真实可用的 MOD 项目。

## 从源码构建

源码构建需要 Python 3.11、安装了 C++ 桌面开发工具的 Visual Studio 2022、
CMake 3.25 或更高版本，以及递归克隆的 REFramework。

~~~powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,bundle]"

$refSource = (Resolve-Path "..\REFramework").Path
$baseline = Get-Content .\reframework\nightly-baseline.json -Raw | ConvertFrom-Json
git -C $refSource checkout $baseline.commit
git -C $refSource submodule update --init --recursive

.\reframework\export_service\install.ps1 -REFrameworkRoot $refSource
cmake -S $refSource -B .tmp\ref-build -G "Visual Studio 17 2022" -A x64
cmake --build .tmp\ref-build --config Release --target REFramework

cmake -S . -B out\build -G "Visual Studio 17 2022" -A x64 "-DREFRAMEWORK_ROOT=$refSource"
cmake --build out\build --config Release --target reframework_mcp
cmake --install out\build --config Release --prefix "<游戏目录>"
~~~

项目检查命令：

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m build
~~~

## 发布

仓库只保留一个 GitHub Actions 工作流。它每天两次检查官方 REF Nightly，也可
手动运行。发现新 Nightly 后，会完成测试、构建、打包并创建 GitHub Release；
手动运行还可以指定 Nightly tag 或强制再次发布。

## 许可证

MIT。REFramework 是独立项目，使用其自身许可证。
