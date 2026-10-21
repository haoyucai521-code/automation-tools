# 硬件接入与实测记录

## 当前状态（2026-10-21）

尚未提供实物接线、示波器数据、准确芯片型号或真实固件工程；本机串口枚举为 **no ports found**。因此没有执行打开串口、复位、connect-only、烧录或协议硬件测试。

- Python：3.12.2；PySerial：3.5；PyYAML：6.0.3。
- ARM GCC：xPack 15.2.1，版本输出见 `tool-evidence/arm_gcc_version.txt`。仅已验证合成 Cortex-M0 工程生成 BIN，不能代表真实固件或板卡支持。
- CubeProgrammer：在默认安装目录发现 **v2.16.0**，已执行 `--help`，退出码 0；原始输出见 `tool-evidence/cube_help.txt`。它不在当前 PATH，实际配置须填完整路径。
- IAR：当前 PATH 未发现 `IarBuild.exe`，不等于断言机器上没有安装；需要提供实际安装及工程路径。

以上信息来自本次环境检查，不会自动把模板的安全标记改为 true。

## 必须先完成的测量

| 项目 | 记录要求 | 对应配置/状态 |
| --- | --- | --- |
| 准确芯片与板卡编号 | 丝印、料号、硬件修订 | `target.exact_part_number`，待确认 |
| RTS 两状态对应 NRST | True/False 各自实测电压、波形 | `asserted_rts_value`、`polarity_verified` |
| 电压兼容性 | 模块供电、MCU NRST 容限、必要隔离/开漏 | `voltage_verified` |
| 复位网络接线 | ST-Link NRST 不与 CH340 推挽输出争用 | `wiring_verified` |
| 打开/关闭瞬态 | 多次开关串口的示波器波形 | `open_transient_verified` |
| BIN 地址与区域 | linker 文件版本、Bootloader 分区、区域容量 | `address/region_start/region_size/address_source` |
| 本机 CLI | 版本与实际帮助、连接和成功输出 | `cli_verified`，本次仅保存帮助，目标配置仍待确认 |
| 连接时序 | 延迟候选值、板卡、每次 run_id 与成功率 | `release_delay_ms/timing_verified` |
| 芯片兼容性 | 分型号连接、写入、校验结果 | `compatibility_verified`、`target.status` |

只有实际测量完成后才把相应字段改为布尔 `true`；不要用字符串 `"true"`，不要机械地全部置 true。

## Connect-only 实验

1. 确认电气与开关串口瞬态，填写端口、准确型号、RTS 极性和四个电气确认字段。
2. 按本机帮助配置 Cube 路径、`SWD/NORMAL/SWrst`、连接成功正则，核验后设置 `cli_verified: true`。
3. 选择一次候选 `release_delay_ms`。此时 `timing_verified` 与 `compatibility_verified` 可以仍为 false，目标仍为 template。
4. 执行 `embedded-auto connect --target stm32f030 --json`。该命令只连接，不带 `-w/-v`，不需要 BIN 地址或产物。
5. 保存该次 `run_config.yaml`、`events.jsonl`、`flash.log`、`flash_result.json` 与串口日志；记录真实 NRST 波形和释放时刻。根据测量选择下一候选值，**不要自动无限尝试**。
6. 找到稳定时序后，填好固件布局与写入/校验输出规则，再明确允许区域内的单次烧录。分别验证每个准确型号。

不以 GUI 卡顿作为同步信号；默认不使用依赖 ST-Link NRST 的 Under Reset/HWrst。

## 后续硬件验收（全部待执行）

连续复位启动、启动日志完整、单次连接/写入/校验、故障时释放 NRST、断连/串口占用/超时、连续 20 次无人工烧录、IAR 与真实 GCC 工程、三种目标各自兼容性、一个已知固件 Bug 的 Agent 闭环。记录工具版本、板卡、接线、工程版本、run_id 和失败记录，不能仅填写“通过”。
