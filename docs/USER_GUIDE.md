# 使用指南

本文统一维护 CLI、配置、硬件接入和排障。安装入口见 [README](../README.md)，当前可用范围与实测参数见 [验收记录](VALIDATION.md)，实现边界见 [项目设计](design/PROJECT_DESIGN.md)。

## 命令与返回

原有固件/串口命令支持 `--config`（默认 `configs/app.yaml`）、`--target`、`--json`、`--timeout`（整个操作秒数）、`--dry-run`、`--verbose`、`--port`、`--baudrate`；公共选项可放在子命令前后。`--config` 指全局 YAML，目标从同目录 `targets/<target>.yaml` 加载。`report` 不需要 target。BLE 使用独立子命令及公共选项，不接受也不构造 MCU target。

| 命令 | 行为 | 额外参数 |
| --- | --- | --- |
| `device check` | 检查配置、工具和串口枚举，不打开端口 | 无 |
| `build` | 构建，归档 BIN 快照和身份清单 | `--backend iar/gcc` 可选 |
| `connect` | 仅复位辅助连接，不擦除、不写入 | 无 |
| `flash` | 校验已有产物，全擦、写入、校验、脉冲复位 | `--artifact` 必需；`--manifest` 可选 |
| `reset` | 普通脉冲复位 | 无 |
| `monitor` | 定时采集串口证据，不主动发送数据 | `--duration` 默认 30 秒 |
| `test` | 对当前固件发送并断言协议响应 | `--suite` 必需 |
| `run` | 构建、全擦/写入/校验、复位、等待启动、协议测试 | `--suite` 必需；`--backend` 可选 |
| `report` | 读取历史结果，不操作硬件 | `--run-id` 必需 |

```powershell
embedded-auto device check --target stm32f030 --json
embedded-auto run --target stm32f030 --suite example_protocol --dry-run --json
```

`--dry-run` 保存计划，不编译、不打开串口、不连接、不擦除。退出成功只证明计划生成；必须查看 `data.ready_for_execution`、`blocking_issues`，也不能据此证明实物或产物可用。`device check` 是无侵入检查，不代替硬件验收。

退出码 0 为命令定义下成功，1 为操作失败，2 为参数语法错误。JSON 最终字段为 `success/operation/error_code/message/duration_ms/run_id/log_path/data`；服务详细结果位于 `data`，总目录为 `data.run_path`。`report` 返回原结果和原成功状态，不重新验证。模型摘要、终端 PASS 不能替代这些字段和原始证据。

## BLE UUID 读写与通知

当前 D0–D5 已实现 Fake 离线闭环、Bleak 薄适配、六命令及归档；D6 真实外设验收仍待设备与 UUID 规格。示例 [配置](../configs/ble/demo.yaml)、[外设 profile](../configs/ble/profiles/demo.yaml) 和 [套件](../configs/ble/suites/demo.yaml) 均可复制修改，UUID 必须写完整 128 位格式。

```powershell
embedded-auto ble scan --config configs/ble/demo.yaml --json
embedded-auto ble inspect --config configs/ble/demo.yaml --json
embedded-auto ble read --config configs/ble/demo.yaml --service 12345678-1234-5678-1234-56789abc0000 --characteristic 12345678-1234-5678-1234-56789abc0001 --expect-hex "01 00" --json
embedded-auto ble write --config configs/ble/demo.yaml --service 12345678-1234-5678-1234-56789abc0000 --characteristic 12345678-1234-5678-1234-56789abc0001 --hex "A5 5A" --write-mode with-response --allow-write --json
embedded-auto ble notify --config configs/ble/demo.yaml --service 12345678-1234-5678-1234-56789abc0000 --characteristic 12345678-1234-5678-1234-56789abc0001 --duration 2 --json
embedded-auto ble test --config configs/ble/demo.yaml --suite configs/ble/suites/demo.yaml --allow-write --json
```

