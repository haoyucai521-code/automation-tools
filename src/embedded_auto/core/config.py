"""安全加载配置。模板可预览，但未核验字段不能触发硬件动作。"""
import copy
import codecs
import math
import re
from pathlib import Path

import yaml

from .errors import ToolError
from .paths import identifier, resolve_path


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


def validate_config(config: dict, operation: str) -> None:
    """仅校验当前动作需要的字段，monitor 不要求烧录地址。"""
    app, target = config["app"], config["target"]
    identifier(target.get("id"))
    number(app.get("default_timeout_s", 60), "app.default_timeout_s", 0.001)
    build_needed = operation in ("build", "run")
    flash_needed = operation in ("flash", "run")
    serial_needed = operation in ("monitor", "reset", "connect", "flash", "test", "run")
    if build_needed:
        b = config["build"]
        if b.get("backend") not in ("gcc", "iar"):
            raise ToolError("CONFIG_INVALID", "build.backend 仅支持 gcc/iar")
        text(b.get("cwd"), "build.cwd")
        artifact = text(b.get("artifact"), "build.artifact")
        if Path(artifact).suffix.lower() != ".bin":
            raise ToolError("CONFIG_INVALID", "第一版只接受 .bin 产物")
        number(b.get("timeout_s", 60), "build.timeout_s", 0.001)
        if b["backend"] == "gcc":
            command = b.get("command")
            if not isinstance(command, list) or not command:
                raise ToolError("CONFIG_INVALID", "GCC command 必须为非空参数数组，不通过 shell 执行")
            for arg in command:
                text(arg, "build.command")
        else:
            for field in ("executable", "project_file", "configuration"):
                text(b.get(field), f"build.{field}")
    if serial_needed:
        s, r = config["serial"], config["reset"]
        text(s.get("port"), "serial.port")
        number(s.get("baudrate"), "serial.baudrate", 1)
        if s.get("bytesize", 8) not in (5, 6, 7, 8) or s.get("parity", "N") not in ("N", "E", "O", "M", "S") or s.get("stopbits", 1) not in (1, 1.5, 2):
            raise ToolError("CONFIG_INVALID", "串口帧参数无效")
        number(s.get("read_timeout_s", 0.1), "serial.read_timeout_s", 0.001, 1)
        number(s.get("write_timeout_s", 1), "serial.write_timeout_s", 0.001, 60)
        try:
            codecs.lookup(s.get("encoding", "utf-8"))
        except (LookupError, TypeError) as exc:
            raise ToolError("CONFIG_INVALID", "serial.encoding 不是有效文本编码") from exc
        if s.get("rtscts", False) is not False or s.get("dsrdtr", False) is not False:
            raise ToolError("CONFIG_INVALID", "RTS 复位模式禁止硬件流控")
        if type(r.get("asserted_rts_value")) is not bool:
            raise ToolError("CONFIG_INVALID", "reset.asserted_rts_value 必须是实测布尔值")
        for field in ("polarity_verified", "voltage_verified", "wiring_verified", "open_transient_verified"):
            if r.get(field) is not True:
                raise ToolError("CONFIG_INVALID", f"reset.{field} 未确认，禁止打开串口")
        number(r.get("pulse_ms", 100), "reset.pulse_ms", 0.001, 60000)
        number(r.get("post_pulse_delay_ms", 0), "reset.post_pulse_delay_ms", 0, 60000)
    if flash_needed or operation == "connect":
        f = config["flash"]
        text(f.get("executable"), "flash.executable")
        if f.get("interface") != "SWD" or f.get("mode") != "NORMAL" or f.get("reset_mode") not in ("SWrst", "HWrst", "Crst"):
            raise ToolError("CONFIG_INVALID", "当前适配器要求 SWD/NORMAL 及明确复位模式")
        # 克隆 ST-Link 的 NRST 不可依赖，禁止要求它进行硬复位。
        if f.get("reset_mode") == "HWrst":
            raise ToolError("CONFIG_INVALID", "当前 CH340 复位接线不支持 ST-Link HWrst")
        if f.get("cli_verified") is not True:
            raise ToolError("CONFIG_INVALID", "须先核对本机 CubeProgrammer 帮助和 CLI 参数")
        if operation != "connect" and (f.get("compatibility_verified") is not True or target.get("status") != "verified"):
            raise ToolError("CONFIG_INVALID", "此目标未完成 connect-only 兼容性核验")
        if operation != "connect" and config["reset"].get("timing_verified") is not True:
            raise ToolError("CONFIG_INVALID", "复位连接时序尚未实测")
        number(config["reset"].get("release_delay_ms"), "reset.release_delay_ms", 0, 60000)
        number(f.get("timeout_s", 60), "flash.timeout_s", 0.001)
        pattern(f.get("connect_success_pattern"), "flash.connect_success_pattern")
        text(target.get("exact_part_number"), "target.exact_part_number")
    if flash_needed or build_needed:
        f = config["flash"]
        text(target.get("exact_part_number"), "target.exact_part_number")
        if flash_needed and f.get("verify") is not True:
            raise ToolError("CONFIG_INVALID", "第一版禁止跳过写入校验")
        for key in ("address", "region_start", "region_size"):
            f[key] = integer(f.get(key), f"flash.{key}")
        if f["address"] != 0x08000000:
            raise ToolError("CONFIG_INVALID", "当前 STM32/APM32 流程要求固件从 0x08000000 开始")
        if not 0 <= f["region_start"] <= f["address"] < f["region_start"] + f["region_size"] <= 2**32:
            raise ToolError("CONFIG_INVALID", "烧录地址和允许区域无效")
        text(f.get("address_source"), "flash.address_source")
        if flash_needed:
            for key in ("erase_success_pattern", "write_success_pattern", "verify_success_pattern"):
                pattern(f.get(key), f"flash.{key}")
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
