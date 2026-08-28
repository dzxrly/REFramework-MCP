# REFramework-MCP C2 技术架构

- 文档版本：1.0.0
- 产品版本：1.0.0
- 状态：已实现基线
- 上游发布渠道：praydog/REFramework-nightly
- 已验证 REF Nightly：nightly-01397-684ca77369ec1050e844e8651a9b1d5b7c5aa370

## 1. 决策摘要

REFramework-MCP 采用“外部 Python 主机 + 游戏内窄 C++ 适配层”的 C2 架构。
绝大多数能力，包括 MCP 协议、SQLite 索引、元数据搜索、关系图、AccessPlan、
Lua 草稿、权限策略和审计，都由 Python 实现并冻结为一个 Windows 控制台 EXE。

面向普通用户的发布包只有一个 ZIP。解压后包含：

- REFramework-MCP.exe：双击启动 MCP Host 并输出日志；
- game/dinput8.dll：与该发布包所标注 REF Nightly 对齐的 REFramework；
- game/reframework/plugins/reframework_mcp.dll：游戏内 Bridge；
- compatibility-report.json：REF Nightly tag、commit、适配模式和三个二进制哈希；
- 文档、配置样例、许可证和用于审计的窄适配源码。

用户把 game 目录中的内容复制到游戏目录后，双击 REFramework-MCP.exe 即可。
无参数启动默认监听 http://127.0.0.1:8765/mcp；关闭窗口或按 Ctrl+C 即停止
MCP Server。配置文件不是必需品；若 EXE 同目录存在 config.toml，则自动加载。

无法把整个系统真正压缩成“只有一个外部 EXE”。REFramework 和 Bridge 必须被
游戏进程加载，因此物理上仍需要两个 DLL。一个外部进程不能替代进程内对象访问、
游戏线程调度、Hook 和 Lua 状态管理。

## 2. 目标与非目标

### 2.1 目标

- 让 AI Agent 高效搜索内部类型、精确成员和重载；
- 从类型、成员、现有 MOD 和运行时证据中建立链式访问路径；
- 生成、验证并运行有边界的临时 Lua 探针；
- 支持 run_generate_sdk，并自动导入、校验和激活元数据快照；
- 在审批、runtime epoch 和 AccessPlan 验证约束下执行写操作；
- 把 REFramework 更新影响限制在窄 C++ 适配层；
- 以官方 REF Nightly 作为唯一生产上游；
- 对新 REF Nightly 自动或手动执行完整对齐检测，失败时明确退出。

### 2.2 非目标

- 不把任意裸指针暴露给 MCP 客户端；
- 不允许 Agent 绕过审批执行任意 Lua 或任意内存写入；
- 不用 GUI 复制 Object Explorer；
- 不承诺未知 REF Nightly 在未构建验证时兼容；
- 不把 Action 中仅完成补丁应用视为兼容；完整 REF 构建和 ABI 检查也是硬门槛；
- 不维护一个与 REF Nightly 平行的“REF 正式版”兼容通道。

## 3. 系统上下文

~~~mermaid
flowchart LR
    A[AI Agent / MCP Client] -->|stdio 或本地 HTTP| H[REFramework-MCP.exe]
    H -->|SQLite 查询| D[(metadata.db)]
    H -->|Windows Named Pipe v1| B[reframework_mcp.dll]
    B -->|REFramework Plugin API| R[REF Nightly dinput8.dll]
    B -->|Export Service ABI v1| E[Export Service]
    B -->|Probe Service ABI v1| P[Probe Service]
    R --> G[RE Engine Game]
    E --> O[ObjectExplorer SDK / JSON 导出]
    P --> L[隔离 Lua State]
~~~

所有外部 MCP 流量先进入 Python Host。Bridge 不开放 TCP 端口，只监听本机命名
管道。游戏对象只以短期 ObjectRef 离开游戏进程；ObjectRef 绑定 runtime epoch，
游戏重启、场景切换或注册表失效后不能继续使用。

