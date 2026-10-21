# 故障排查

先查看 run_summary.json 的主错误码、data.cleanup、events.jsonl，再查看对应原始日志；不要用再次烧录覆盖失败证据。

| 错误 | 处理 |
| --- | --- |
| CONFIG_INVALID | 检查 TODO、布尔类型、精确芯片、地址来源、允许区域、串口电气核验、超时和整个测试套件；模板不可直接运行 |
| BUILD_TOOL_NOT_FOUND / BUILD_CONFIG_INVALID | 配置实际 IAR 路径或 GCC 工程 argv；不要把 shell 命令字符串当数组 |
| BUILD_FAILED / BUILD_TIMEOUT | 读 build.log；仅退出码 0 且有本次产物才继续，不烧录旧 BIN |
| ARTIFACT_STALE | 本次构建没有更新 BIN；检查目标是否真正生成 bin、工程/配置是否匹配。使用明确重建/导出命令，不修改时间戳伪造新产物 |
| ARTIFACT_* | 核对清单、快照路径、目标、SHA-256、大小、地址/来源和允许区域；修改后重新构建，不手工伪造清单 |
| FLASH_TOOL_NOT_FOUND | 使用 CubeProgrammer 完整路径；本机默认安装目录存在但不在 PATH |
| CONNECT_FAILED / CONNECT_TIMEOUT | 保留 CLI 输出和时间线，人工检查接线、电源与 RTS；先做 connect-only，不自动改成 Under Reset |
| FLASH_FAILED / FLASH_VERIFY_FAILED | 停止协议测试，检查输出、工具/芯片支持、地址及布局；校验失败不得算成功 |
| RESET_CONTROL_FAILED | 报告硬件状态未知，检查 NRST；清理仍尝试释放，失败必须人工处理 |
| SERIAL_PORT_BUSY / SERIAL_OPEN_FAILED | 关闭占用串口的其他程序，确认真实 COM；工具不强制关闭外部应用 |
| SERIAL_READ/WRITE/CONTROL/CLOSE_FAILED | 检查断线/驱动，查看 cleanup。RX 错误不应阻止仍可用端口最后尝试释放 NRST |
| SERIAL_BUFFER_RANGE | 消费窗口已超出内存缓冲，原始文件仍保留。合理调整 max_buffer_bytes 或缩短判定窗口 |
| BOOT_TIMEOUT | 查最后一次复位后的原始日志、编码和 ready_pattern；不要改成无断言就称已正常启动 |
| PROTOCOL_TEST_FAILED | 查 test_report.json 的 tx/rx hex、时间、偏移、逐条断言；判断真实响应与用例，不静默放宽预期 |
| WORKFLOW_TIMEOUT / CANCELLED | 查看子进程、NRST 与串口清理记录；再决定下一次动作 |
| CLEANUP_FAILED / PROCESS_CLEANUP_FAILED / LOG_WRITE_FAILED | 原始主故障仍须保留；检查磁盘、端口、PID/子进程、硬件状态。不要把报告生成失败当成测试通过 |
| REPAIR_LIMIT_REACHED / REPAIR_POLICY_VIOLATION | 停止修复，交付证据与剩余风险；不要自动提高上限或改测试 |

电源中断、操作系统终止进程或强杀 Python 后，软件无法保证最终复位电平。重新开始前人工确认 NRST、电源及后台工具，不能仅因未发现 Python 进程就认为硬件已恢复。
