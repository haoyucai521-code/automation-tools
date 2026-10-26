# BLE UUID 读写与通知工具：开发与测试方案

日期：2026-10-26。状态：**待实施的开发方案，不是已实现功能或验收结论。**

本方案基于已拆分的 `application / serial / firmware / core / adapters` 结构重新编写，替代已删除的旧草案。当前只有 Windows 电脑，没有可供测试的 BLE 外设，也没有 UUID 表。先交付可离线演示和测试的通用工具；真实蓝牙链路与设备符合性验收留到有设备且获得操作授权以后。

当前实现以 [项目设计](../docs/design/PROJECT_DESIGN.md) 为准，实施遵守 [编码规范](../docs/CODING_GUIDE.md)。本文的新模块、命令、配置和错误码均为拟定契约；本轮只写文档，不创建包、不安装依赖、不扫描或连接设备。

## 1. 目标与首版范围

### 1.1 要解决的问题

1. 没有 UUID 表时，发现设备的 GATT 服务、特征、句柄和声明的读写/通知属性。
2. 选择指定服务与特征，执行读取、显式模式写入、通知订阅及限时采集。
3. 有预期数据时按确定性规则判定；没有预期时只记录操作结果，不宣称“设备功能正确”。
4. 无设备时，以独立 Fake 外设模型演示完整流程，并验证工具自身的逻辑、异常处理和证据链。
5. 后续替换为 Windows Bleak 适配器，不改领域测试和结果契约。

### 1.2 MVP 必须包含

- 配置及全套件预检；UUID 仅接受完整的 128 位标准格式，规范化为小写。
- `scan / inspect / read / write / notify / test` 六个 BLE 子命令。
- BLE 专用 Backend Protocol、FakeBackend、领域会话、原始事件与报告。
- 基础字节断言：`equals_hex / contains_hex / length`；同一步的全部断言都必须满足。
- 特征存在性/必需属性检查，写权限门槛，通知 checkpoint，超时、取消、有界缓存和完整清理。
- Bleak 薄适配器及 Mock 契约验证；可选安装，不影响固件/串口或 Fake 的基础安装。
- 复用现有 `OperationResult`、`RunContext` 和通用 `report --json`，不重复造一套报告入口。

### 1.3 明确后置

`inspect --draft-suite`、独立 `ble report`、16/32 位 UUID 简写、数值字段 DSL、正则/脚本断言、`expect_error`、强制 indicate/auto 模式、描述符手工读写、自动配对/解除配对、自动重连、自动重发、写后恢复、GUI、多设备并发、固件构建/烧录联动。

首版不做 BLE 射频、链路层或 Bluetooth SIG 认证测试。Fake 不是 Windows 虚拟蓝牙外设，不会出现在系统蓝牙列表中；不承诺仅靠一台电脑完成真实 GATT client/server 无线闭环。

## 2. 验证分层与结论边界

| 层级 | 内容 | 当前条件下能否完成 | 合格结论 |
| --- | --- | --- | --- |
| L0 | Schema、纯规则、结果映射、依赖边界 | 可以 | 工具静态规则和纯逻辑通过 |
| L1 | Fake 全流程及故障注入 | 可以 | 模拟模式的工具流程通过 |
| L2 | Mock Bleak、打包和 CLI 集成 | 可以；锁定依赖后验证 | 适配调用契约通过，不是 WinRT/无线链路通过 |
| L3 | Windows + 蓝牙适配器 + 真实外设 | 没有外设，待验收 | 指定环境的扫描/GATT/清理通过 |
| L4 | 真实外设 + 明确 UUID/数据/触发规格 | 缺外设及规格，待验收 | 指定规格下设备读写/通知符合预期 |

- 每次运行的 `data.backend`、`data.simulated` 和 `data.validation_level` 必须明确；Fake 的 `PASS` 不得省略“模拟”标记。
- L3/L4 在当前计划中记为“未执行：缺设备/规格”，不是通过，也不让工具悄悄切换 Fake。
- 首版离线交付完成不等于设备测试工具已经实机验收。没有数据规格时，真实模式也只能验证操作和观察到的内容。
- 仓库已有结构拆分回归记录为 132 项通过，属于前一任务证据，见 [验收记录](../docs/VALIDATION.md)。真正开始 BLE 实施时必须重新运行基线，本文不把该历史数量当作未来结果。

## 3. 新结构上的落点与依赖

### 3.1 拟定文件

以下文件只在获得实施授权后按阶段创建，不预建占位包。

| 路径（相对仓库根） | 职责 |
| --- | --- |
| `src/embedded_auto/ble/models.py` | 候选设备、服务/特征快照、订阅事件和判定数据 |
| `src/embedded_auto/ble/interfaces.py` | BLE 专用后端接口，不导入 Bleak/WinRT |
| `src/embedded_auto/ble/config.py` | BLE 配置、Fake profile、套件预检和完整 UUID 规范化 |
| `src/embedded_auto/ble/session.py` | 单连接/单事件循环、特征定位、通知入口和生命周期 |
| `src/embedded_auto/ble/test_runner.py` | 套件步骤、字节断言、checkpoint 窗口及逐项证据 |
| `src/embedded_auto/ble/evidence.py` | BLE 原始事件归档和有界入口/窗口计数 |
| `src/embedded_auto/adapters/ble/fake.py` | 独立于 suite 预期的模拟外设及故障注入 |
| `src/embedded_auto/adapters/ble/bleak_backend.py` | Bleak 对象转换、错误映射、Windows 平台差异 |
| `src/embedded_auto/application/ble_composition.py` | 根据显式 backend 选择并注入 Fake/Bleak |
| `src/embedded_auto/application/ble_workflow.py` | BLE 配置加载、运行上下文、步骤顺序、清理与归档 |
| `src/embedded_auto/application/ble_cli.py` | 六个子命令的注册、路由和 BLE 专属文本/JSON 呈现 |
| `configs/ble/demo.yaml`、`profiles/demo.yaml`、`suites/demo.yaml` | 离线示例，后两者相对 `configs/ble/` |
| `tests/test_ble_*.py` | 纯逻辑、Fake、CLI、Mock 和可选实机测试 |

