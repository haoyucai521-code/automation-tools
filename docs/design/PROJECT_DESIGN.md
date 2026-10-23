# 当前项目设计

本文描述已实现结构与约束，不承担支持矩阵、历史记录或规划职责。使用与配置见 [使用指南](../USER_GUIDE.md)，实测结论见 [验收记录](../VALIDATION.md)，代码风格见 [编码规范](../CODING_GUIDE.md)。

## 用途与边界

Windows 本地 Python 工具，将 IAR/GCC 构建、BIN 身份校验、CH340 RTS 复位、CubeProgrammer 烧录、串口采集和确定性协议测试串成单目标工作流。CLI/Python 调用方与 Agent 供应商解耦；当前实现不含 GUI、MCP Server、动态插件或多板并发。

本仓库控制外部固件工程，不存放其全部源码。硬件参数来自目标配置；实机校准不等于正式 CLI、真实构建或协议闭环已经验收。

## 模块索引

路径以仓库根为基准；仅列自研模块。无需另建重复的 `SOURCEMAP.map`。

| 模块 | 路径/关键文件 | 入口 | 职责 |
| --- | --- | --- | --- |
| CLI | [cli.py](../../src/embedded_auto/cli.py)、[__main__.py](../../src/embedded_auto/__main__.py) | `main()` | 参数、配置、命令路由与结果输出 |
| 配置/路径 | [core/config.py](../../src/embedded_auto/core/config.py)、[paths.py](../../src/embedded_auto/core/paths.py) | `load_config()`、`validate_config()` | 合并配置、路径规则、按动作预检 |
| 模型/契约 | [core/models.py](../../src/embedded_auto/core/models.py)、[interfaces.py](../../src/embedded_auto/core/interfaces.py)、[errors.py](../../src/embedded_auto/core/errors.py) | `OperationResult`、各 `Protocol`、`ToolError` | 统一结构和错误接口 |
| 上下文/进程 | [core/run_context.py](../../src/embedded_auto/core/run_context.py)、[process.py](../../src/embedded_auto/core/process.py) | `RunContext`、`ProcessRunner.run()` | 运行目录、事件、超时/取消、进程执行与回收 |
| 构建 | [services/build_service.py](../../src/embedded_auto/services/build_service.py)、[adapters/build/](../../src/embedded_auto/adapters/build/) | `BuildService.build()`、`validate_artifact()` | IAR/GCC 调用、产物新鲜度、快照和身份/区域检查 |
| 烧录 | [services/flash_service.py](../../src/embedded_auto/services/flash_service.py)、[adapters/programmer/stm32cube_cli.py](../../src/embedded_auto/adapters/programmer/stm32cube_cli.py) | `FlashService.flash()`、`ResetAssistedConnectStrategy.run()` | 复位辅助连接、全擦/写入/校验及结果分类 |
| 串口 | [services/serial_session.py](../../src/embedded_auto/services/serial_session.py)、[adapters/serial/](../../src/embedded_auto/adapters/serial/) | `SerialSession` | 唯一端口所有者、单读线程、订阅、收发及控制线 |
| 复位 | [services/reset_controller.py](../../src/embedded_auto/services/reset_controller.py) | `ResetController` | 逻辑极性、保持/释放/脉冲及状态记录 |
| 日志 | [services/log_service.py](../../src/embedded_auto/services/log_service.py)、[log_analyzer.py](../../src/embedded_auto/services/log_analyzer.py) | `LogService`、`LogAnalyzer.analyze()` | 原始/解码归档及带证据路径的摘要 |
| 协议测试 | [services/test_runner.py](../../src/embedded_auto/services/test_runner.py) | `validate_suite()`、`TestRunner.run()` | 套件预检、发送、等待、全部断言与结果证据 |
| 工作流 | [services/workflow_engine.py](../../src/embedded_auto/services/workflow_engine.py) | `plan()`、`inspect()`、`execute()` | 注入各服务、状态/分支、清理与归档 |
| Agent 契约 | [agent/contract.py](../../src/embedded_auto/agent/contract.py)、[repair_policy.py](../../src/embedded_auto/agent/repair_policy.py) | `describe_contract()`、`RepairPolicy`、`RepairLoop.run()` | 工具映射、有限修复回调与保护检测 |

测试定位：[构建/烧录](../../tests/test_build_flash.py)、[串口/协议](../../tests/test_serial_protocol.py)、[工作流/CLI/修复](../../tests/test_workflow_cli.py)、[真实 ARM GCC 合成编译](../../tests/test_gcc_integration.py)。

## 数据模型与流转

- `OperationResult` 字段为 `success/operation/error_code/message/duration_ms/log_path/data/run_id`，`to_dict()` 将路径等转换为可 JSON 序列化值。
- `ToolError` 携带稳定 `code`、消息和补充数据。服务报告结果，编排层保留步骤与原始主错误，最终另附 `cleanup`。
- 配置为 `app/target/build/flash/reset/startup/serial` 分区字典；`sources` 记录来源文件。按命令验证，不要求只监听串口的动作填写构建命令。
- 构建清单记录成功状态、目标/料号、地址来源、SHA-256、大小、时间、命令及源/快照路径。它是本地完整性/身份约束，不是数字签名。
- 串口订阅事件含方向、字节和时间；内存缓冲使用绝对 RX 偏移。测试报告关联发送/接收 hex、时间、偏移和逐项断言。
- `RunContext` 为一次运行持有唯一目录、截止时间、取消事件、有效配置和追加事件，最终归档统一结果。

