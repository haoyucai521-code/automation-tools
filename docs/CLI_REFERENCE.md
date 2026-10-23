# CLI 与配置参考

所有命令支持 `--config`（默认 `configs/app.yaml`）、`--target`、`--json`、`--timeout`（全操作秒数）、`--dry-run`、`--verbose`、`--port`、`--baudrate`。公共选项可放在子命令前后。`--config` 指全局文件，目标配置从其同目录 `targets/<target>.yaml` 加载。

| 命令 | 用途 | 额外参数 |
| --- | --- | --- |
| `device check` | 枚举工具/串口，报告配置问题；不打开端口 | 无 |
| `build` | 构建并保存可审计 BIN 快照和 manifest | `--backend iar/gcc`（可选） |
| `connect` | 仅做复位辅助连接实验，不擦除、不写入 | 无 |
| `flash` | 校验清单后，全片擦除、写入已构建快照并校验 | `--artifact` 必须；`--manifest` 可选 |
| `reset` | 普通脉冲复位 | 无 |
| `monitor` | 定时采集 RX/TX 证据 | `--duration` 默认 30 秒 |
| `test` | 对当前固件运行确定性协议套件 | `--suite` 必须 |
| `run` | 构建、烧录、复位启动、测试、清理和归档 | `--suite` 必须；`--backend` 可选 |
| `report` | 读取既有最终报告，不访问目标 | `--run-id` 必须；不需要 target |

退出码 0 表示命令定义下的成功，1 表示操作失败，2 为 argparse 参数错误。`--json` 的操作结果为单个 JSON 对象；`--help` 和参数语法错误沿用 argparse 格式。对象字段：`success/operation/error_code/message/duration_ms/run_id/log_path/data`。具体服务路径位于 `data` 的步骤结果中；总证据目录为 `data.run_path`。`report` 返回原运行结果和原成功状态，不重新执行。

## 路径和构建

`app.workspace` 相对于全局 YAML 目录；`runs_dir` 和 `build.cwd` 相对于 workspace；`artifact/project_file` 相对于 build.cwd。带目录的 executable 相对于 workspace，纯名称通过 PATH 查找。CLI 指定的 suite 路径相对于调用目录；不带后缀的 suite ID 从 `configs/test_suites` 查找。

GCC 示例（替换为真实工程命令）：

```yaml
build:
  backend: gcc
  cwd: D:/firmware/project
  command: [cmake, --build, build, --target, firmware_bin]
  artifact: build/firmware.bin
  timeout_s: 60
```

IAR 示例：`backend: iar`、`executable: <IarBuild.exe完整路径>`、`project_file: <工程.ewp>`、`configuration: Release`，生成命令为 `[executable, project_file, '-build', configuration]`。单独切换 `--backend` 不会替你生成另一后端的工程或命令。

即使仅 build，也需准确芯片、地址来源和允许区域，以生成身份清单；**不要求**已验证的硬件接线或芯片兼容状态。仅 monitor/reset/test 不要求烧录地址，但必须已完成串口打开和电气确认。

独立 flash 只能使用带清单的快照：

```powershell
embedded-auto flash --target stm32f030 --artifact "runs/<build_run_id>/firmware.bin" --json
```

默认同目录读取 `artifact_manifest.json`。不提供忽略哈希、忽略校验或直接烧录任意裸 BIN 的绕过选项；清单不构成来源签名。真实烧录要求 `status: verified`、`cli_verified/compatibility_verified/timing_verified: true`。按用户确认，当前 STM32/APM32 平台统一使用 `address: "0x08000000"`；配置和烧录适配器拒绝其他起点。固件 linker 起点仍必须匹配，`region_size` 单位为字节，容量须按具体板卡填写。

### 全片擦除流程（2026-10-23 新增）

`flash` 和 `run` 在写入前必做一次全片擦除，无跳过选项：

```text
CLI -c port=SWD mode=NORMAL reset=SWrst -e all
  ↓ 退出码 0，且匹配连接和擦除成功标志
CLI -c port=SWD mode=NORMAL reset=SWrst -w firmware.bin 0x08000000 -v
```

两个阶段各自执行外部 RTS 辅助连接，沿用实测释放时序；串口会话和日志采集持续保持。先校验全部产物身份、区域和成功正则，再允许擦除。擦除失败、不匹配成功标志或被取消时不启动写入；写入/校验失败时不进入协议测试。

目标配置新增必需的 `flash.erase_success_pattern`，应取自本机真实 `-e all` 输出，而不是使用总能匹配的宽泛正则。擦除证据保留在 `erase.log` 和 `flash_result.json` 的 `data.erase`；`--dry-run` 显示擦除命令、执行顺序和破坏性警告，不执行任何硬件动作。

**全片擦除会清除整个内部 Flash，包括 Bootloader、KV、校准/配对等参数，即使它们不在 BIN 覆盖范围内。** 正式流程不自动备份/恢复；执行前按需保存。本次增加的是软件流程，先前单次实机报告没有执行过全片擦除，不能作为新擦除步骤的硬件验收。

## 复位与启动

`asserted_rts_value` 是逻辑有效时的软件布尔值，不是引脚电平。`pulse_ms` 控制普通脉冲宽度；`release_delay_ms` 只用于 CLI 启动后的辅助连接释放；可选 `post_pulse_delay_ms`（默认 0）为普通复位后的独立延时。

`startup.ready_pattern` 是新固件启动文本正则，解码使用 `serial.encoding`，等待 `startup.timeout_s`。若为空，使用 `settle_delay_s`，结果明确标记为弱启动判断，不声称检测到启动完成。

## 协议套件 v1

每项必须包含名称、恰好一个发送字段（UTF-8 `send_text` 或 `send_hex`）、正数 `timeout_s` 和非空 `expect`。全部用例在构建/硬件操作前校验；同一测试的所有断言必须同时满足。二进制偏移均从本测试收到的第一个字节起，报告同时保存全局串口绝对偏移。

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

可用规则：`contains_text`、`contains_hex`、`equals_hex`、`header_hex`、`length`、`regex`、`ordered_text`、`fields`、`checksum`。`fields` 使用无符号整数字节序；`checksum` 支持 sum8/xor8，end 为不含末端的范围。UTF-8 文本正则在字节流上匹配（如需换行跨越，用显式 `(?s)`）；正则工作进程支持超时/取消，不阻塞串口读线程。空响应不会通过零长度或空匹配断言。

前置状态与恢复由用例作者在说明和固件设计中定义；本版不自动执行未知的前置/恢复脚本。测试使用当前完整接收窗口，不实现通用复杂协议的自动拆帧；持续异步日志可能影响 equals/length，需要结合真实协议选择规则或扩展专用解析器。