必要的 `__init__.py` 随实际模块创建。现有 `cli.py` 只增加薄分派；现有 `core/errors.py` 仅在实施时登记新增稳定错误码，不把 BLE 业务实现放进 core。

### 3.2 依赖规则

```text
CLI → application.ble_cli / ble_workflow / ble_composition
                → ble 领域 → core 基础设施
                → adapters.ble（后端构造并注入）
```

- `ble` 不依赖 `serial`、`firmware`、旧 `services`、`_compat` 或应用装配，不直接导入具体 adapters/Bleak。
- `serial`、`firmware`、`core` 不反向导入 BLE。不能复用串口 `SerialPort`、RTS/DTR、字节流 offset 或串口 TestRunner。
- Fake 与 Bleak 实现同一个 BLE 专用接口；不抽象 `UniversalSession/UniversalTransport`。
- 基础安装保持现有依赖。真实模式拟用可选 extra；Fake、旧 CLI 和普通 core 导入均不得隐式导入 Bleak。
- `application/config.py` 仍负责原固件配置，不强行用它加载 BLE。应用层为 BLE 单独加载配置，再调用 `ble/config.py` 校验；现有 target/config/命令语义不变。

### 3.3 RunContext：直接使用已通用化接口

BLE 应用装配归一化后的最小配置，调用已有入口，不修改其固件兼容分支：

```python
ctx = RunContext(
    config, "ble.test", timeout_s,
    run_label="ble-fake",
    evidence_hint="BLE 原始事件: ble_events.jsonl；判定: ble_test_report.json",
)
```

其中 `config` 只需包含本次归一化后的 `app` 和 `ble` 等实际配置；**不伪造 `target.id: ble`，不要求 build/flash/reset/serial**。`ble-fake`/`ble-real` 符合现有标识符规则，operation 显式使用 `ble.scan` 等稳定名称。

- 保留现有 `run_config.yaml / events.jsonl / run_summary.json / agent_summary.md` 归档机制。
- BLE 应用不调用 `snapshot_git()`；现有固件/修复流程的显式 Git 快照不变。
- 现有摘要中的 PASS/FAIL 只跟随 `success`。BLE 应用传入含“操作完成/断言状态/模拟标记”的清晰 message；BLE CLI 自己呈现判定，不复用固件 CLI 文本 PASS 来宣称设备合格。
- 通用 `report --config configs/ble/demo.yaml --run-id <id> --json` 已按 `app.workspace/runs_dir` 读取归档，不需要 target。首版只验证复用，不新增 `ble report`，不改旧 report 输出语义。

## 4. 后端与会话契约

接口均为 async；类型使用领域 dataclass、bytes 和 Callable，禁止在领域模型中暴露 `BLEDevice/BleakGATTCharacteristic/WinRT` 对象。

| 接口 | 输入/输出及职责 |
| --- | --- |
| `scan(timeout_s)` | 返回本次扫描的候选元数据和不透明 candidate ID；适配器内部保留原始设备对象 |
| `connect(candidate_id, timeout_s)` | 只连接本次扫描映射中的唯一候选，不按名字重新搜索或隐式二次 discover |
| `discover()` | 返回当前连接有效的服务/特征 UUID、句柄、属性及可获知的写长度上限 |
| `read(characteristic_ref)` | 返回 bytes；真实适配器采用已冻结的读取策略，记录缓存请求策略，不宣称绕过所有系统缓存 |
| `write(characteristic_ref, data, response)` | response 必须显式；返回正常只说明该模式的操作完成 |
| `subscribe(characteristic_ref, callback)` | 返回订阅 token；回调边界保持，一次 callback 对应一条事件 |
| `unsubscribe(token)`、`disconnect()` | 有界清理，失败可诊断；允许会话层避免重复清理同一已完成资源 |

- 同一命令使用一次 `asyncio.run()`，扫描、连接、订阅、等待和断开在同一事件循环内；不按每条操作重新创建循环。
- `BleSession` 持有连接、特征映射和订阅注册表；测试与日志消费同一事件入口，不分别向后端订阅同一个特征。
- 后端回调只复制数据并提交领域入口，不直接运行断言或写文件；后台任务必须登记、取消并回收。
- 句柄/characteristic_ref 只在当前连接有效，不能把旧快照中的句柄直接写入新连接。
- 连接中断后本轮失败并清理，禁止自动连接另一个同名设备或重放写命令。
- Mock 验证 `connect()` 等返回 `None` 也属于正常返回，不使用 `if not await client.connect()` 判断连接失败。此约束依据官方客户端 API，见参考 B1。

## 5. 真实设备选择与写入安全

### 5.1 唯一选择规则

1. 真实模式 `--device` 只接受 Windows 当前扫描可返回的完整地址标识，MVP 规范化为大写冒号分隔 MAC 格式。Fake 对应值为 `fake:demo`。两者不能互相代替。
2. `--name` 如提供，只用于扫描过滤/展示，不能作为连接目标，更不能按名字自动选择第一台设备。
3. 每次真实命令先进行有界新扫描；零匹配报 `BLE_DEVICE_NOT_FOUND`，不唯一报 `BLE_DEVICE_AMBIGUOUS`，均不连接。
4. 唯一匹配后，记录本次 `scan_id/candidate_id/address/name`，适配器把 candidate ID 解析为**本次扫描实际对象**交给连接 API。不能把历史 JSON 当作可连接对象，不能给 Bleak 地址字符串触发隐式扫描，依据见 B1/B2。
5. 地址变化、设备消失、名字相同或旧 candidate ID 失效都停止；由用户重新查看扫描结果、显式更新选择，不自动“猜同一台”。

### 5.2 写入前确认