- `app.workspace` 和通用 report 均相对配置文件目录，默认 `..`；`runs_dir` 相对 workspace，默认 `runs`。示例显式配置 `../..`。`fake_profile` 相对配置文件目录，`--suite` 相对调用目录，不支持 suite ID 搜索。
- `--backend fake|bleak` 和 `--device` 覆盖配置；连接命令必须有明确设备。真实设备仅接受本轮扫描返回的完整 MAC，配置和地址规范化为大写；Fake 为 `fake:demo`。同名候选不能作为连接标识。
- `--timeout` 仅覆盖总运行预算。`scan/connect/operation/cleanup` 各有独立局部配置，scan 的采样窗口不是 API 超时；通知窗口无匹配为断言失败，不是操作超时。
- read/notify 可用 `--expect-hex`；notify 还支持 suite 中的 `equals_hex / contains_hex / length` 多规则。无预期表示操作完成、`NOT_EVALUATED`，不能显示断言 PASS。空预期不会把“没有通知”判成空 payload。
- 所有写操作每次调用都必须给 `--allow-write`。真实写还要 `--confirm-device` 与配置设备完全一致。只提交一次，按显式 response 调用；不自动重发、拆包、恢复、配对或清缓存。写完成只表示提交/ATT 响应范围，不是业务确认。
- `test` 在首次扫描前完整预检，在发现后统一检查所有 UUID 和声明属性；首例 FAIL/ERROR 停止，后续 case 为 SKIP。suite 中 `subscribe → checkpoint → write → wait_notification` 明确事件窗口；普通 notify 的入口 checkpoint 在 `start_notify` 前。通知 PASS 只证明单条回调在窗口内匹配，不证明与写入有因果关系。
- `--dry-run` 只生成计划，不构造后端或执行 Fake，配置/授权问题列入 `blocking_issues`，verdict 仍为 NOT_EVALUATED。真实后端要求 Python 3.11+ 和 `bleak==3.0.2`（`python -m pip install -e ".[ble]"`）；缺依赖或版本不符报 `BLE_DEPENDENCY_MISSING`，不自动安装或回退。本机已安装版本不符时保持原版本，不擅自升级。
- 归档包括 `run_config.yaml / run_summary.json / agent_summary.md / events.jsonl / ble_events.jsonl / ble_assertions.json`；test 另有 profile/suite 快照。`success` 表示整体操作，data.verdict 表示断言；全部断言 PASS 后清理失败仍是整体失败。溢出、日志缺失和未归档数据不会声称证据完整。
- 订阅本身会改变远端通知状态，并非无作用观察。以上真实命令仅为语法示例，当前没有真实 BLE 外设验收；实际扫描、连接、订阅或写入仍需对应硬件与操作授权。

## 配置与工程

配置起点：[app.yaml](../configs/app.yaml)、[STM32F030](../configs/targets/stm32f030.yaml)、[STM32F070](../configs/targets/stm32f070.yaml)、[APM32E030](../configs/targets/apm32e030.yaml)。三者仍是安全阻塞模板；不要因某块板实测通过就批量将标记置 true。

- `app.workspace` 相对于全局 YAML 所在目录；`runs_dir`、`build.cwd` 相对于 workspace。
- `build.artifact/project_file` 相对于 `build.cwd`；带目录的 executable 相对于 workspace，纯名称通过 PATH 查找。
- CLI 指定的 suite 路径相对于调用目录；不带后缀的 ID 从全局配置同目录的 `test_suites/` 查找。
- 使用 PowerShell、UTF-8 和实际 Windows 路径；中文/空格路径传为单独参数。命令数组不是 shell 脚本。

GCC 构建配置示例（替换成真实工程）：

```yaml
build:
  backend: gcc
  cwd: D:/firmware/project
  command: [cmake, --build, build, --target, firmware_bin]
  artifact: build/firmware.bin
  timeout_s: 60
```

IAR 配置使用 `backend: iar`、实际 `executable`、`project_file` 和 `configuration`，生成 `[IarBuild.exe, 工程.ewp, '-build', 配置名]`。仅切换 `--backend` 不会生成另一套工程或命令。构建必须真正更新非空 BIN；无变化的增量构建会被保守判为旧产物，不要修改时间戳绕过。

即使仅 `build`，也需准确料号、地址来源与允许区域用于生成身份清单，但不要求已验证电气状态。仅 `monitor/reset/test` 不要求烧录地址，但仍要求串口与复位电气确认，因为打开串口就可能影响 RTS。

## 硬件接入