## 4. 容器与职责

| 容器 | 主要语言 | 进程位置 | 职责 | REF 更新敏感度 |
|---|---|---|---|---|
| Console Host | Python，冻结为 EXE | 外部进程 | MCP、配置、搜索、图、AccessPlan、Lua 草稿、策略、审计 | 低 |
| SQLite Store | SQLite | 外部进程 | 快照、FTS5、类型边、MOD 用法、运行时证据、验证记录 | 低 |
| Bridge | C++ | 游戏进程 | Named Pipe、游戏线程调度、ObjectRef、调用、字段、Hook | 中 |
| Export Service | C++ | REF Nightly 模块内 | 异步调用 ObjectExplorer 导出并报告进度、指纹和结果 | 高但代码很窄 |
| Probe Service | C++ | REF Nightly 模块内 | 独立 Lua State、指令/时间/事件限制、逐帧清理 | 高但代码很窄 |

Python 是主要开发语言。新搜索策略、新图算法、新 MCP 工具、提示生成和大多数安全
策略原则上不需要重新编译 REFramework。只有进程内 ABI、REF 内部调用点或 Bridge
插件发生变化时才需要 C++ 构建。

## 5. 启动与传输模型

### 5.1 双击模式

无参数运行 REFramework-MCP.exe 时：

1. 检查 EXE 同目录是否存在 config.toml；
2. 打印版本、配置来源和 MCP URL；
3. 强制选择 Streamable HTTP；
4. 默认只绑定 127.0.0.1:8765；
5. 在前台输出 Uvicorn、MCP、Bridge、导出和索引日志；
6. 窗口关闭或 Ctrl+C 后完成 shutdown 并退出。

### 5.2 MCP 客户端托管模式

当客户端直接启动 EXE 并传入 serve 时，默认仍使用 stdio。stdio 的标准输出只用于
协议帧，日志必须进入标准错误，不能污染 MCP 消息。显式传入
serve --transport streamable-http 时可切换到 HTTP。

### 5.3 配置发现

配置优先级为：

1. 命令行 --config；
2. 冻结 EXE 同目录 config.toml；
3. REFMCP_ 前缀环境变量；
4. 内置默认值。

默认数据目录是 %LOCALAPPDATA%/REFramework-MCP，数据库、导出快照和审计记录不会
被写入 PyInstaller 的临时解包目录。

## 6. 为什么 1.0.0 仍需重编 REF Nightly

当前公开插件边界不能完整提供 1.0.0 所需的两个能力：

- run_generate_sdk 需要调用 ObjectExplorer::generate_sdk，并读取正在导出、
  当前阶段和进度。现有公开插件 API 没有这个完整服务；
- 隔离 Probe 需要创建和销毁独立 REFramework Lua State、安装资源边界，并在
  游戏帧中驱动和清理。当前实现把这部分封装为版本化 Probe Service。

因此 1.0.0 会对对应 REF Nightly 源码应用一个窄适配并重新构建 dinput8.dll。
改动限制为：

- 向 ObjectExplorer 暴露受互斥保护的 SDK 导出入口及只读进度；
- 注册 ExportServiceV1 和 ProbeServiceV1 源文件；
- 链接 Windows BCrypt；
- 导出 reframework_get_export_service_v1；
- 导出 reframework_get_probe_service_v1。

Bridge 通过 GetProcAddress 动态查找两个符号，并校验 ABI major、minor、
struct_size 和函数表完整性。服务缺失或版本不匹配时只关闭对应能力，不解析私有
C++ 对象布局。

这属于有限侵入，而不是无侵入。长期取消 REF 重编译的唯一可靠路径，是让等价的
SDK 导出和隔离 Lua State 能力进入 REFramework 官方公开插件 ABI；在此之前，
把未经验证的新 dinput8.dll 当作兼容版本是不安全的。

## 7. REF Nightly 对齐策略