- 所有写入，包括 Fake，必须在本次调用给 `--allow-write`；不得把这个授权写进持久配置。
- 真实 `write` 或含 write 步骤的 `test` 还要求 `--confirm-device <address>`，值必须与 `--device` 及本轮唯一扫描对象的地址完全匹配。不能填名称、列表行号、通配符或历史 candidate ID。
- 用户先查看 `scan` 输出，再显式确认具体地址。执行命令仍重新扫描并校验该地址；确认只对本轮扫描解析出的对象生效，不复用旧连接。
- 连接前/写入前把已解析目标及写模式呈现在文本输出；JSON 模式只在最终 stdout 输出一个 JSON，过程目标写到 stderr 和归档，不能破坏机器可读输出。
- 配置可保存预期地址、名称过滤和服务过滤；本轮 candidate ID、Bleak 对象、订阅 token、确认授权不能持久化复用。
- 地址/名称匹配不是设备身份认证，无法排除地址复用或伪装。强身份认证需要设备规格和安全协议，首版不宣称提供。

### 5.3 写操作含义

写入前先解析全部套件并检查授权，再连接、发现并检查目标和属性；运行中不因某个写失败去尝试另一个模式。

- `with-response` 要求 `write` 属性；`without-response` 要求 `write-without-response`。不依赖 Bleak 的省略 response 默认值，依据见 B1。
- 一次写调用仅对应一次提交，不自动拆包、不重试。写长度需同时满足所选 API 的上限和当前特征可获知的上限；无法证明超长写可用就阻止该操作，不用固定“20 字节”假设所有设备。
- with-response 正常返回不等于业务生效；without-response 正常返回更不证明对端确认。报告保留 `write_mode/submitted/ack_scope`，后者只说明 API/模式所提供的确认范围。
- 写超时、取消或断连后，数据可能已经被接收；记录 `write_effect=unknown`，禁止自动重发或恢复旧值。
- `notify` 的订阅会配置远端通知状态，不是“零副作用观察”；但不属于用户业务数据写入，不要求 `--allow-write`。将来执行真实订阅仍需明确硬件操作授权。

## 6. CLI 与配置契约

### 6.1 命令与行为

以下为拟定接口，当前均不可当作已实现命令执行。

| 命令 | 行为 | 默认判定 |
| --- | --- | --- |
| `embedded-auto ble scan` | 限时扫描，记录候选及广播元数据；不连接 | 操作结果，NOT_EVALUATED |
| `embedded-auto ble inspect` | 唯一目标连接，枚举服务/特征/属性；不自动读取全部特征 | 操作结果，NOT_EVALUATED |
| `embedded-auto ble read` | 对指定 service/characteristic 读取一次 | 无预期为 NOT_EVALUATED；有预期才判断 |
| `embedded-auto ble write` | 显式写模式、单次写入；不隐式读回 | 只提交为 NOT_EVALUATED |
| `embedded-auto ble notify` | 限时订阅并记录独立回调事件；结束取消订阅 | 无预期为 NOT_EVALUATED；有预期才判断 |
| `embedded-auto ble test` | 完整预检后执行 suite；遇首个 FAIL/ERROR 停止，后续 SKIP | 按 suite 断言汇总 |

公共参数：`--config / --backend fake|bleak / --device / --timeout / --json / --dry-run`；scan 使用 `--scan-timeout` 和可选 `--name`。read/write/notify 使用 `--service`、`--characteristic`；write 增加 `--hex`、`--write-mode with-response|without-response`、`--allow-write`，真实写增加 `--confirm-device`。read/notify 可给 `--expect-hex`；notify 的 `--duration` 是采集上限，test 使用 `--suite` 显式文件路径。

- `--backend` 覆盖配置，未指定时取配置值；示例配置为 fake。缺依赖或真实设备时不得回退 Fake。
- 在固件 `--target` 检查和固件配置加载之前分派 BLE；不能要求 BLE 填写 MCU 参数。
- BLE 文本显示“操作完成 / 断言 PASS/FAIL/NOT_EVALUATED / 模拟或真实”；JSON 顶层字段不改变。
- `--dry-run` 只解析与生成执行计划，不构造 Bleak 客户端、不扫描、不连接、不写、不订阅，也不实际跑 Fake；授权和配置问题列入 blocking_issues。计划生成成功不是断言通过。
- argparse 参数语法错误退出 2；正常完成退出 0；操作错误、断言失败、取消或清理失败退出 1。其他旧命令保持原行为。

### 6.2 离线配置样例

拟保存为 `configs/ble/demo.yaml`：

```yaml
version: 1
app:
  workspace: "../.."
  runs_dir: "runs"
  default_timeout_s: 30
ble:
  backend: fake
  device: "fake:demo"
  fake_profile: "profiles/demo.yaml"
  scan_timeout_s: 3
  connect_timeout_s: 5
  operation_timeout_s: 2
  cleanup_timeout_s: 5
  ingress_limits:
    max_events: 128
    max_payload_bytes: 65536
  window_limits:
    max_events: 256
    max_payload_bytes: 131072
```

路径规则必须冻结：`workspace` 相对配置文件目录，`runs_dir` 相对归一化后的 workspace；`fake_profile` 相对配置文件目录；CLI `--suite` 相对调用目录。不通过改变工作目录解析路径，不新增 suite ID 搜索规则。

应用将 workspace/runs_dir 等路径归一化后才交给 RunContext。通用 report 读取同一配置，会得到相同的运行根目录。真实 backend 不要求 fake_profile，Fake 必须提供它；未知 backend/字段、非有限或非正超时、非法容量均预检失败。

### 6.3 独立 Fake 外设描述

拟保存为 `configs/ble/profiles/demo.yaml`：

```yaml
version: 1
device:
  id: "fake:demo"
  name: "BLE Tool Demo"
services:
  - uuid: "12345678-1234-5678-1234-56789abc0000"
    characteristics:
      - uuid: "12345678-1234-5678-1234-56789abc0001"
        properties: [read, write, notify]
        initial_hex: "01 00"
        on_write:
          store: true
          emit_notification: true
```

