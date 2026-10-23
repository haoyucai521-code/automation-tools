# 2026-10-23 实机流程与验证记录

## 用户确认的通用接线和流程

用户确认其 STM32/APM32 板卡沿用以下流程，烧录方式可复用：

1. ST-Link 正常接 SWD；CH340 的 RX 接板卡日志输出脚，共地。
2. 日志脚可在固件启动后由 SWD 引脚复用；当前接线为 ST-Link CLK 和 CH340 RX 共接该信号，CH340 TX 不用于此日志采集。
3. CH340 RTS 接 NRST。逻辑 `RTS=True` 保持复位，`RTS=False` 释放复位。
4. 串口保持打开、接收和归档就绪后，设置 True，启动 CubeProgrammer 连接，按实验确认的时机设置 False，再等待写入和校验。
5. 写入校验通过后，再做 True→False 脉冲复位，接收新固件启动日志。
6. 关闭串口后的实际 RTS 电平不保证保持，不能把“关闭前设过 False”等同于“关闭后板卡仍运行”。必要时在完成后保持串口打开，或由用户断开 RTS。

通用流程来自用户确认；本工具目前直接实测仅覆盖下面这块显示板。其他板卡不因此自动获得“20 次烧录稳定性已通过”或“电压/瞬态已测量”的结论。原通用目标模板的安全标记不自动全部置 true。

## 当前显示板的极性和串口实测

- 日期：2026-10-23。
- 目标：用户确认的 APM32E030 显示板；项目 IAR linker 文件注释为 APM32E030F8P6，但尚未独立核对芯片丝印。
- 串口：USB-SERIAL CH340，COM7，9600，8N1，无 RTS/CTS 流控。
- 接线保持 ST-Link 与 CH340 同时连接。
- 测试证据：`runs/rts_rx_check_20261023T064852307876Z/`。
- True 第 1/2 轮：各接收 6 秒，均 0 字节。
- False 第 1/2 轮：各接收 6 秒，均收到 281 字节，包含 ZOS 启动日志。
- 已观察日志：`Reset: Unknown/Normal`、`WELCOME TO ZOS`、版本 `1.0.0.0`、构建时间 `Oct 23 2026 14:31:01`、`open uart[0]`。
- 结论：当前板卡实测配置 `asserted_rts_value: true`；False 释放后 MCU 启动并输出日志。未测量实际电压/波形，不填造示波器结论。
- 早期两个状态均 0 字节的实验保留原记录，不采用该次无数据结果推断极性。

## 首次烧录试验的固件范围（未全片擦除）

- 用户提供：`C:/Users/user/Desktop/app.bin`，29,547 字节。
- 用户指定地址：`0x08000000`，文件末端（不含）为 `0x0800736B`。
- 向量表初值：SP=`0x20001178`，Reset=`0x08007169`；这只是格式/范围检查，不证明文件真实来源。
- 项目：`D:/Chy/Code/儿童座椅/R157H/prod-mkyf-cy041e-r157h`。
- GCC 显示板 linker：`cmake/linker/apm32e030_app.ld`，代码区 `0x08000000` 起、长度 `0xF000`，末尾 KV 区不属于此次写入范围。
- IAR 显示板 linker：`EWARM_DISPLAY/apm32e030x8_flash.icf`，向量起点同为 `0x08000000`。
- 本次不构建、不伪造 `build_success`，不把用户提供 BIN 伪装成本工具构建产物。试验单独保存固件快照、哈希、来源声明、CLI 原始输出和串口证据。
- 先做 connect-only，检查实际工具输出；只在连接与身份无冲突时进行用户授权的单次写入和校验。不执行全片擦除、Option Bytes 或保护设置更改。

## 首次烧录验证结果：单次通过（未全片擦除）

已在当前实物显示板完成以下实际操作，无需人工换线：

1. **Connect-only 通过**：CubeProgrammer v2.16.0，SWD，NORMAL，SWrst，默认 SWD 频率 4000 kHz。设置 RTS=True，启动进程后等待 200 ms，再设置 False。该延迟是本次通过的候选值，不是跨板卡稳定时序范围。
2. **备份通过**：使用 `-u 0x08000000 0x10000 <backup.bin>` 读取原有 64 KB Flash，备份文件 `flash_before.bin` 为 65,536 字节。
3. **单次写入通过**：`-c port=SWD mode=NORMAL reset=SWrst -w <用户BIN快照> 0x08000000 -v`，退出码 0，实际输出 `File download complete`。
4. **校验通过**：同次命令明确输出 `Download verified successfully`。工具输出擦除 sectors `[0 28]`，没有执行全片擦除或 Option Bytes 修改。未额外比较写入范围以外 Flash 的前后内容，不能据此宣称参数区已经读回验证。
5. **复位启动日志通过**：校验结束后，True 保持 100 ms，再 False 释放；捕获到 `Reset: Software Reset`、`WELCOME TO ZOS`、版本 `1.0.0.0`、构建时间 `Oct 23 2026 14:31:01` 和 `open uart[0]`。
6. **资源清理通过**：尝试释放复位、关闭 COM7、关闭日志均成功。关闭后的物理 RTS/NRST 电平未测量；依据用户此前观察，关闭端口可能让板卡重新保持复位。此时没有后台脚本占用 COM7，需要保持运行时可由串口助手打开并保持 RTS=False，或按已确认的接线断开 RTS。

### 设备输出与型号边界

