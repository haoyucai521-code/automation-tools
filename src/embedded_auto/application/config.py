"""现有配置格式的加载及跨领域校验，保持校验顺序和错误语义。"""
import copy
from pathlib import Path

from embedded_auto.core.config import integer, load_yaml, merge, number, pattern, text
from embedded_auto.core.errors import ToolError
from embedded_auto.core.paths import identifier, resolve_path
from embedded_auto.firmware.config import validate_build_config, validate_layout_config, validate_programmer_config
from embedded_auto.serial.config import validate_serial_config


def load_config(path: str | Path, target: str, overrides: dict | None = None) -> dict:
    path = Path(path).resolve()
    identifier(target)
    app = load_yaml(path)
    target_path = path.parent / "targets" / f"{target}.yaml"
    config = merge(app, load_yaml(target_path))
    if overrides:
        config = merge(config, overrides)
    for section in ("app", "target", "build", "flash", "reset", "startup", "serial"):
        if not isinstance(config.get(section), dict):
            raise ToolError("CONFIG_INVALID", f"缺少或无效的配置节: {section}")
    if config["target"].get("id") != target:
        raise ToolError("CONFIG_INVALID", "配置文件与 target.id 不一致")
    root = resolve_path(config["app"].get("workspace", ".."), path.parent)
    config["app"]["workspace"] = str(root)
    config["app"]["runs_dir"] = str(resolve_path(config["app"].get("runs_dir", "runs"), root))
    build = config["build"]
    build["cwd"] = str(resolve_path(build.get("cwd", "."), root))
    for key in ("project_file", "artifact"):
        if build.get(key):
            build[key] = str(resolve_path(build[key], Path(build["cwd"])))
    for section in ("build", "flash"):
        exe = config[section].get("executable")
        if isinstance(exe, str) and ("/" in exe or "\\" in exe):
            config[section]["executable"] = str(resolve_path(exe, root))
    config["sources"] = {"app": str(path), "target": str(target_path)}
    return config


def validate_config(config: dict, operation: str) -> None:
    """按原调用顺序校验，仅要求当前操作所需字段。"""
    app, target = config["app"], config["target"]
    identifier(target.get("id"))
    number(app.get("default_timeout_s", 60), "app.default_timeout_s", 0.001)
    build_needed = operation in ("build", "run")
    flash_needed = operation in ("flash", "run")
    serial_needed = operation in ("monitor", "reset", "connect", "flash", "test", "run")
    if build_needed:
        validate_build_config(config)
    if serial_needed:
        validate_serial_config(config)
    if flash_needed or operation == "connect":
        validate_programmer_config(config, operation)
    if flash_needed or build_needed:
        validate_layout_config(config, flash_needed)
    if operation == "run":
        startup = config["startup"]
        number(startup.get("timeout_s", 5), "startup.timeout_s", 0.001)
        if startup.get("ready_pattern"):
            pattern(startup["ready_pattern"], "startup.ready_pattern")
        else:
            number(startup.get("settle_delay_s", 1), "startup.settle_delay_s", 0)


def validation_issues(config: dict, operation: str) -> list[str]:
    try:
        validate_config(copy.deepcopy(config), operation)
        return []
    except ToolError as exc:
        return [str(exc)]
