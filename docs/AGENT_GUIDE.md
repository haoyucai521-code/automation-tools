# Agent 集成契约

先用 CLI，不依赖特定 Agent SDK。命令返回 JSON，最终判断必须读取 `success/error_code` 和归档原始证据，不能只看终端中的 PASS、自然语言摘要或模型解释。

`embedded_auto.agent.contract.describe_contract()` 提供 v1 结果 schema、固定错误码和逻辑工具到 CLI 的映射：inspect_target/build_firmware/flash_firmware/reset_target/capture_serial/run_protocol_tests/get_run_report/run_workflow。

## 建议调用顺序

1. 明确当前任务唯一目标板、固件工程、允许动作和最大修复次数；不把外部 Agent 的修改权限当作硬件操作授权。
2. 保存 Git 状态和本轮补丁。工具本身不创建分支/worktree、不 commit/push、不调用外部 Agent。
3. 执行 `embedded-auto run --target <id> --suite <suite> --json`，读取对应运行目录下的结构化报告及 build/flash/serial 原始日志。
4. 只有构建/启动/协议失败才考虑代码修复；工具缺失、接线、复位、烧录、串口异常应停止修复并报告环境问题。
5. 成功立即停止；失败最多修复 3 轮（初次验证 + 最多 3 次修复后的验证）。保留每轮 run_id、diff 和停止原因。

## Python 修复策略

`RepairPolicy(allowed_targets=('stm32f030',), max_repairs=3)` 为不可变策略；只接受有限 0..100 上限。`RepairLoop(policy, protected_paths=[suite_path, ...]).run(target, execute, repair, ctx)` 接受：

- `execute()`：调用工作流，返回 OperationResult，不将失败误报成功。
- `repair(round_number, previous_result)`：外部宿主在已授权源码范围内进行一轮修改。工具不实现模型调用或自行改代码。
- `ctx`：RunContext，用于总超时、事件、每轮前后 Git 状态/diff 和 repair_report.json。

调用方必须把**全部测试预期文件**和额外策略/布局配置加入 `protected_paths`；工具源码及 ctx 配置来源会自动纳入保护。回调前后发现保护文件变化，返回 `REPAIR_POLICY_VIOLATION` 并保留现场，不擅自回滚修改。环境/硬件错误不重试；达到上限返回 `REPAIR_LIMIT_REACHED`。

这是可信客户端的流程约束与篡改检测，**不是 OS 沙箱**，不能阻止任意有文件写权限的外部 Agent 修改文件或主动绕过该 API。宿主仍须约束写入范围，防止修改断言、工具安全限制和目标白名单。涉及 Bootloader、布局、时钟、低功耗、DMA、中断/启动代码的修改需要在任务报告突出说明；本工具不对源码语义自动分类。

每次修复的 Git 快照保存到 repair_XX_before/after，验证结果记录其 run_id；未跟踪新文件需由宿主另存补丁。MCP 是计划中的可选薄适配，本版未实现，不影响 CLI 集成。真实已知固件 Bug 的自动修复验收仍需板卡与工程。
