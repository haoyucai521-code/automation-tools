from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


def json_value(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


@dataclass
class OperationResult:
    success: bool
    operation: str
    error_code: str | None = None
    message: str = ""
    duration_ms: int = 0
    log_path: Path | str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None

    def to_dict(self):
        return json_value(asdict(self))


class WorkflowState(StrEnum):
    IDLE = "IDLE"
    VALIDATE_CONFIG = "VALIDATE_CONFIG"
    BUILD = "BUILD"
    VALIDATE_ARTIFACT = "VALIDATE_ARTIFACT"
    OPEN_SERIAL = "OPEN_SERIAL"
    ERASE_FLASH = "ERASE_FLASH"
    FLASH_ERASE_SUCCEEDED = "FLASH_ERASE_SUCCEEDED"
    WRITE_FLASH = "WRITE_FLASH"
    ASSERT_RESET = "ASSERT_RESET"
    START_PROGRAMMER = "START_PROGRAMMER"
    RELEASE_RESET_AT_CONFIGURED_TIME = "RELEASE_RESET_AT_CONFIGURED_TIME"
    WAIT_FLASH_AND_VERIFY = "WAIT_FLASH_AND_VERIFY"
    WAIT_BOOT = "WAIT_BOOT"
    RUN_TESTS = "RUN_TESTS"
    PASS = "PASS"
    FAILED = "FAILED"
    CLEANUP = "CLEANUP"
    ARCHIVE = "ARCHIVE"