REF Nightly 是本项目唯一正式上游。仓库中的
reframework/nightly-baseline.json 记录当前已通过完整验证的 Nightly tag、
release 编号和源 commit。

### 7.1 固定基线路径

常规 CI 和 Package Release 从 baseline 清单读取 commit。安装器要求当前源码
commit 精确相等，然后应用提交绑定的固定补丁。该路径可复现、可审计。

### 7.2 新 Nightly 对齐路径

REF Nightly Alignment Action 每日检查官方
praydog/REFramework-nightly 最新 release，也允许 workflow_dispatch：

- 未指定 tag：检测当前最新官方 Nightly；
- 指定 nightly_tag：手动检测该官方 release；
- force_rebuild：即使成功缓存存在也重新执行。

官方 tag 必须满足 nightly-编号-40位commit。Action 使用 tag 中的 commit 检出
praydog/REFramework 源码及全部子模块。

### 7.3 兼容适配顺序

1. 若 commit 等于已验证基线，应用固定补丁；
2. 对新 Nightly，先尝试同一补丁的上下文应用；
3. 若上下文变化，启用带唯一锚点检查的语义适配；
4. 任一关键文件缺失、锚点为零、锚点重复或 Lua State ABI 消失，立即失败；
5. 不使用模糊猜测、不产生带冲突标记的源码。

语义适配在已验证基线上必须与固定补丁产生字节级相同的四个修改文件。该等价性是
本地回归门槛。

### 7.4 完整通过条件

只有下列步骤全部成功，才上传该 Nightly 对应的整合包：

1. 41 项以上 Python 自动化测试通过；
2. Ruff、格式、Mypy 和冻结 Tool Schema 通过；
3. PyInstaller 单文件 EXE 构建并通过 --version 冒烟测试；
4. Export/Probe 适配应用成功；
5. 完整 REF Nightly 的 REFramework target 构建成功；
6. Bridge 和两个 Service 语法目标构建成功；
7. dinput8.dll 实际导出两个 Service ABI 符号；
8. 生成 compatibility-report.json 和三个二进制 SHA-256；
9. 压缩包与独立 SHA-256 文件生成成功。

任何步骤失败，Action 以失败状态退出，只上传诊断，不上传可用运行包。成功结果按
Nightly commit 和适配器内容哈希缓存；新的官方 Nightly 会自动产生新缓存键。

## 8. 元数据与四图模型

### 8.1 静态类型关系图

来源是 il2cpp_dump.json。节点包括类型和成员；边包括继承、字段类型、属性类型、
返回类型、参数类型、泛型参数、RSZ 包含与反序列化关系。完整签名、重载索引、
静态性、可见性、参数和返回类型都保存在 SQLite。

### 8.2 MOD 用法图

来源是用户提供的真实 Lua MOD。索引器记录类型查询、字段读取、方法调用、Hook、
变量绑定、调用顺序、文件和行号。该图不是权威类型定义，但能证明某条链曾在实际
MOD 中使用，并为根选择和路径排序提供强证据。

### 8.3 运行时对象图

来源是 list_singletons 和 inspect_object。节点是绑定 runtime epoch 的
ObjectRef、Singleton 和受控值；边是字段、Getter、集合后备存储和对象关系。
原始地址不进入 MCP 响应或持久身份。

### 8.4 动态 Hook/Probe 图

来源是 Hook 参数、返回值和 Probe 事件。它补充静态 TDB 无法证明的实际流向，
例如“某方法参数在当前游戏状态中持有目标对象”。动态证据必须带 runtime epoch，
不能跨游戏会话复用。

~~~mermaid
flowchart LR
    S[静态类型关系图] --> P[AccessPlanner]
    M[MOD 用法图] --> P
    R[运行时对象图] --> P
    H[动态 Hook / Probe 图] --> P
    P --> A[带证据与成本的 AccessPlan DAG]
    A --> V[离线验证 + 当前运行时验证]
    V --> L[Lua Probe 或经审批的写操作]