Fake 只支持明确列出的固定行为，不执行配置里的 Python/表达式。`on_write` 与 suite 的 expect 独立：即使 expect 改错，Fake 也发送同样的实际数据；不得从断言生成“正确响应”。写入更新值，发送一次通知，允许在 write await 返回前发生回调。每次命令初始化新外设状态，不能把上次写值带入下一轮制造偶然通过。

故障注入先用于测试夹具中的受控参数，不为演示 profile 添加一套复杂脚本语言。Fake 不包含模拟 GATT 服务就绪等同 Windows 硬件通过的结论。

### 6.4 最小套件样例

拟保存为 `configs/ble/suites/demo.yaml`：

```yaml
version: 1
characteristics:
  echo:
    service_uuid: "12345678-1234-5678-1234-56789abc0000"
    uuid: "12345678-1234-5678-1234-56789abc0001"
    required_properties: [read, write, notify]
tests:
  - name: "echo_read_write_notify"
    steps:
      - action: read
        characteristic: echo
        expect: {equals_hex: "01 00", length: 2}
      - action: subscribe
        characteristic: echo
      - action: checkpoint
        id: before_write
        characteristic: echo
      - action: write
        characteristic: echo
        hex: "A5 5A"
        response: true
      - action: wait_notification
        characteristic: echo
        after: before_write
        timeout_s: 2
        expect: {equals_hex: "A5 5A", length: 2}
      - action: read
        characteristic: echo
        expect: {equals_hex: "A5 5A", length: 2}
```

规则：

- 全部步骤和别名必须在第一次扫描前完成语法预检；每例至少一个非空 expect。不存在的别名、未知 action/rule、重复 checkpoint ID、引用尚未建立或来自其他特征的 checkpoint 都报 CONFIG_INVALID。
- 初次发现后统一检查整个 suite 所需特征和声明属性；发现不满足预期时 FAIL，后续写入不发生。读写操作还要做自身能力检查，不能只信配置。
- UUID 以 service+characteristic 二元组定位；不同服务的同 UUID 可区分。若仍有重复服务实例/重复特征，MVP 报歧义而不选第一个；inspect 保留句柄作诊断，跨连接句柄定位后置。
- `subscribe` 必须先于该特征的 checkpoint/wait；步骤均有 operation_timeout，wait 可设置更短的局部 timeout；全部受运行总截止时间限制。
- `response` 必须是布尔值；写 hex 非空。`contains_hex` 预期非空，`length` 为非负整数；`equals_hex` 可显式期望空数据。空 read 可以匹配明确空预期，但**没有通知事件**不能因 length=0 或空 equals_hex 而通过。
- 多规则全部满足才 PASS；通知规则针对同一条 callback payload，不拼接多条事件来凑出成功数据。
- 首例失败停止，所有已建立订阅由 finally 清理。该套件验证了模拟数据与窗口匹配，不证明写入和通知的严格因果关系。

离线拟定调用示例（均不在本轮执行）：

```powershell
embedded-auto ble scan --config configs/ble/demo.yaml --json
embedded-auto ble inspect --config configs/ble/demo.yaml --json
embedded-auto ble test --config configs/ble/demo.yaml --suite configs/ble/suites/demo.yaml --allow-write --json
```

## 7. 通知时间窗口、容量与生命周期

### 7.1 入口 checkpoint 与匹配

- 为每次连接建立 epoch；为每次订阅建立 generation/token；通知进入工具回调时，在锁内分配递增 ingress_seq，并复制 bytes、记录 monotonic_ns。锁也用于建立 checkpoint，避免“已进入队列但还没消费”的旧事件越过标记。
- checkpoint 保存该特征/订阅 generation 对应的入口高水位。wait 只考虑当前连接、当前订阅、`ingress_seq > checkpoint` 的事件，并受该 wait 的截止时间约束。
- 序号在入口分配，不能等队列消费时才分配；write 返回前到达的事件也可在之后的 wait 中匹配。
- 记录器与订阅注册表必须先准备好，再调用后端 start_notify，避免初始回调没有接收者。suite 的显式 checkpoint 可以主动排除订阅建立期间的数据；普通 notify 的采集入口在调用 start_notify 前准备就绪。
- 错特征、旧连接、旧订阅、checkpoint 之前和有效窗口之外的数据不满足断言；仍尽可能归档并注明排除原因。
- 操作时限用 monotonic 时钟；UTC 用于报告展示，不用于判断先后。Fake 单元测试用可控时钟/调度，不靠不稳定的超短 sleep 断言精确耗时。

**证明范围必须收窄：**只能证明“工具回调入口 checkpoint 之后、有效窗口内收到了符合预期的事件”。其他来源可能产生同值通知；甚至系统已收到、但稍后才交付工具回调的数据也可能落入窗口。仅凭时间和相同字节不能证明“由本次写入导致”。严格因果关联需设备协议内事务 ID/序号及相应规格，后续另立需求。

### 7.2 两组独立容量

| 配置 | 限制对象 | 满时行为 |
| --- | --- | --- |
| `ingress_limits` | 已进入工具、尚未被消费者归档处理的通知队列；分别累计事件数和 payload 字节数 | 不阻塞回调、不无声丢弃；设置粘滞错误 BLE_EVENT_OVERFLOW，记录拒收计数并让本轮失败 |
| `window_limits` | 已消费后保留、供 wait 查询的内存事件窗口；独立累计数量和字节数 | 可淘汰最旧事件；被查询 checkpoint 已过期时显式 BLE_WINDOW_EXPIRED，不能只在剩余事件中找匹配并报 PASS |

无界列表、无界 async queue 和“只保留最后一条通知”都不接受。示例数值是初始安全预算，不是性能指标。上限与内存计数需同时覆盖单条超大 payload；不能仅限制数量。

