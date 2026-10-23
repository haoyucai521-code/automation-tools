# Embedded Auto

以 Python 实现的 Windows 本地嵌入式自动化工具：IAR/GCC 构建、BIN 身份与区域校验、CH340 RTS 复位、STM32CubeProgrammer、原始串口归档、确定性协议测试及有限 Agent 修复策略。

**软件实现与无硬件验证已提供；真实板卡验收尚未完成。** 三个目标文件均为不可直接执行的模板。APM32E030 兼容性未作假定，不会自动填写烧录地址、极性或成功标志。

## 安装与快速检查

要求 Python 3.11+。通常使用 `python3`；本次 Windows 环境使用已安装的 `python`：

```powershell
python -m pip install -e .
embedded-auto --help
embedded-auto run --target stm32f030 --suite example_protocol --dry-run --json
python -m unittest discover -s tests -v
```

`--dry-run` 只保存执行计划和命令参数，不编译、不打开串口、不连接或写入目标。返回成功表示计划生成成功；配置/套件的已知阻塞项请读取 `data.ready_for_execution` 和 `blocking_issues`，不代表工具、板卡或产物已经可用。真实操作遇到未确认字段返回 `CONFIG_INVALID`。

## 首次接入

1. 阅读 `docs/HARDWARE_SETUP.md`，先确认电气安全，再填写 `configs/app.yaml` 的串口及目标 YAML。
2. 从真实工程的 linker 配置填写准确芯片、BIN 地址、允许区域、地址来源和构建命令。
3. 核对本机 CubeProgrammer `--help`，配置实际工具路径与成功输出正则。
4. 先 `connect` 做只连接时序实验，每次保存报告；确认后才设置目标 `status: verified`、`compatibility_verified` 和 `timing_verified`。
5. 调整示例协议断言，再运行完整工作流。

```powershell
embedded-auto device check --target stm32f030 --json
embedded-auto build --target stm32f030 --backend gcc --json
embedded-auto connect --target stm32f030 --json
embedded-auto monitor --target stm32f030 --duration 30 --json
embedded-auto run --target stm32f030 --suite example_protocol --timeout 120 --json
embedded-auto report --run-id <run_id> --json
```

每次操作使用独立 `runs/<run_id>/`；保留配置、状态事件、Git 状态/diff、BIN 快照/清单、构建与烧录结果、串口原始字节、测试报告和 Agent 摘要。不自动重试烧录，不删除历史失败记录。

**当前 STM32/APM32 烧录策略：先全片擦除，再从 `0x08000000` 写入并校验。** 擦除失败不继续写入；`connect` 不执行擦除。全片擦除会清除内部 Flash 中的 Bootloader、KV 和参数，执行前自行备份需要保留的内容。目标配置需填写真实的 `erase_success_pattern`；dry-run 会显示擦除命令和警告。

## 文档

- `docs/CLI_REFERENCE.md`：命令、配置与协议断言格式。
- `docs/ARCHITECTURE.md`：模块边界和安全机制。
- `docs/AGENT_GUIDE.md`：稳定 JSON 契约、修复回调及停止策略。
- `docs/HARDWARE_SETUP.md`：接线、实测步骤与连接实验。
- `docs/TROUBLESHOOTING.md`：错误分类与恢复。
- `docs/DEVELOPMENT_STATUS.md`：计划逐阶段交付情况、支持矩阵与验收缺口。

MCP、GUI、多板并发及动态插件不属于本版；CLI 与 Python 接口不依赖任何 Agent 供应商。
