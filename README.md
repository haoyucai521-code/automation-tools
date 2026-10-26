# Embedded Auto

Windows/Python 本地嵌入式自动化工具：IAR/GCC 构建、BIN 身份与区域校验、CH340 RTS 复位、CubeProgrammer 全片擦除/写入/校验、串口证据、确定性协议测试和有限 Agent 修复回调。

**当前 APM32E030 显示板已完成单次全擦、写入校验和启动日志实测。** 这不是三平台或完整真实构建/协议闭环验收；三个目标 YAML 仍为阻塞模板。当前支持矩阵和实机证据只在 [验收记录](docs/VALIDATION.md) 维护。

## 安装与安全预览

Python 3.11+。本机没有 `python3` 命令，使用已安装的 `python`：

```powershell
python -m pip install -e .
embedded-auto --help
embedded-auto run --target stm32f030 --suite example_protocol --dry-run --json
```

dry-run 不构建、不打开串口、不操作目标；返回成功仅指计划生成，继续检查 `data.ready_for_execution` 和 `blocking_issues`。真实操作前按 [使用指南](docs/USER_GUIDE.md) 核对工程、布局、电气与授权，先只连接再烧录。

**当前烧录会全片擦除，参数/KV/Bootloader 都可能丢失；正式流程不自动备份或恢复。** 固定写入起点与完整操作说明见 [烧录与启动](docs/USER_GUIDE.md#烧录与启动)。

## 开发与文档

| 入口 | 职责 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | Agent 开发约束、按需阅读和文档更新路由，不授予硬件/Git 操作权限 |
| [编码规范](docs/CODING_GUIDE.md) | 当前 Python 风格、分层、异常/资源、测试与证据要求 |
| [项目设计与模块索引](docs/design/PROJECT_DESIGN.md) | 当前模块、数据模型、流程和资源所有权；不另建重复索引/资源表 |
| [使用指南](docs/USER_GUIDE.md) | CLI、配置、硬件接入、协议、Agent API 和排障 |
| [验收记录](docs/VALIDATION.md) | 支持矩阵、软件/实机结果、原始证据入口和验收缺口 |
| [BLE 开发与测试方案](DEV_PLAN/BLE_TOOL_DEVELOPMENT_TEST_PLAN.md) | 基于新结构的 Fake MVP、适配契约及后续实机验收计划；尚未实现 |

Agent 先按设计索引定位本轮相关代码，再依编码规范实现/复验，按路由仅更新受影响文档；不必每轮阅读全部资料。源码在 `src/embedded_auto`，测试在 `tests`：

```powershell
python -m unittest discover -s tests -v
```

测试使用 Fake 硬件及可选真实 ARM GCC 合成编译，不做实机擦除。硬件操作须另行明确授权。运行证据在忽略提交的 `runs/`，精简实测 JSON 与本机工具输出保留于 `docs/tool-evidence/`；[原始开发计划](docs/archive/embedded_auto_development_plan.md) 仅作历史归档。
