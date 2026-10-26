"""跨领域 YAML、合并与基础值校验；不加载领域配置。"""
import copy
import math
import re
from pathlib import Path

import yaml

from .errors import ToolError


def load_yaml(path: Path) -> dict:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ToolError("CONFIG_INVALID", f"无法读取配置 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ToolError("CONFIG_INVALID", f"配置必须是映射: {path}")
    return value


def merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


def number(value, name, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        raise ToolError("CONFIG_INVALID", f"{name} 必须是范围内的有限数值")
    return value


def integer(value, name):
    try:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError()
        return int(value, 0) if isinstance(value, str) else value
    except (TypeError, ValueError):
        raise ToolError("CONFIG_INVALID", f"{name} 必须是整数或十六进制地址") from None


def text(value, name):
    if not isinstance(value, str) or not value.strip() or "TODO" in value or value.lower() == "unverified":
        raise ToolError("CONFIG_INVALID", f"{name} 尚未配置或确认")
    return value


def pattern(value, name):
    text(value, name)
    try:
        re.compile(value)
    except re.error as exc:
        raise ToolError("CONFIG_INVALID", f"{name} 正则无效: {exc}") from exc
