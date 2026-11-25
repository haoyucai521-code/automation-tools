"""离线核对具体烧录快照；不创建会话、启动程序或授予授权。"""
import copy
import json
import shutil
from pathlib import Path

from embedded_auto.core.errors import ToolError
from embedded_auto.core.models import OperationResult
from embedded_auto.firmware.build_service import validate_artifact
from embedded_auto.firmware.config import validate_layout_config
from embedded_auto.firmware.erase_policy import validate_erase_scope
from .config import validate_config


def load_flash_artifact(config, artifact, manifest_path=None):
    if artifact is None:
        raise ToolError("ARTIFACT_MISSING", "必须明确指定产物")
    image = Path(artifact).resolve()
    manifest_file = Path(manifest_path).resolve() if manifest_path else image.parent / "artifact_manifest.json"
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        checked = validate_artifact(image, manifest, config)
    except (OSError, ValueError) as exc:
        raise ToolError("ARTIFACT_INVALID", f"无法核对产物及其身份清单: {exc}") from exc
    return image, manifest, checked


def check_programmer_ready(config, remaining):
    # 调用前应已校验烧录器及相关超时配置。这里只核对本机路径和预算，不执行 CLI。
    flash = config["flash"]
    executable = shutil.which(flash["executable"])
    if not executable:
        raise ToolError("FLASH_TOOL_NOT_FOUND", "烧录工具路径不存在或不可执行")
    if config["reset"]["release_delay_ms"] / 1000 >= min(flash.get("timeout_s", 60), remaining):
        raise ToolError("FLASH_CONFIG_INVALID", "复位释放延迟超出阶段或剩余总预算")
    return executable


def preflight(config, ctx, artifact, manifest_path=None):
    candidate = copy.deepcopy(config)
    issues = []
    data = {"offline_checks_passed": False, "hardware_actions_executed": False,
            "device_identity_verified": False, "tool_version_verified_this_run": False,
            "authorization_required": True, "authorization_granted": False,
            "blocking_issues": issues, "operation_reviewed": "mcu program",
            "target_id": candidate["target"].get("id"),
            "exact_part_number": candidate["target"].get("exact_part_number"),
            "preserve_regions": candidate["flash"].get("preserve_regions", []),
            "warning": "全片擦除会清除整个内部 Flash，可能丢失参数、KV 和 Bootloader"}

    def check(stage, action):
        ctx.check()
        try:
            value = action()
        except ToolError as exc:
            issues.append({"stage": stage, "error_code": exc.code, "message": str(exc), "data": exc.data})
        else:
            ctx.check()
            return value

    def configured_programmer():
        validate_config(candidate, "flash")
        return check_programmer_ready(candidate, ctx.remaining)

    def concrete_artifact():
        # 与串口/工具检查独立：即使 COM 配置未就绪，也能报告已验证的 BIN 身份。
        validate_layout_config(candidate, False)
        _, _, checked = load_flash_artifact(candidate, artifact, manifest_path)
        return {key: checked[key] for key in ("image", "run_id", "sha256", "size", "address", "address_source")}

    data["artifact"] = check("artifact", concrete_artifact)
    data["erase_scope"] = check("erase_scope", lambda: validate_erase_scope(candidate))
    data["programmer_path"] = check("configuration", configured_programmer)
    ctx.check()
    data["offline_checks_passed"] = not issues
    return OperationResult(not issues, ctx.operation, issues[0]["error_code"] if issues else None,
                           "离线预检存在阻断项" if issues else "离线检查通过；未连接设备或获得烧录授权",
                           data=data)