原始 JSONL 归档不因窗口淘汰而截断。入口溢出时明确报告证据不完整及丢失计数，不声称全量/无损采集。日志写失败用 LOG_WRITE_FAILED 结束，不静默继续给出成功判定。

### 7.3 失败、取消与清理

固定次序：禁止新步骤 → 逐一尝试取消订阅 → 尝试断开 → 有界等待/回收已登记任务 → 处理可用的剩余队列和 flush → 汇总清理结果 → 归档。某项失败不能阻止其他清理动作。

- 后端 subscribe/connect 超时或部分失败也要收束已创建的客户端、回调注册和任务，不能只清理“正常返回”的资源。
- 意外断连回调记录原因；后续测试停止。旧回调晚到不进入新订阅断言，不重新打开连接。
- 总截止时间到达后仍执行 finally；清理使用独立的 `cleanup_timeout_s` 总预算，不在清理入口调用已过期的 ctx.check()。预算耗尽记清理失败，不无限等待。
- 原主失败保留；清理失败另列 `data.cleanup`，不能把断言 FAIL 改成 PASS，也不能用后来的清理异常覆盖原错误。
- 先通过且清理失败时，保留已完成断言，但整体 success=false、CLEANUP_FAILED。归档失败也不能返回成功；若磁盘不可写，stderr/最终返回尽可能保留主错误和归档失败信息。
- 进程强杀、断电、驱动/native 调用无法取消时不保证硬件状态；报告已请求清理和实际结果，不承诺物理链路必然恢复。

## 8. 结果、错误和退出码：闭合映射

不再引入独立的 `execution_status`。保留现有 `OperationResult` 顶层结构；新增内容只放 data：

```text
success / operation / error_code / message / duration_ms / log_path / data / run_id
data:
  backend, simulated, validation_level
  verdict: PASS | FAIL | NOT_EVALUATED
  device, cases, cleanup, evidence_paths, evidence_complete
```

步骤 `status` 只表示 `COMPLETED / ERROR / SKIP`，步骤的 `verdict` 表示断言结果。断言 FAIL 也是“已完成判定”，不再混成执行异常。

| 实际结果 | 步骤 status / verdict | 顶层 success / error_code / 退出码 |
| --- | --- | --- |
| scan/inspect/read/写提交正常，但没有断言 | COMPLETED / NOT_EVALUATED | true / null / 0；仅“操作完成” |
| 必需断言全部完成且通过，清理成功 | COMPLETED / PASS | true / null / 0；Fake 必须带模拟标记 |
| API 正常，读值或通知内容不符合预期 | COMPLETED / FAIL | false / BLE_ASSERTION_FAILED / 1 |
| wait 的整个有效窗口没有匹配事件 | COMPLETED / FAIL | false / BLE_ASSERTION_FAILED / 1；保留已观察事件或空观察证据 |
| suite 声明的必需 UUID/属性在发现后不满足 | COMPLETED / FAIL | false / BLE_ASSERTION_FAILED / 1；未开始写入 |
| 本地属性不支持所请求操作，或选择歧义 | ERROR / NOT_EVALUATED | false / 对应能力/歧义错误 / 1 |
| 缺依赖、扫描/连接异常、远端拒绝、意外断连、日志/队列故障 | ERROR / NOT_EVALUATED | false / 对应操作错误 / 1 |
| 总超时或用户取消 | ERROR / NOT_EVALUATED | false / WORKFLOW_TIMEOUT 或 CANCELLED / 1 |
| 已完成的断言全 PASS，但清理失败 | 保留原步骤及 PASS | false / CLEANUP_FAILED / 1；data.verdict 可保留 PASS |
| 已有 FAIL/ERROR，随后清理也失败 | 保留原判定；后续 SKIP | false / 保留原主错误 / 1，另附 cleanup |
| 全部用例均未执行，没有任何断言证据 | SKIP / NOT_EVALUATED | false / BLE_SUITE_NOT_EXECUTED / 1 |
| dry-run 计划已生成，未执行任何测试 | 无执行步骤 / NOT_EVALUATED | true / null / 0；data.dry_run=true，不是 suite PASS |
| 配置/套件预检失败 | 不执行 / NOT_EVALUATED | false / CONFIG_INVALID / 1；argparse 语法错误为 2 |

汇总规则：任一已评估断言 FAIL 则汇总 FAIL；否则只有**所有必需断言均已执行且通过**才为 PASS；缺步骤/断言未执行则为 NOT_EVALUATED。断言全通过但清理失败可以 verdict=PASS、success=false，两者不可互相替代。`success` 仍是自动化调用方判断该命令整体成功的字段。

MVP **不支持 `expect_error`**。远端拒绝即 ERROR，不能被当作负向测试 PASS；本地属性阻止、缺依赖和断连更不能伪装为远端拒绝。未来增加负向断言，必须另行定义错误来源、可匹配范围和状态转换表后再扩展 schema。

拟定错误目录在 D1 冻结：沿用 CONFIG_INVALID、WORKFLOW_TIMEOUT、CANCELLED、CLEANUP_FAILED、LOG_WRITE_FAILED、INTERNAL_ERROR；新增 BLE_DEPENDENCY_MISSING、BLE_DEVICE_NOT_FOUND、BLE_DEVICE_AMBIGUOUS、BLE_CONNECT_FAILED、BLE_GATT_FAILED、BLE_CHARACTERISTIC_NOT_FOUND、BLE_CHARACTERISTIC_AMBIGUOUS、BLE_CAPABILITY_UNSUPPORTED、BLE_REMOTE_REJECTED、BLE_DISCONNECTED、BLE_OPERATION_TIMEOUT、BLE_EVENT_OVERFLOW、BLE_WINDOW_EXPIRED、BLE_ASSERTION_FAILED、BLE_SUITE_NOT_EXECUTED。