用户确认实物为 APM32E030；项目 IAR linker 注释为 APM32E030F8P6。本机 CubeProgrammer 实际显示 `Device ID: 0x440`、`Device name: STM32F05x/F030x8`、`Flash size: 64 KBytes`，ST-Link 固件为 V2J37S7。这是工具对兼容设备的识别输出，不把该名称作为芯片厂商/丝印的独立证明；不因此阻断用户已确认并实际通过的共用烧录方式。

### 原始证据

- Connect-only：`runs/20261023T065717058523Z_r157h_display_apm32_trial_2c324e3c/`。
- 备份、写入、校验和启动：`runs/20261023T065749683211Z_r157h_display_apm32_trial_31bfd724/`。
- 目录内保存 `run_config.yaml`、`run_summary.json`、`events.jsonl`、`backup.log`、`programmer.log`、`flash_before.bin`、`user_supplied_app.bin`、`external_image.json`、`serial.bin`、`serial.jsonl`、`serial.log`。
- 可版本化精简结果：`docs/tool-evidence/display_apm32_validation_2026-10-23.json`。
- 用户 BIN SHA-256：`703e4fb76b7b2a242d92902a2ea8c132b033eefcd8adcf5f4a7dc008e2c4e99d`。
- 写入试验串口共记录 1,025 个 RX 字节；已检查原始 BIN 与 serial.jsonl 中 RX 字节拼接完全一致。
- 共用 SWCLK 的接收流在 SWD 操作期间包含非 UART 字节；这是原始监听内容，不作为固件日志解码成功与否的判断依据。之后明确出现有效启动日志，原始字节未过滤/丢弃。

### 验收范围

本次为一次 connect-only、一次备份、一次写入/校验及启动日志检查。没有执行 20 次稳定性、其他板卡烧录或双向协议测试。桌面 BIN 通过一次性实机校准脚本调用现有底层适配器完成；没有伪造构建清单，也没有放宽正式 `flash/run` CLI 的构建身份检查。正式 CLI 对外部 BIN 的导入能力尚未开发。

## 随后新增的用户流程要求

上述单次实测完成后，用户要求 STM32/APM32 平台统一执行：**全片擦除 → 从 `0x08000000` 写入 → 校验 → 复位并接收日志**。软件已将全片擦除设为独立阶段，只有成功才启动写入；只连接实验仍不擦除。全片擦除包括参数/KV/Bootloader，不受此次 BIN 长度限制。先前的备份和“擦除 sectors [0 28]”属于历史试验，不能当作新全片擦除流程的实测成功证据。新增步骤随后获用户授权，在下面的独立试验中完成验证。

## 全片擦除新流程实测：单次通过

用户要求继续实机验证后，使用同一板卡、ST-Link、CH340 和桌面 BIN 完成一次独立试验。固件 SHA-256 与首次试验一致；每次启动 CLI 均按 True 保持复位、进程启动后 200 ms 切 False 释放。

1. **擦除前备份通过**：读取并保存当前 `0x08000000` 起的全部 65,536 字节；核对连接输出仍为已确认的 ST-Link、Device ID `0x440` 和 64 KB Flash，再允许擦除。
2. **全片擦除通过**：执行 `-c port=SWD mode=NORMAL reset=SWrst -e all`，退出码 0，明确输出 `Mass erase successfully achieved`。
3. **空白检查通过**：另一次只读命令将全部 64 KB 读回 `flash_after_erase.bin`，65,536 字节均为 `0xFF`。确认空白后才启动写入，不以退出码或成功文案单独代替该次物理内容检查。
4. **写入与 CLI 校验通过**：从 `0x08000000` 写入 29,547 字节的用户 BIN 快照，退出码 0，输出 `File download complete`、`Download verified successfully`。
5. **独立固件读回通过**：再读回 `0x08000000` 起的 29,547 字节，保存 `firmware_readback.bin`，与用户 BIN 快照逐字节完全一致。
6. **复位启动通过**：True 保持 100 ms 后切 False，接收到 `WELCOME TO ZOS`、版本 `1.0.0.0`、构建时间 `Oct 23 2026 14:31:01`、`open uart[0]`。接收流仍保留 SWD 共线期间的非 UART 字节。
7. **清理通过**：尝试释放复位、关闭 COM7、关闭日志全部成功，无后台脚本继续占用端口。关闭后 RTS/NRST 的物理电平未测量。

### 全片擦除试验证据和边界

- 运行目录：`runs/20261023T071143715782Z_r157h_display_apm32_trial_4aa03135/`。
- 关键文件：`flash_before.bin`、`backup.log`、`erase.log`、`flash_after_erase.bin`、`blank_check.log`、`programmer.log`、`firmware_readback.bin`、`readback.log`、`external_image.json`、`run_summary.json`、`events.jsonl`、三类串口日志。
- 可版本化摘要：`docs/tool-evidence/display_apm32_mass_erase_2026-10-23.json`。
- 本机实际擦除成功匹配文本：`Mass erase successfully achieved`；后续同版本 CLI 的 `erase_success_pattern` 可据此配置，不能代替其他工具版本实测。
- 全片擦除实际清除了原有内部 Flash；擦除前备份已保留，**没有自动恢复旧参数/KV**。固件启动后可能重新初始化参数，不能把空白检查理解为启动后的 Flash 仍全为 `0xFF`。
- 本次是一轮实机校准，调用现有底层适配器并保留外部 BIN 来源声明；没有伪造构建成功清单。正式 CLI 外部 BIN 导入、真实工程构建到协议测试闭环、STM32 实物和连续 20 次稳定性仍未验收。
