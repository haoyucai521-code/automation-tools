"""破坏性擦除门槛：授权仅由当前调用传入，不读取持久配置或环境变量。"""
from embedded_auto.core.config import integer, text
from embedded_auto.core.errors import ToolError


def validate_erase_scope(config):
    flash, target = config["flash"], config["target"]
    policy = flash.get("erase_policy")
    if policy != "mass_erase":
        raise ToolError("FLASH_ERASE_SCOPE_UNVERIFIED", "仅支持明确配置的 mass_erase；范围擦除和免擦模式未核验")
    regions = flash.get("preserve_regions", [])
    if not isinstance(regions, list):
        raise ToolError("FLASH_REGION_CONFLICT", "preserve_regions 必须为数组")
    if regions:
        raise ToolError("FLASH_REGION_CONFLICT", "全片擦除与任何保留区冲突")
    try:
        start = integer(flash.get("physical_flash_start"), "flash.physical_flash_start")
        size = integer(flash.get("physical_flash_size"), "flash.physical_flash_size")
        region_start = integer(flash.get("region_start"), "flash.region_start")
        region_size = integer(flash.get("region_size"), "flash.region_size")
        address = integer(flash.get("address"), "flash.address")
        text(flash.get("physical_flash_source"), "flash.physical_flash_source")
        text(target.get("exact_part_number"), "target.exact_part_number")
        if (target.get("status") != "verified" or start < 0 or size <= 0 or
            start + size > 2**32 or region_size <= 0 or region_start < start or
            region_start + region_size > start + size or not region_start <= address < region_start + region_size):
            raise ValueError("物理 Flash 范围与目标写入区域不一致")
    except (ToolError, ValueError) as exc:
        raise ToolError("FLASH_ERASE_SCOPE_UNVERIFIED", str(exc)) from exc
    return {"target_id": target.get("id"), "exact_part_number": target["exact_part_number"],
            "address": address, "erase_policy": "mass_erase", "erase_start": start, "erase_size": size}


def validate_erase_policy(config, artifact=None, authorization=None):
    expected = validate_erase_scope(config)
    expected["sha256"] = artifact.get("sha256") if artifact else None
    numeric_fields = ("address", "erase_start", "erase_size")
    if (not isinstance(authorization, dict) or authorization != expected or artifact is None or
            any(type(authorization.get(key)) is not int for key in numeric_fields)):
        raise ToolError("FLASH_DESTRUCTIVE_AUTH_REQUIRED", "须为本次完整目标、镜像、地址及全片范围提供精确授权",
                        {"required_binding": expected})
    return expected