适配器保留原异常类型及可获取的平台/ATT 状态；只有可靠的远端拒绝证据才归类 BLE_REMOTE_REJECTED，不按模糊英文关键字猜测。本轮不修改实际 ERROR_CODES。

## 9. 证据与 Mock/真实适配约束

### 9.1 每次运行归档

| 文件 | 内容 |
| --- | --- |
| `run_config.yaml` | 已选 backend、归一化路径及运行参数；不含可复用的写入授权 |
| `events.jsonl` | RunContext 高层状态事件 |
| `ble_scan.json` | 本轮扫描 ID、候选元数据及选择过程 |
| `ble_gatt.json` | 本轮服务/特征快照、属性、句柄及查询策略；失败时标明未取得 |
| `ble_events.jsonl` | TX 请求/完成、RX read/通知、连接/订阅变化、错误，保留回调边界和原始 hex |
| `ble_test_report.json` | 单步输入、断言、实际事件引用、verdict、超时与 SKIP 原因 |
| `run_summary.json`、`agent_summary.md` | 顶层结果、模式、判定、证据路径和清理状态 |

原始事件至少含 run_id、backend、event_id、epoch、订阅 generation、service/characteristic UUID、方向/事件类型、UTC、monotonic_ns、payload_hex/length；通知另有 ingress_seq。摘要引用事件 ID，不只复制一句 PASS。

仅按实际取得的数据生成证据：语法错误或连接失败时不伪造 GATT/数据文件；没有 suite 时可以不生成 test_report。hex 必须可还原原始 bytes，不用解码文本替代。日志可能含地址和设备数据，不自动提交或发送外部服务。

### 9.2 Bleak 薄适配器

- 只使用已冻结版本的公开客户端/扫描 API；不继承 Bleak 私有 BaseBackend 实现领域接口。其内部后端 API 不被视为稳定接口，见 B4。
- 当前查询的官方 latest 文档页面标为 3.0.2，但这是资料版本，不是本机已安装或已锁定版本。D0/D5 核对 Python、Windows build、包元数据和依赖，再选兼容版本/范围，不写不受控的“最新版”。
- 官方不同页面的 Windows 支持措辞并不完全一致：后端总览列 Windows 11 build 22000+，Windows 具体页面仍有 Windows 10 描述。MVP 不据此承诺支持所有 Windows 10/11；锁版后的支持矩阵和实机证据才是验收依据，见 B3/B4。
- 单独核对 Windows asyncio/COM apartment 环境。没有 GUI 消息循环的 CLI 不通过盲目调用 allow_sta() 绕过检查；遇到 STA 冲突先按官方说明诊断，不修改全局安全/系统配置，见 B3。
- 通知首版只接受声明 `notify` 的特征；indicate-only 明确不支持，不自动升级为强制 indication。回调数据不是空口抓包，不能证明底层包类型或重传行为。
- 不自动配对或解除配对，不清除 Windows 蓝牙缓存，不改变蓝牙开关/驱动；认证失败返回错误并留下原始原因。
- Mock 返回 bytearray/None、重复 UUID、部分连接失败、回调先于 await 返回、断连及晚到回调等真实 API 形态。Mock 不模拟“已经实际通过 WinRT”。

## 10. 详细测试矩阵

以下是**计划场景**，不是已执行的 unittest 数量。一个场景可拆成多个参数化子测试；实施后以原始日志记录真实通过、失败、跳过数量。

### 10.1 L0/L1：规则、Fake 与误报防护