## 当前执行流程

```text
VALIDATE_CONFIG / 预检整个套件
  → 保存 Git 状态与 diff
  → BUILD
  → VALIDATE_ARTIFACT
  → 日志订阅就绪 / OPEN_SERIAL
  → ERASE_FLASH：复位保持 → 启动 CLI → 按配置延迟释放 → 等待擦除
  → 检查退出码和连接/擦除成功标志
  → WRITE_FLASH：再次复位辅助连接 → 固定平台起点写入与校验
  → PULSE_RESET
  → WAIT_BOOT
  → RUN_TESTS
  → CLEANUP
  → ARCHIVE
```

- 当前平台起点、CLI 参数和警告在 [烧录操作](../USER_GUIDE.md#烧录与启动) 统一说明；校验入口为 `validate_config()` 和 `CubeProgrammer.command()`。
- `build` 不打开串口；`connect` 不擦除/写入；`flash` 校验已有快照、全擦/写入/校验并脉冲复位，但不执行 `run` 的启动标志等待与协议断言。
- 擦除与写入使用独立进程、同一串口和日志会话；每段分别执行复位辅助连接。适配器只构造命令，连接策略控制 RTS；复位控制器不自行打开串口。
- 产物/套件/成功正则等先预检，才允许破坏性阶段。构建失败不烧录旧 BIN，擦除失败不写入，写入/校验失败不测试。
- `run` 从最后一次脉冲前的新字节偏移等待启动标志，避免旧固件日志误报；没有标志时明确记为弱延时判断。
- 每条路径都尝试清理。主失败保留，清理失败不覆盖它；操作成功但清理失败则整体不成功。

## 资源所有权与可靠性

此表是当前代码约束索引，不替代代码、电路核验或实测；不维护 MCU 的 DMA/中断/栈等外部固件资源表。

| 资源 | 唯一所有者 | 边界与检查入口 |
| --- | --- | --- |
| 物理 COM 与接收线程 | `SerialSession` | 进程内所有权表、OS 独占；单读循环分发，`open()/close()` 管生命周期 |
| RTS/DTR | 同一串口会话 | `ResetController` 映射复位逻辑；`set_rts()` 在历史接收错误后仍可尝试释放 |
| 接收内存窗口 | `SerialSession` | 默认有界缓冲，过旧偏移显式失败；归档原始字节不截断 |
| 串口证据文件 | `LogService` | 先订阅再读；RX 原始字节、收发事件和增量解码文本分别保存 |
| 外部工具进程 | `ProcessRunner` | 无 shell，输出直接落盘；超时/中断/回调失败后回收进程树并等待 |
| 构建源产物与运行快照 | 工程构建器 / `BuildService` | 不删用户产物；新鲜度/非空检查后复制快照并验证一致性 |
| 内部 Flash | `FlashService` 编排、烧录器实际操作 | 写入区域检查不限制全片擦除范围；无自动备份/恢复，禁止无限重试 |
| 运行目录/报告 | `RunContext` | 唯一目录、事件追加、总结原子替换；不复用历史运行目录 |

SHA-256 在产物复制与信任边界使用；不把哈希当来源认证。增量构建未真正更新 BIN 会保守返回 `ARTIFACT_STALE`。

## 协议与 Agent 边界

测试套件不执行任意 Python，判定由代码规则完成。全部用例预检、每例新偏移、明确超时、多规则全部满足才 PASS；病态协议正则在独立工作进程中执行并支持取消/超时。通用拆帧与副作用恢复不在当前测试引擎内。

`RepairLoop` 接收可信宿主提供的验证/修复回调，不启动模型、不自行改代码。不可变 `RepairPolicy` 限定目标及修复次数；只有指定的可修复错误才进入下一轮。工具源码、配置来源和调用方列入的断言文件被指纹保护，每轮记录 Git 状态/diff 和结果。

保护检测不是 OS 沙箱：有写权限的外部 Agent 仍可能主动绕过，宿主须限定修改范围与硬件权限。具体使用见 [Agent 调用](../USER_GUIDE.md#agent-调用)。

## 归档与不能保证的事项

报告目录结构与排障入口见 [报告与错误处理](../USER_GUIDE.md#报告与错误处理)。Git diff 只包含已跟踪文件变化；未跟踪文件仅列状态，需要宿主另外保存补丁，不自动采集任意敏感内容。

断电、强杀、驱动/内核故障不能保证执行 `finally`。关闭前设置释放不证明关闭后 NRST 电平；电压、瞬态和共线争用也不能靠软件检查代替。硬件成功结论仅覆盖 [验收记录](../VALIDATION.md) 中有证据的组合。
