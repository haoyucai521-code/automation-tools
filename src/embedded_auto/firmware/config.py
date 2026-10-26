"""现有固件构建、烧录和产物布局规则；由应用按原顺序调用。"""
from pathlib import Path

from embedded_auto.core.config import integer, number, pattern, text
from embedded_auto.core.errors import ToolError


def validate_build_config(config):
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


def validate_programmer_config(config, operation):
    target = config["target"]
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


def validate_layout_config(config, flash_needed):
    target = config["target"]
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