~~~

## 9. search_members 与链式探索

search_members 是 MOD 探索的核心入口，不等同于简单字符串搜索。它执行：

1. 使用 FTS5 和结构化过滤寻找类型、成员名和完整签名；
2. 保留精确重载、参数、返回类型、静态性和声明类型；
3. 融合现有 MOD 使用次数和当前 runtime 观测次数；
4. 对候选目标执行有界可达性估计；
5. 返回排序证据、截断信息和继续构建 AccessPlan 所需的稳定标识。

search_members 不对数百万条边执行无界的“每候选从根向前 BFS”。1.0.0 使用从
目标向候选根的反向多根遍历，并设定：

- 路径缓存上限 256；
- 单次反向扩展上限 2,000 个类型；
- 单节点候选入边上限 1,024；
- 实际扩展入边上限 256。

索引为 type_edges(target_type) 提供反向查询。查询结果会报告遍历方向、扩展数、
预算和是否截断。缓存键包含快照、目标、根集合、深度和策略，不能跨快照误用。

search_members 回答“有哪些成员值得进一步研究”；find_access_paths 回答“从哪些
根、经哪些精确操作可以到达目标”。两者不能合并成一个不可控的大查询。

## 10. AccessPlan

AccessPlan 是强类型 DAG，不是 Lua 字符串。一个 Plan 包含：

- RootSpec：Managed Singleton、Native Singleton、Hook 参数、ObjectRef、
  静态类型或已有 MOD 根；
- AccessNode：字段读取、属性读取、方法调用、类型转换、集合迭代；
- 精确 MemberRef：声明类型、成员种类、完整签名和重载；
- 每条边的静态、MOD、运行时或动态证据；
- 成本、置信度、风险、前置条件和替代路径；
- snapshot_id 与可选 runtime_epoch。

validate_access_plan 先验证静态成员和类型，再按需验证当前 ObjectRef、Singleton、
字段或方法。invoke_method 和 set_field 只接受当前 runtime epoch 下通过实时
验证的 Plan。

## 11. run_generate_sdk 数据流

~~~mermaid
sequenceDiagram
    participant Agent
    participant Host as Python Host
    participant Bridge
    participant Export as Export Service
    participant OE as ObjectExplorer
    participant DB as SQLite
    Agent->>Host: run_generate_sdk(json_only 或 sdk_and_json)
    Host->>Bridge: Named Pipe 请求
    Bridge->>Export: Service ABI v1
    Export->>OE: 异步 generate_sdk
    Export-->>Host: job_ref、阶段、进度
    Host->>Host: 轮询并校验 manifest、大小、SHA-256
    Host->>DB: 流式导入 ijson、建 FTS 和关系边
    DB-->>Agent: 激活 snapshot 与 Resource URI
~~~

导出服务按游戏、TDB 指纹和模式复用有效结果。Python 导入器不把完整多 GB JSON
一次性载入内存；它使用流式解析和事务写入。快照只有在 manifest、计数、哈希和
数据库导入全部完成后才激活。

## 12. Lua Probe

draft_lua_probe 只根据已选择 AccessPlan 生成草稿。validate_lua_probe 检查：

- 禁用模块和文件/网络能力；
- 符号与精确成员；
- 生命周期与回调注册；
- 指令、时间、帧、事件和输出预算；
- 可选的当前 REF Lua 编译。

run_lua_probe 使用独立 Lua State。Probe Service 删除 io、package、require、
loadfile、dofile、debug、fs 和 imgui 等全局入口，限制指令数、超时、帧数、
事件数和输出字节，并对 userdata 与 lightuserdata 做脱敏。ObjectRef 解析仍由
Bridge 控制。

## 13. MCP 工具边界

