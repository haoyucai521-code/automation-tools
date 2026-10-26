from dataclasses import asdict, dataclass, field
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
