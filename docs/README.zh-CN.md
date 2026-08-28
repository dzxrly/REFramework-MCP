# REFramework-MCP

[English](../README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md)

REFramework-MCP 通过
[REFramework](https://github.com/praydog/REFramework) 将 MCP 客户端连接到
运行中的 RE Engine 游戏。它可以索引 REFramework 元数据和现有 Lua MOD，查询
类型与成员、构建访问路径，并通过进程内 Bridge 执行有边界的实时探针。

1.0.0 版适配官方 REF Nightly
nightly-01397-684ca77369ec1050e844e8651a9b1d5b7c5aa370。REF Nightly 是本项目
唯一使用的上游发布渠道。Python Host 是主程序；C++ 只负责游戏进程内 Bridge，
以及两个小型、版本化的 REFramework Service ABI。

详细设计见 [C2 技术架构 v1.0.0](C2-Architecture-v1.0.0.zh-CN.md)。

## 组成

- Python MCP Host：元数据索引、MOD 用法索引、四图融合搜索、AccessPlan
  生成、权限策略、审批和审计。
- REFramework-MCP Bridge：Windows Named Pipe 插件，负责持有不透明
  ObjectRef，并把运行时任务调度到游戏线程。
- Export Service：为 REFramework 增加异步 JSON 或 SDK 加 JSON 导出，
  run_generate_sdk 由此实现。
- Probe Service：编译并运行隔离且受资源限制的 Lua Probe。

服务器不使用原始地址作为对象身份。运行时对象以短期 ObjectRef 返回，并绑定到
单个 runtime epoch。

## 环境要求

- Windows 10 或 11
- 受支持的 RE Engine 游戏

源码构建还需要：

- Python 3.11 至 3.13
- Visual Studio 2022，安装“使用 C++ 的桌面开发”
- CMake 3.25 或更高版本
- 递归克隆且位于已验证 REF Nightly 源提交的 REFramework

## 安装

若使用打包版本，只需下载
reframework-mcp-1.0.0-ref-nightly-01397-windows-x64.zip。解压后，把 game
目录中的内容复制到实际游戏目录；这会安装已对齐的 dinput8.dll 和
reframework/plugins/reframework_mcp.dll。如果以后需要恢复另一个
REFramework 构建，请先备份游戏目录中已有的 dinput8.dll。

随后双击 REFramework-MCP.exe。它会打开控制台输出日志，并在
http://127.0.0.1:8765/mcp 提供 MCP；关闭窗口或按 Ctrl+C 即停止服务器。
打包版不要求用户安装 Python，也不需要 GUI 或配置文件。source-adapter 仅用于
审计和源码构建，不复制到游戏目录。

以下步骤说明源码安装方式。

### 1. 安装 Python Host

在本项目目录执行：

~~~powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
~~~

默认无需配置文件：使用本地 stdio、Named Pipe
\`\\.\pipe\reframework-mcp-v1\`，数据写入
\`%LOCALAPPDATA%\REFramework-MCP\`。仅当需要修改存储、传输、权限策略或
MOD 目录时，才把 \`config.example.toml\` 复制为 \`config.toml\`。

### 2. 向 REFramework 加入 Export 和 Probe Service

请使用位于已验证 REF Nightly 提交的干净仓库。常规安装脚本会拒绝其他提交。
带守卫的跨 Nightly 模式只供完整的 REF Nightly Alignment 工作流使用。

~~~powershell
git clone --recursive https://github.com/praydog/REFramework.git C:\Code\REFramework
git -C C:\Code\REFramework checkout 684ca77369ec1050e844e8651a9b1d5b7c5aa370
git -C C:\Code\REFramework submodule update --init --recursive

.\reframework\export_service\install.ps1 -REFrameworkRoot C:\Code\REFramework
cmake -S C:\Code\REFramework -B C:\Code\REFramework\build -G "Visual Studio 17 2022" -A x64
cmake --build C:\Code\REFramework\build --config Release --target REFramework
~~~

按普通 REFramework 安装方式，将
C:\Code\REFramework\build\bin\REFramework\dinput8.dll 复制到游戏目录。

### 3. 构建并安装 Bridge 插件

~~~powershell
cmake -S . -B out\build -G "Visual Studio 17 2022" -A x64 -DREFRAMEWORK_ROOT=C:\Code\REFramework
cmake --build out\build --config Release --target reframework_mcp
cmake --install out\build --config Release --prefix "C:\Games\YourGame"
~~~

最后一条命令会把 reframework_mcp.dll 安装到所选游戏目录下的
reframework\plugins。

发布压缩包只由 GitHub Actions 构建。本项目不把手工拼装或提交到仓库中的包作为
发布来源。

## 连接 MCP 客户端

打包版双击 EXE 即以本地 HTTP 模式启动：

~~~powershell
.\REFramework-MCP.exe
~~~

启动游戏后，可在终端检查 Bridge：

~~~powershell
.\REFramework-MCP.exe doctor
~~~

若 MCP 客户端负责启动 stdio 进程，让它指向同一个 EXE 并传入 serve。请使用
绝对路径：

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

显式传入命令行参数时保留普通 CLI 语义；serve 默认使用 stdio。也可显式选择
本地 HTTP：

~~~powershell
.\REFramework-MCP.exe serve --transport streamable-http --host 127.0.0.1 --port 8765
~~~

打包版会自动加载 EXE 同目录下可选的 config.toml。
不要把 HTTP 端点暴露到本机之外。运行时工具能够读取游戏状态，并可在审批后修改
游戏状态。

## 推荐工作流

1. 调用 runtime_status，检查 Bridge、游戏、TDB 指纹、runtime epoch、
   权限策略和活动元数据快照。
2. 以 json_only 模式调用 run_generate_sdk。轮询返回的导出 Resource，直至
   快照完成索引并激活。
3. 使用 CLI 索引有代表性的 Lua MOD，再调用 search_members 和
   search_usage_examples。精确签名、重载、参数与返回类型、已知 MOD 链路和
   实时观测会一起参与排序。
4. 使用 find_access_paths 生成 AccessPlan 候选，再通过
   validate_access_plan 逐节点验证所选方案。
5. 依次使用 draft_lua_probe、validate_lua_probe 和 run_lua_probe 进行
   有边界的运行时探索。
6. 使用观察 Hook 补充动态关系。invoke_method、set_field、变换 Hook 和
   windowed Hook 测试需要策略审批；方法调用和字段写入还要求当前 runtime
   epoch 下通过实时验证的 AccessPlan。

run_generate_sdk 是异步操作。json_only 已足够支持搜索和图构建；
sdk_and_json 还会写出常规 REFramework SDK。policy 设为 force 时，需要与
完整请求参数绑定的短期 approval_ref。

## 工具

| Tool | 用途 |
|---|---|
| runtime_status | 返回 Bridge、运行时、导出、快照、图和权限状态 |
| run_generate_sdk | 启动或复用异步 REFramework 元数据导出 |
| search_types | 在已索引快照中搜索类型 |
| describe_type | 返回类型、成员、继承、泛型、RSZ 和覆盖信息 |
| search_members | 搜索精确重载，融合静态、MOD、有界可达性和实时证据 |
| find_type_dependencies | 遍历静态类型和成员依赖 |
| list_singletons | 列出实时 Managed 和 Native Singleton 根 |
| inspect_object | 有界检查字段、allowlist getter、子 ObjectRef 和存储视图 |
| search_usage_examples | 搜索从 Lua MOD 提取的语法级用法 |
| find_access_paths | 从四张图生成并排序多根、强类型 AccessPlan DAG |
| validate_access_plan | 离线并结合当前运行时验证 AccessPlan |
| draft_lua_probe | 根据所选 AccessPlan 生成 REFramework Lua 草稿 |
| validate_lua_probe | 检查语法、符号、风险、生命周期和可选实时编译 |
| invoke_method | 在实时 Plan 验证和审批后调用精确方法 |
| set_field | 在实时 Plan 验证和审批后写入精确字段 |
| run_lua_probe | 运行已验证的一次性或限时隔离 Probe |
| install_hook | 安装有边界的观察 Hook 或经审批的变换 Hook |
| remove_hook | 幂等移除本服务器持有的 Hook 并封存事件 |

## Resources

大型或需要持久读取的结果通过以下 URI 模板提供：

- reframework://metadata/exports/{job_ref}
- reframework://metadata/snapshots/{snapshot_id}/manifest
- reframework://metadata/snapshots/{snapshot_id}/coverage
- reframework://explorations/{exploration_id}/graph
- reframework://access-plans/{plan_ref}
- reframework://access-plans/{plan_ref}/validation
- reframework://usage/{usage_ref}
- reframework://hooks/{hook_ref}/events
- reframework://probes/{probe_ref}/events

将 exploration_id 设为 current 可读取当前连接的 runtime epoch。
usage_ref 可以是 usage:{usage_pk}，也可以是用法项目 ID。

## 离线命令

~~~powershell
reframework-mcp import-dump C:\path\to\il2cpp_dump.json --manifest C:\path\to\manifest.json
reframework-mcp index-mod C:\Mods\Example --game-id mhwilds
reframework-mcp serve --index-configured-mods
~~~

MOD 索引器会记录类型查询、字段、方法调用、Hook、变量绑定和链路顺序。若希望
访问路径排序反映某个游戏中已经验证的做法，请输入真实可用的 MOD 项目。

## 运行时安全与当前边界

- 写操作审批有效期很短，并绑定操作、完整参数哈希、runtime epoch 和 Plan
  验证引用。
- invoke_method 和 set_field 会拒绝缺失、仅离线、已过期或属于旧 epoch 的
  AccessPlan 验证。
- Getter 默认不执行。inspect_object 只调用 getter_allowlist 中明确列出的
  零参数 getter。
- Hook 与 Probe 的队列、生命周期、事件数、指令数、帧数和输出字节均有限制。
- ObjectRef 会过期；原始指针不会进入 MCP、IPC 响应、Probe 输出或日志。
- REFramework 公共插件 API 目前没有带边界检查的托管数组读取接口。因此
  1.0.0 会明确报告 System.Array 直接分页不可用，只通过公共 Field API 暴露
  List 与 Dictionary 的后备存储视图，不猜测私有内存布局。需要直接读取数组
  元素时，应改用已验证的 Lua Probe。
- REF Nightly 是唯一生产上游。新的官方 Nightly 只有在定时或手动触发的对齐
  工作流通过补丁、完整 REF 构建、Bridge 构建、ABI 导出和打包门槛后才列为支持。
- 访问路径发现采用有界的反向多根遍历。结果会报告遍历是否被截断；如果高密度图
  达到探索预算，应缩小根或目标范围。

## 开发

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m build

cmake --build out\build --config Release --target reframework_mcp reframework_export_service_syntax reframework_probe_service_syntax
~~~

18 个工具的输入输出契约由 scripts\export_schemas.py 生成，并由
schemas\tool-contracts-v1.sha256 锁定。测试包含具有代表性的 MHST3 与 MHWS
MOD 访问链样例。

REF Nightly Alignment 会定时运行，也可在 GitHub Actions 中手动触发。手动
运行可以检测当前最新官方 Nightly，也可以指定某个官方 nightly tag，并可强制
重建已经命中成功缓存的目标。

## 实机验证

2026-08-28 已在本地安装 REFramework 的 Monster Hunter Stories 3 与
Monster Hunter Wilds 上验证完整的 18 Tool / 9 Resource 契约。真实
\`json_only\` 导出结果为：

- MHST3：158,211 个类型、1,574,747 个成员、2,808,263 条类型边。
- MHWilds：322,054 个类型、3,428,218 个成员、5,915,472 条类型边。

实机测试覆盖 Bridge 协商、Singleton 枚举、对象检查、隔离 Lua 编译与执行、
SDK 导出/复用、类型与成员搜索、依赖遍历。在 8.36 GB 的 MHWilds 索引上，
测试机连续三次通过 stdio 调用 \`search_members(app.GA)\` 的耗时为
1.54–2.44 秒。为避免改变用户游戏状态，实机测试没有执行方法/字段写操作和
变换 Hook；其审批与验证门由自动化测试覆盖。

## 许可证

MIT。REFramework 是独立项目，使用其自身许可证。