| Tool | 主要执行层 | 风险等级 |
|---|---|---|
| runtime_status | Host + Bridge | 只读 |
| run_generate_sdk | Export Service + Host | 受控文件生成 |
| search_types | SQLite | 只读 |
| describe_type | SQLite | 只读 |
| search_members | SQLite + Planner | 只读 |
| find_type_dependencies | SQLite | 只读 |
| list_singletons | Bridge | 运行时只读 |
| inspect_object | Bridge | 有边界运行时只读 |
| search_usage_examples | SQLite | 只读 |
| find_access_paths | Planner | 只读 |
| validate_access_plan | Host + Bridge | 验证 |
| draft_lua_probe | Host | 生成草稿 |
| validate_lua_probe | Host + Probe Service | 验证 |
| invoke_method | Bridge | 经审批写操作 |
| set_field | Bridge | 经审批写操作 |
| run_lua_probe | Probe Service | 有边界执行 |
| install_hook | Bridge | 观察或经审批变换 |
| remove_hook | Bridge | 幂等清理 |

大型结果由 9 个 Resource 模板提供，避免把完整导出、图、Plan、Hook 或 Probe
事件全部塞入单次 Tool 响应。

## 14. 安全与审计

- HTTP 默认只监听 127.0.0.1；
- Bridge 只使用本机 Named Pipe；
- 写操作审批绑定工具名、完整参数哈希、runtime epoch 和短期有效期；
- invoke_method 与 set_field 额外绑定实时 Plan 验证引用；
- Getter 默认不执行，只允许显式 allowlist；
- Hook 和 Probe 有所有者、队列、数量、帧数和时间上限；
- remove_hook 幂等；
- ObjectRef 不等于地址，过期后必须重新探索；
- 每个有副作用请求写入审计日志；
- 直接 System.Array 分页在 1.0.0 中不猜测私有布局，必要时使用已验证 Probe。

## 15. 兼容性与维护成本

| 变化类型 | 预期影响 | 维护成本 |
|---|---|---|
| MCP SDK、Pydantic、SQLite 查询变化 | 仅 Host/EXE | 低 |
| 新 Tool、新评分或新图证据 | 主要是 Python | 低至中 |
| REF 公共插件 API 小改 | Bridge | 中 |
| ObjectExplorer 函数位置或周边文本变化 | 上下文或语义适配 | 中 |
| generate_sdk 签名、状态字段或 TDB API 改变 | Export Service | 中至高，Action 应失败 |
| Lua State 创建/销毁 ABI 消失 | Probe Service | 高，Action 应失败 |
| REF 构建系统或目标名重大变化 | Build Action | 中至高，Action 应失败 |

维护策略不是“尽量编过”，而是“可证明兼容才产包”。自动适配只覆盖等价结构变化；
一旦语义可能变化，失败比生成危险二进制更正确。

## 16. 已验证规模与性能

2026-08-28 的实机验证覆盖 Monster Hunter Stories 3 和 Monster Hunter
Wilds：

- MHST3：158,211 个类型、1,574,747 个成员、2,808,263 条类型边；
- MHWilds：322,054 个类型、3,428,218 个成员、5,915,472 条类型边；
- MHWilds SQLite 数据库约 8.36 GB；
- MHWilds 上连续三次 stdio search_members(app.GA) 为 1.54 至 2.44 秒。

验证覆盖 Bridge 协商、Singleton、对象检查、隔离 Lua 编译和执行、SDK
导出/复用、类型与成员搜索和依赖遍历。实机测试没有执行方法/字段写入和变换
Hook；这些副作用路径由自动化审批与验证门测试覆盖。

## 17. 版本与发布命名

MCP 产品版本与 REF Nightly 编号分别记录。例如：

reframework-mcp-1.0.0-ref-nightly-01397-windows-x64.zip

同一个包内的 compatibility-report.json 是二进制兼容来源。没有通过 Alignment
Action 的 REF Nightly 不列为支持目标。MCP Release 不维护独立的 REF stable
分支；它只发布与明确官方 REF Nightly 对齐的整合包。
