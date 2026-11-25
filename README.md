# Embedded Auto

当前版本：0.2.0（实机验收状态见下方记录）。

Windows/Python 本地嵌入式工具，按 **`mcu` 烧录与复位、`serial` 串口收发、`ble` 蓝牙操作**三个模块使用。人或 Agent 可以逐项调用，工具不会自行推进或重试。IAR/GCC 构建、BIN 导入与报告是辅助命令；旧测试套件和 `run` 保留兼容。

**当前 APM32E030 显示板已完成单次全擦、写入校验和启动日志实测。** 这不是三平台或完整真实构建/协议闭环验收；三个目标 YAML 仍为阻塞模板。当前支持矩阵和实机证据只在 [验收记录](docs/VALIDATION.md) 维护。

## 安装与安全预览

Python 3.11+。本机没有 `python3` 命令，使用已安装的 `python`：

```powershell
python -m pip install -e .
embedded-auto --help
embedded-auto mcu --help
embedded-auto serial --help
embedded-auto device check --target stm32f030 --json
```

BLE 示例配置与套件可直接离线运行，不需要 MCU target，也不要求安装 Bleak：

```powershell
embedded-auto ble scan --config configs/ble/demo.yaml --json
embedded-auto ble test --config configs/ble/demo.yaml --suite configs/ble/suites/demo.yaml --allow-write --json
```

这里的写入仅作用于本次新建的 Fake 外设状态；命令及套件使用见 [BLE 操作](docs/USER_GUIDE.md#ble-uuid-读写与通知)。真实后端需另装可选依赖 `embedded-auto[ble]`，锁定 `bleak==3.0.2`；不得由缺依赖自动回退 Fake。

`mcu preflight` 可离线核对具体 BIN 和擦除范围，不授予授权；区别于只检查计划的 `--dry-run`。真实操作前按[使用指南](docs/USER_GUIDE.md)核对布局、电气和授权。

**烧录需逐次提供绑定镜像和全片范围的授权；全片擦除会清除参数/KV/Bootloader，不自动备份恢复。** 外部 BIN 导入及完整门槛见 [烧录与启动](docs/USER_GUIDE.md#烧录与启动)。

## 开发与文档

| 入口 | 职责 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | Agent 开发约束、按需阅读和文档更新路由，不授予硬件/Git 操作权限 |
| [编码规范](docs/CODING_GUIDE.md) | 当前 Python 风格、分层、异常/资源、测试与证据要求 |
| [项目设计与模块索引](docs/design/PROJECT_DESIGN.md) | 当前模块、数据模型、流程和资源所有权；不另建重复索引/资源表 |
| [使用指南](docs/USER_GUIDE.md) | CLI、配置、硬件接入、协议、Agent API 和排障 |
| [验收记录](docs/VALIDATION.md) | 支持矩阵、软件/实机结果、原始证据入口和验收缺口 |
| [BLE 开发与测试方案](DEV_PLAN/BLE_TOOL_DEVELOPMENT_TEST_PLAN.md) | Fake MVP、适配契约、离线实现范围及后续实机验收计划；D6 待设备与 UUID 规格 |

Agent 先按设计索引定位本轮相关代码，再依编码规范实现/复验，按路由仅更新受影响文档；不必每轮阅读全部资料。源码在 `src/embedded_auto`，测试在 `tests`：

```powershell
python -m unittest discover -s tests -v
```

测试使用 Fake 硬件及可选真实 ARM GCC 合成编译，不做实机擦除。硬件操作须另行明确授权。运行证据在忽略提交的 `runs/`，精简实测 JSON 与本机工具输出保留于 `docs/tool-evidence/`；[原始开发计划](docs/archive/embedded_auto_development_plan.md) 仅作历史归档。