| ID | 场景 | 必须验证的结果 |
| --- | --- | --- |
| P01 | 正常配置、中文/空格路径、覆盖值 | 路径相对基准固定，原始输入不被意外修改 |
| P02 | 缺配置节、未知字段/backend/action/rule | CONFIG_INVALID；后端 scan/connect/write 调用次数均为 0 |
| P03 | UUID 大小写、非法字符、16/32 位简写 | 完整格式规范化；错误/简写在触硬件前拒绝 |
| P04 | NaN/Inf/布尔超时、非正容量、非法 hex/长度 | 明确拒绝；不接受 bool 冒充 int |
| P05 | suite 后面一例语法错误，前面含 write | 整体预检失败，第一条 write 也不执行 |
| P06 | 错别名、重复/前向/跨特征 checkpoint | 扫描前拒绝，不等到运行中才失败 |
| P07 | 空 suite、无 expect 的用例、空 contains | 不允许把空测试定义判为 PASS |
| P08 | 缺写授权、真实缺确认、Fake profile缺失 | 预检阻断；dry-run 列明阻塞，不实际执行 |
| S01 | Fake scan/inspect 正常、scan 无候选 | scan 可返回空观察；inspect 无目标失败，不伪造设备 |
| S02 | 零/多个目标匹配 | NOT_FOUND/AMBIGUOUS，connect 0 次 |
| S03 | 两台同名设备、只提供 name | 不按名称自动选择；不能写第一台 |
| S04 | 地址变化、历史 candidate ID、失效扫描对象 | 不重用、不回退；必须重新明确选择 |
| S05 | 地址确认与实际唯一扫描对象不一致 | 写 0 次，保留选择差异 |
| S06 | 重复 UUID：不同服务与同服务歧义 | 前者按二元组定位；后者明确失败，不静默取第一项 |
| S07 | suite 必需特征/属性缺失 | 前置断言 FAIL；套件写 0 次 |
| W01 | 已知初值读取，含零字节/不可解码数据 | 原始 bytes/hex 完整，所有断言同时满足才 PASS |
| W02 | 正确执行但故意改错 expect | 必须 FAIL，证明 Fake 不从 expect 生成响应 |
| W03 | read 明确空预期、长度0 | 实际空 read 可匹配；缺 read 事件不能满足 |
| W04 | with-response 与 without-response 属性检查 | 显式布尔模式正确传递；不自动换模式 |
| W05 | 正常写后读回与单纯提交 | 前者有数据断言；后者 NOT_EVALUATED，不宣称业务生效 |
| W06 | 当前写上限、超上限与不可确认上限 | 允许范围内单次写；超限阻断，不自动分包/重试 |
| W07 | 写已提交后超时/取消/断连 | write_effect=unknown；本次提交最多一次，不自动恢复 |
| W08 | 远端拒绝与本地属性拒绝/缺依赖 | 来源分开；都不因 expect_error 而 PASS，首版无此字段 |
| N01 | 订阅后正常通知、无通知直到超时 | 前者匹配；后者 FAIL，不拿空 payload 判定 |
| N02 | 回调先于 write 返回、后续 wait 才开始 | checkpoint 后有效事件可命中，不因返回次序丢失 |
| N03 | checkpoint 前已有排队旧事件 | 即使消费发生在 checkpoint 后也不能命中 |
| N04 | 错特征、旧 epoch、旧 generation、取消后晚到 | 不进入当前断言，诊断可定位排除原因 |
| N05 | 两条 payload 拼起来才符合预期 | 不拼接，不制造 PASS |
| N06 | 有效窗口内不匹配，之后到来同值事件 | 不能用超时后的事件追认通过 |
| N07 | 其他来源在窗口内产生同值事件 | 可匹配观察，但报告不能写“已证明写入导致” |
| N08 | 入口数量/字节/单事件超限 | 粘滞溢出失败，记录拒收计数，不声称证据完整 |
| N09 | 窗口淘汰后查询旧 checkpoint | WINDOW_EXPIRED；磁盘原始事件不因内存淘汰截断 |
| N10 | 订阅过程中部分失败及 start_notify 前后边界 | 已注册回调/资源有清理，初始事件有接收入口 |
| R01 | 操作正常但无断言 | success=true、NOT_EVALUATED，文本不称设备测试通过 |
| R02 | 断言全通过且清理成功 | success=true、PASS；Fake 模拟标记不可省略 |
| R03 | 断言 FAIL、后续步骤 | 退出1、保留实际数据，后续 SKIP |
| R04 | 全部 SKIP、尚未执行任何断言 | 不能汇总 PASS；SUITE_NOT_EXECUTED 或明确原主错误 |
| R05 | PASS 后 unsubscribe/disconnect 失败 | verdict 保留，整体 CLEANUP_FAILED/退出1 |
| R06 | 主错与多个清理错同时出现 | 保留主错误，剩余清理仍执行，分别记录结果 |
| R07 | 在 scan/connect/read/write/wait 及总截止取消 | 退出1、无无限等待、无遗留已登记任务 |
| R08 | 事件落盘、flush 或最终归档失败 | 不返回成功；尽可能保留原错和证据路径 |

### 10.2 L2：集成、隔离与适配契约

| ID | 场景 | 必须验证的结果 |
| --- | --- | --- |
| I01 | 无 Bleak 安装运行旧 CLI/core/Fake | 正常工作；不隐式导入可选依赖 |
| I02 | 显式选择 bleak 但缺依赖 | BLE_DEPENDENCY_MISSING，不回退 Fake |
| I03 | 独立进程导入 core/serial/firmware/ble | 无反向领域依赖，不从 _compat/services 进入 BLE |
| I04 | Mock Bleak None 返回、bytearray、重复 UUID、断连回调 | 领域对象转换/错误映射正确，连接不按返回值真值误判 |
| I05 | 六个 CLI 命令、JSON stdout、退出码、dry-run | 不要求 MCU target；JSON单对象；dry-run 对真实 API 调用0次 |
| I06 | 通用 report 读取 Fake/真实标签的历史结果 | 不操作硬件；使用相同 runs_dir；保留原 success/verdict |
| I07 | 同一事件循环、不同命令新循环及 Windows 环境 Mock | 资源不跨循环复用，不盲目绕过 COM 检查 |
| I08 | wheel 内容、可选 extra、旧112+结构回归 | 新模块/入口被打包，原功能及旧 Python 入口预期不变 |

### 10.3 L3/L4：有设备后才执行

| ID | 真实场景 | 证据与门槛 |
| --- | --- | --- |
| H01 | Windows build/适配器/驱动/依赖版本核对 | 建立本次环境清单；不自动改设置或配对记录 |
| H02 | 同名/地址变化/设备关闭情况下扫描与选择 | 候选原始记录；不得误连或写另一台 |
| H03 | 枚举与指定 UUID 单次读取 | 与已知设备规格或独立工具结果对照；无规格仅记观察 |
| H04 | 获准的无害写入、模式/长度边界与读回 | 写入数据、地址、模式、提交次数及回读原始数据 |
| H05 | 有明确触发方法的通知及取消订阅 | 触发规格、事件边界/内容/超时证据；不以同值推出因果 |
| H06 | 测试中关闭设备/链路中断/取消 | 有界结束、无自动重发、清理结果与未知写影响 |
| H07 | 未授权特征/认证要求、缓存变化 | 记录真实 ATT/平台错误；不自动配对、解配或清缓存 |
| H08 | 获准设备上重复运行20次 | 每轮 run_id/结果/清理均保留；不能用单次成功代替稳定性 |

没有设备时这8项全部待验收；不要通过接入无关周边设备来凑实机结果。接收真实 UUID 表后再确定安全操作、预期值、触发动作及 L4 用例，不猜测写入控制命令。

## 11. 开发阶段、产物和完成门槛

