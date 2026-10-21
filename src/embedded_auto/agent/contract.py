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
    "inspect_target": "device check",
    "build_firmware": "build",
    "flash_firmware": "flash",
    "reset_target": "reset",
    "capture_serial": "monitor",
    "run_protocol_tests": "test",
    "get_run_report": "report",
    "run_workflow": "run",
}

REPAIRABLE_ERRORS = frozenset({"BUILD_FAILED", "BOOT_TIMEOUT", "PROTOCOL_TEST_FAILED"})


def describe_contract():
    return {"version": 1, "result_schema": RESULT_SCHEMA, "tools": TOOLS,
            "error_codes": list(ERROR_CODES), "repairable_errors": sorted(REPAIRABLE_ERRORS)}
