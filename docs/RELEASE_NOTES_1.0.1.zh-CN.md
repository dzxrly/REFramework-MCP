# REFramework-MCP 1.0.1

1.0.1 是针对《怪物猎人：荒野》全工具实机测试结果的修复版本，保持
Bridge Protocol、Export Service ABI、Probe Service ABI 和 AccessPlan schema
均为 1.0。

## 修复内容

- 修复 SDK 导出或主机索引期间 `runtime_status` / `doctor` 因恢复查询缺少
  `state` 列而崩溃。
- 变更类工具现在优先使用 MCP Elicitation 请求用户确认；不支持 Elicitation
  的客户端会收到符合工具 schema 的 `approval_required` 数据和短期
  `approval_ref`，不再得到通用执行错误。
- 方法型 AccessPlan 支持无副作用实时干运行验证：检查接收者、精确重载和参数
  编码；只有显式允许的中间零参数 getter 才会执行。
- `plan_validation_ref` 严格绑定目标操作、对象、成员和方法参数，不能跨成员
  或跨参数复用。
- ObjectRef 响应包含租约，主机运行时图会清理过期节点与关联边；单例计划按
  类型重新解析，不再固化临时 ObjectRef。
- SDK 总进度改为单调的分阶段加权值，并报告 TDB 实体总数、已处理实体估计值、
  主机索引实际计数及独立的 `bridge_state` / `host_state`。
- Bridge 与快照共用同一 TDB fingerprint；新 manifest 写入
  `runtime_epoch`、`reframework_version` 和实体计数。
- CLI 的 `--transport`、`--host` 和 `--port` 在服务初始化前生效，
  `runtime_status` 会报告真实传输方式。
- 成功执行的短 Lua Probe 至少报告一个执行帧和一个已执行指令下界，并公开
  指令采样粒度。

## 升级说明

请从同一个 1.0.1 发布包同时更新主机程序、`dinput8.dll` 和
`reframework/plugins/reframework_mcp.dll`，不要混用 1.0.0 组件。

1.0.1 不复用 1.0.0 Export Service 生成的缓存。升级后第一次
`run_generate_sdk` 会重新导出，以补齐统一 fingerprint 和运行时身份字段；
后续相同版本仍可正常复用。