| 阶段 | 工作与交付 | 退出条件 |
| --- | --- | --- |
| D0：重新立基线 | 核对当前结构/工作树，运行原全套测试，记录 Python/Windows 环境与实际原始日志 | 与现有用户工作隔离；不把历史132当作新跑结果；不操作硬件 |
| D1：冻结契约 | 确定本方案配置/suite/接口/错误表、唯一设备选择、结果映射和容量边界 | 评审可逐行解释状态和授权；无 expect_error/数值DSL等隐含扩张 |
| D2：纯规则与 Fake 读闭环 | BLE 模型/接口、配置预检、独立 profile、scan/inspect/read 和原始事件 | 不安装 Bleak 即可演示；错误预期必失败；L0及相关L1通过 |
| D3：写与通知核心 | 单次写授权、订阅、入口checkpoint、有界队列/窗口、取消清理 | 写返回前通知、旧事件、溢出及清理叠加故障有回归 |
| D4：CLI/套件/归档 | 六命令、suite runner、RunContext和通用report集成 | Fake示例可运行，结果表无误报，旧CLI/原回归保持通过 |
| D5：Bleak薄适配 | 兼容版本选择、可选extra、Mock API、Windows边界及打包验证 | L2完成；仍不得写“Windows蓝牙实机通过” |
| D6：条件具备后实机 | 明确外设/UUID规格/允许动作，逐项执行H矩阵 | L3/L4分别验收；当前缺设备，不纳入离线完成声明 |

D1–D5 可按小步提交候选变更供审阅，但未经用户明确要求不执行 Git commit/push。先做 Fake MVP，不并行堆叠 GUI、草稿生成或扩展断言。

工作量粗估为6–10个有效开发日，包含回归及文档，不含设备等待、真实协议资料获取和驱动/设备异常排查；这是规划量级，不是交付日期承诺。

## 12. 执行、回归与验收方法

### 12.1 实施时的软件检查

使用本机 `python`，不为 `python3` 名称约定安装第二套解释器。标准命令：

```powershell
python -m unittest discover -s tests -v
python -m unittest discover -s tests -p "test_ble_*.py" -v
git diff --check
```

- 开始实现前和合并候选结束时都执行完整回归。新增测试增加真实计数，但不得删除/放宽原测试预期来迁就新模块。
- 普通测试进程默认只跑纯逻辑、Fake、Mock；真实测试必须独立 opt-in，同时要求明确目标配置和硬件授权。无条件跳过的硬件项必须在日志给理由，不能把测试框架退出0写成实机验收通过。
- 对通知先后/取消/容量边界，用可控调度与事件屏障确定复现；关键竞态场景可重复20轮，记录每轮结果。重复次数不替代覆盖。
- 检查 AST 与独立进程实际 imports；验证普通 core、serial、firmware、Fake 在 Bleak 不可导入时仍可用。
- 修改 pyproject optional extra/包结构时验证 wheel 包内容、CLI entry point 和干净环境导入；不要顺带升级全仓依赖。
- 当前结构整理留下的用户改动、`runs/.gitkeep` 删除和 `docs/plans/` 等用户工作不得被恢复/重写。

### 12.2 离线 MVP 验收

1. 在没有 BLE 外设、无需蓝牙打开、未安装 Bleak 的环境，Fake演示完整发现/读取/写入/通知断言和报告。
2. 同一 profile 配正确预期能通过，改错预期必须失败；无预期只显示操作完成。
3. dry-run不创建真实后端资源；缺依赖/缺设备不自动切换模式。
4. L0/L1/L2矩阵有原始证据，错误/跳过/清理状态可追溯，JSON与文本不混淆模拟和真实。
5. 原有固件/串口CLI、配置、JSON、错误及安全行为保持；无新领域反向依赖。
6. 文档明确“L3/L4未执行：缺设备/规格”，不以Fake或Mock覆盖该缺口。

### 12.3 文档路由

本文件始终是开发计划，不提前改写“当前实现”或“验收通过”。真正实现各阶段后，按项目路由只更新受影响内容：

- README：安装/可选extra及导航。
- PROJECT_DESIGN：真实已存在的模块、依赖和资源所有权。
- USER_GUIDE：真实可用的命令、配置及安全操作。
- VALIDATION：本轮真实测试数量、日志、Fake/Mock/实机边界。

## 13. 本次重新规划如何落实旧评审

| 原问题 | 本版处理 |
| --- | --- |
| 最小BLE配置不能传入旧RunContext | 使用结构拆分后的run_label/evidence_hint；不再伪造target；保留固件上下文行为 |
| execution_status/verdict/success重复且expect_error未闭合 | 删除execution_status；固定结果表；expect_error后置，远端拒绝不算PASS |
| 真实设备选择不明确 | 完整地址、本轮唯一新扫描对象、name仅过滤、显式写授权/地址确认、禁止旧candidate复用 |
| checkpoint被当作因果证明 | 定义回调入口窗口与其局限；明确同值/延迟交付都不能证明写入导致 |
| MVP范围过大 | 六命令+基础字节断言；复用report；草稿/简写/数值DSL/指示模式后置 |
| max_events/max_payload_bytes含义混淆 | ingress_limits和window_limits独立配置、计数和故障语义 |

## 14. 官方依据与需复核项

资料核对日期：2026-10-26。仅依据官方文档，未因此安装或调用蓝牙库。以下页面内容可能更新，D5锁版时保存实际版本及相关契约证据；不能把在线latest页面等同项目依赖锁定。

- **B1**：[BleakClient公开API](https://bleak.readthedocs.io/en/latest/api/client.html)：扫描对象连接、None返回、显式写模式、长度约束、重复UUID定位及通知回调。
- **B2**：[BleakScanner公开API](https://bleak.readthedocs.io/en/latest/api/scanner.html)：候选设备/广播数据和扫描生命周期；广播名称可能需要后续扫描响应，不能依赖首包名称完成身份判断。
- **B3**：[Windows后端说明](https://bleak.readthedocs.io/en/latest/backends/windows.html)：WinRT、缓存选项及COM apartment注意事项。
- **B4**：[平台支持及后端API边界](https://bleak.readthedocs.io/en/latest/backends/index.html)：平台支持范围、内部后端API稳定性声明。

仍需实施前确认：本机Windows具体build及Python兼容范围、选择的Bleak版本/依赖、真实适配器/驱动、是否需要认证配对、设备安全可写范围、通知触发方法和业务预期。前两项可在无外设时核对；后几项不阻塞Fake MVP，但阻塞对应实机/符合性结论。
