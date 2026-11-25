from embedded_auto.core.errors import ERROR_CODES


RESULT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "EmbeddedAutoResult",
    "type": "object",
    "required": ["success", "operation", "error_code", "message", "duration_ms", "run_id", "log_path", "data"],
    "properties": {
        "success": {"type": "boolean"},
        "operation": {"type": "string"},
        "error_code": {"type": ["string", "null"]},
        "message": {"type": "string"},
        "duration_ms": {"type": "integer", "minimum": 0},
        "run_id": {"type": ["string", "null"]},
        "log_path": {"type": ["string", "null"]},
        "data": {"type": "object"},
    },
}

TOOLS = {
    "preflight_firmware": "mcu preflight",
    "connect_target": "mcu connect",
    "reset_target": "mcu reset",
    "flash_firmware": "mcu program",
    "read_serial": "serial read",
    "send_serial": "serial send",
    "capture_serial": "serial capture",
    "scan_ble": "ble scan",
    "inspect_ble": "ble inspect",
    "read_ble": "ble read",
    "write_ble": "ble write",
    "notify_ble": "ble notify",
    "inspect_target": "device check",
    "build_firmware": "build",
    "import_artifact": "artifact import",
    "get_run_report": "report",
    "evaluate_stability": "stability evaluate",
}

TOOL_GROUPS = {
    "mcu": ("preflight_firmware", "connect_target", "reset_target", "flash_firmware"),
    "serial": ("read_serial", "send_serial", "capture_serial"),
    "ble": ("scan_ble", "inspect_ble", "read_ble", "write_ble", "notify_ble"),
}

OPTIONAL_TOOLS = {
    "run_protocol_tests": "test",
    "run_ble_suite": "ble test",
    "run_workflow": "run",
}

TOOL_EFFECTS = {
    "preflight_firmware": "只检查指定 BIN 与全片擦除计划，不连接或授予授权",
    "connect_target": "复位辅助连接，可能打断固件，不擦除或写入",
    "reset_target": "一次 RTS 复位脉冲，不擦除或写入",
    "flash_firmware": "全片擦除、写入、强制校验及复位；不得将预检数据当成授权",
    "read_serial": "限时接收并返回有界预览，不发送；打开 COM 可能影响 RTS",
    "send_serial": "发送一次，可在同一会话监听；收到数据不证明是响应",
    "capture_serial": "限时接收并保存日志，不发送；打开 COM 可能影响 RTS",
    "scan_ble": "只扫描，不连接或写入",
    "inspect_ble": "连接并查看服务与特征",
    "read_ble": "读指定特征",
    "write_ble": "写指定特征，写入成功不证明业务已执行",
    "notify_ble": "在有限窗口内接收指定特征通知，不发送写入",
}

REPAIRABLE_ERRORS = frozenset({"BUILD_FAILED", "BOOT_TIMEOUT", "PROTOCOL_TEST_FAILED"})


def describe_contract():
    return {"version": 2, "result_schema": RESULT_SCHEMA, "tools": TOOLS, "tool_groups": TOOL_GROUPS,
            "optional_tools": OPTIONAL_TOOLS, "tool_effects": TOOL_EFFECTS,
            "execution_policy": "只执行用户要求的动作，不自动继续或重试；实机动作需明确授权，授权不由 Agent 自行生成。",
            "error_codes": list(ERROR_CODES), "repairable_errors": sorted(REPAIRABLE_ERRORS)}