1. 核对目标板/完整芯片料号、供电及共地。ST-Link 按实物接 SWD；CH340 RX 接日志输出，TX 仅在确有接收端且协议需要时连接。
2. CH340 RTS 接 NRST，但不要让两个输出源未经确认就同时驱动复位网络；当前策略不依赖克隆 ST-Link 的 NRST。
3. 核实模块输出电压/MCU 容限、True/False 与复位状态的关系，以及串口开关瞬态；软件布尔值不是物理电平。
4. 若日志脚复用 SWCLK，烧录期间串口可能捕获非 UART 字节；保留原始数据，不把它们直接判为固件日志故障。能共线使用只证明已测试的硬件组合，需检查输出争用，不能推广至任意调试器。
5. 串口独占：关闭占用 COM 的其他串口助手；不能一边让工具占有 COM，一边用另一应用控制 RTS。

| 核验项 | 填写位置/证据 |
| --- | --- |
| 板卡、完整料号及修订 | `target.id/exact_part_number`，丝印/人工确认 |
| COM、波特率与帧格式 | `serial`；`python -m serial.tools.list_ports -v` 只枚举 |
| RTS 逻辑有效状态 | `reset.asserted_rts_value`、`polarity_verified` |
| 电压、接线与开关瞬态 | `voltage_verified/wiring_verified/open_transient_verified`，按真实核验记录 |
| CLI 版本/参数/成功输出 | `flash.executable/cli_verified` 和成功正则 |
| 连接释放延迟 | `reset.release_delay_ms/timing_verified`，板卡和运行证据 |
| 写入布局与容量 | `flash.address/region_start/region_size/address_source`，核对 linker |
| 目标兼容性 | `compatibility_verified/target.status`，明确实测范围 |

只在有对应证据时写布尔 `true`，不要用字符串 `"true"` 或机械解锁所有标记。当前显示板的功能性极性测试不代替电压/瞬态测量；正式 CLI 的检查仍保留。

### 先验证连接和日志

先完成相关安全配置，再按需运行；这些是实机操作示例，不应自动执行：

```powershell
embedded-auto monitor --target apm32e030 --port COM7 --baudrate 9600 --duration 15 --json
embedded-auto connect --target apm32e030 --json
embedded-auto reset --target apm32e030 --json
```

`connect` 要求明确料号、CLI 核验和连接成功正则；此时 `timing_verified/compatibility_verified` 可仍为 false，目标可仍为 template。选择一次候选释放延迟，记录每次结果，再决定下一次操作；不用 GUI 卡顿作同步信号，不自动无限尝试，不自动改成 Under Reset/HWrst。

`pulse_ms` 是普通复位宽度；`release_delay_ms` 仅用于启动 CLI 后释放复位；可选 `post_pulse_delay_ms` 默认为 0，是普通复位后的独立等待。不要混为一个时序。

**清理成功不证明关闭后板卡仍运行。** 驱动可能在关闭串口后改变 RTS，当前显示板有关闭后被保持复位的用户观察。需继续运行时，可由串口助手重新打开并保持已确认的释放状态，或在确认接线后人工断开 RTS；工具不会悄悄留下后台占用。

## 烧录与启动

当前 STM32/APM32 策略固定：**全片擦除 → 从 `0x08000000` 写入 → 校验 → 脉冲复位**。配置和适配器拒绝其他固件起点；linker 起点仍须匹配，`region_size` 为按板卡确认的字节数。

**全片擦除会清除整个内部 Flash，包括 Bootloader、KV、校准、配对参数，即使不在 BIN 覆盖区域内。正式流程不自动备份/恢复。** 执行前确认目标、授权范围和需保留内容；不要把实机校准脚本的额外备份/读回当成正式 CLI 的内建功能。

正式 `flash` 仅接受带 `artifact_manifest.json` 的构建快照，不支持任意裸 BIN 直接导入，也没有忽略身份/哈希/校验的开关。清单不是数字签名，必须来自受信本地构建目录。显示板桌面 BIN 的试验是单独校准记录，不是可复用导入接口。

```powershell
embedded-auto build --target apm32e030 --backend gcc --json
embedded-auto flash --target apm32e030 --artifact "runs/<build_run_id>/firmware.bin" --json
embedded-auto run --target apm32e030 --suite example_protocol --timeout 120 --json
```

默认读取 BIN 同目录清单；真实写入还要求 `target.status: verified`、CLI/兼容性/时序核验及真实成功正则。进入擦除前先完成全部产物和配置预检。

