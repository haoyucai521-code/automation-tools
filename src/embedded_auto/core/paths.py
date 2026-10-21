import re
from pathlib import Path

from .errors import ToolError


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", value):
        raise ToolError("CONFIG_INVALID", "标识符只能包含字母、数字、下划线、短横线和点")
    return value


def resolve_path(value, base: Path) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise ToolError("CONFIG_INVALID", "路径不能为空")
    p = Path(value).expanduser()
    return (base / p).resolve() if not p.is_absolute() else p.resolve()