```text
CLI -c port=SWD mode=NORMAL reset=SWrst -e all
  ↓ 退出码 0 且连接、擦除成功标志匹配
CLI -c port=SWD mode=NORMAL reset=SWrst -w firmware.bin 0x08000000 -v
```

两段各自按复位保持、启动 CLI、延迟后释放执行，同一串口持续归档。擦除失败/取消不启动写入；写入或校验失败不进入协议测试。`connect` 不执行擦除。默认没有自动重试。

`flash.erase_success_pattern` 必须来自本机实际输出，不用总能匹配的宽泛正则；当前已实测文本与工具版本在 [验收记录](VALIDATION.md)。擦除证据见 `erase.log` 和 `flash_result.json` 的 `data.erase`；dry-run 显示顺序、命令及破坏性警告。

`flash` 在校验后脉冲复位，不等待启动标志或执行协议测试；`run` 还根据 `startup.ready_pattern` 等待新固件启动，超时为 `startup.timeout_s`，使用 `serial.encoding` 解码；病态启动正则在独立工作进程中执行，受启动超时与总截止时间约束。标志为空时只等 `settle_delay_s` 并明确标记弱判断，不声称检测到了启动。

## 协议套件

格式版本为 1。每例必须含名称、恰好一个 UTF-8 `send_text` 或十六进制 `send_hex`、正数 `timeout_s` 和非空 `expect`。套件整体在构建/硬件前预检，同一例所有断言必须同时满足。

```yaml
version: 1
tests:
  - name: binary_response
    send_hex: "01 02"
    timeout_s: 2
    expect:
      header_hex: "AA 55"
      length: 6
      fields:
        - {offset: 2, size: 2, byteorder: little, value: 1}
      checksum: {algorithm: sum8, start: 0, end: 5, index: 5}
```

规则：`contains_text/contains_hex/equals_hex/header_hex/length/regex/ordered_text/fields/checksum`。`fields` 为无符号字段，指定大小和字节序；校验和支持 sum8/xor8，`end` 不含末端。偏移从本例 RX 起点算，报告同时保存全局字节偏移。

UTF-8 文本正则匹配字节流，跨行需显式 `(?s)`；协议正则工作进程支持超时/取消。空响应不会因零长度或空匹配通过。引擎不自动拆分复杂协议帧，也不执行未知前置/恢复脚本；副作用由用例作者明确，异步日志可能影响 `equals/length`。

[example_protocol.yaml](../configs/test_suites/example_protocol.yaml) 的 PING/PONG 只是样例。只输出日志的口不代表能接收命令；当前显示板没有完成双向协议验收，不把日志启动成功当作协议通过。

## Agent 调用

可执行命令的 Agent 直接用 CLI，无专有 SDK。`describe_contract()` 在 [agent/contract.py](../src/embedded_auto/agent/contract.py) 提供结果 schema、错误码及逻辑工具映射：`inspect_target/build_firmware/flash_firmware/reset_target/capture_serial/run_protocol_tests/get_run_report/run_workflow`。

1. 明确目标板、工程、允许动作和最大修复次数；源码修改与硬件动作分别授权。
2. 保存 Git 状态和本轮补丁。工具不创建分支、不 commit/push、不调用外部模型。
3. 运行获准命令并读取 JSON 和原始证据；工具/硬件错误先停止，不能当作固件 Bug 盲目改代码。
4. 成功停止；默认最多修复 3 轮，即初次验证加最多 3 次修复后的验证，不删除失败记录或静默修改断言。

Python 接口为 `RepairLoop(policy, protected_paths=[...]).run(target, execute, repair, ctx)`：

- `RepairPolicy(allowed_targets=('stm32f030',), max_repairs=3)` 为不可变策略，修复次数仅接受有限 0..100。
- `execute()` 返回 `OperationResult`；`repair(round_number, previous_result)` 由可信宿主在授权范围修改源码；`ctx` 为运行上下文。
- 宿主必须将全部测试预期、额外策略/布局文件加入 `protected_paths`；源码和配置来源自动保护。变化被检测时保留现场并停，不擅自回滚。
- 每轮 Git 快照位于 `repair_XX_before/after`，结果为 `repair_report.json`；未跟踪新文件须宿主另存。涉及布局、时钟、低功耗、DMA、中断或启动代码的改动应突出风险。

这是流程和篡改检测，不是 OS 沙箱。当前无 MCP Server，有限回调也不等于真实已知 Bug 修复闭环已验收。

## 报告与错误处理

每次操作使用独立 `runs/<run_id>/`，按实际执行步骤生成文件：

| 文件 | 内容 |
| --- | --- |
| `run_config.yaml`、`events.jsonl`、`run_summary.json` | 生效配置、时序事件、最终步骤和清理结果 |
| `git_state.json`、`git_diff.patch`、`agent_summary.md` | 源码状态及证据入口，不是完整源码备份 |
| `build.log/build_result.json`、`firmware.bin/artifact_manifest.json` | 构建证据和快照；独立 flash 另存输入清单 |
| `erase.log`、`flash.log/flash_result.json` | 擦除与烧录退出码、参数和匹配结果 |
| `serial.bin`、`serial.jsonl`、`serial.log` | 原始 RX、RX/TX 时间/hex/偏移、解码文本 |
| `test_suite.json`、`test_report.json`、`log_analysis.json` | 实际套件、逐项证据和日志摘要 |

先读 `run_summary.json` 的主错误及 `data.cleanup`，再沿事件和原始文件定位；不要用再次烧录覆盖失败证据。运行目录默认不进入 Git。

| 错误 | 排查/停止要求 |
| --- | --- |
| `CONFIG_INVALID` | 核对 TODO、布尔类型、芯片/区域、电气确认、超时和套件；模板不是可执行配置 |
| `BUILD_TOOL_NOT_FOUND/BUILD_CONFIG_INVALID` | 真实工具/工程路径与 argv；Cube/IAR 不在 PATH 不等于未安装 |
| `BUILD_FAILED/BUILD_TIMEOUT/ARTIFACT_STALE` | 查 `build.log`，确认本次生成 BIN，不改时间戳、不烧录旧文件 |
| `ARTIFACT_*` | 查目标、清单、快照、哈希、大小、地址与区域；重新正确构建，不伪造 manifest |
| `FLASH_TOOL_NOT_FOUND/CONNECT_FAILED/CONNECT_TIMEOUT` | 检查完整工具路径、CLI 输出、电源、接线/复位；先只连接，不自动改模式 |
| `FLASH_ERASE_FAILED` | 查 `erase.log/data.erase`；不启动写入。若已经擦除而后续失败，板卡可能无法启动，保留现场，不静默恢复/重试 |
| `FLASH_FAILED/FLASH_VERIFY_FAILED` | 停止协议测试，检查输出、布局与工具兼容，不将校验失败标成功 |
| `RESET_CONTROL_FAILED` | 硬件状态未知，检查 NRST；清理仍尝试释放，失败需人工处理 |
| `SERIAL_PORT_BUSY/SERIAL_OPEN_FAILED` | 释放串口助手占用、检查实际 COM；不强制关闭外部应用 |
| `SERIAL_READ_FAILED/SERIAL_WRITE_FAILED/SERIAL_CONTROL_FAILED/SERIAL_CLOSE_FAILED/SERIAL_SUBSCRIBER_FAILED` | 检查驱动、断线、回调和清理结果；接收失败不应阻止释放复位 |
| `SERIAL_BUFFER_RANGE` | 窗口已过期；原始文件仍保留，合理调整缓冲或判定窗口 |
| `BOOT_TIMEOUT/PROTOCOL_TEST_FAILED` | 查最后复位后的 RX、编码、标志或逐条断言；不放宽预期制造 PASS |
| `BOOT_MATCHER_FAILED` | 启动匹配器自身启动/通信故障，属环境问题而非固件协议失败；查本机进程/资源后重试，不进入修复循环 |
| `WORKFLOW_TIMEOUT/CANCELLED` | 查进程、NRST、COM 清理记录，再决定下一步 |
| `CLEANUP_FAILED/PROCESS_CLEANUP_FAILED/LOG_WRITE_FAILED` | 保留主故障和 PID/路径，检查磁盘、端口及硬件，不把报告失败当成功 |
| `REPAIR_LIMIT_REACHED/REPAIR_POLICY_VIOLATION` | 停止，交付证据，不自动提高上限或修改保护规则 |

断电、强杀、系统/驱动异常不能保证 finally 执行。恢复前人工确认电源、NRST 和后台进程；不能仅凭没有 Python 进程就宣布硬件恢复。
