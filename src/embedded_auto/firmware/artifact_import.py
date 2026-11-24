"""外部 BIN 的离线快照；完整性检查不等于固件或设备身份认证。"""
from pathlib import Path
import hashlib
import json
import shutil
import struct

from embedded_auto.core.config import integer, text
from embedded_auto.core.errors import ToolError
from embedded_auto.core.models import OperationResult
from embedded_auto.core.run_context import utc_now
from .build_service import _fingerprint, validate_artifact


def _vectors(image, config):
    flash = config["flash"]
    address = flash["address"]
    if address != 0x08000000:
        raise ToolError("ARTIFACT_IMPORT_INVALID", "未验证 Bootloader 偏移，拒绝导入")
    start = integer(flash.get("ram_start"), "flash.ram_start")
    size = integer(flash.get("ram_size"), "flash.ram_size")
    text(flash.get("ram_source"), "flash.ram_source")
    if start < 0 or size <= 0 or start + size > 2**32:
        raise ToolError("ARTIFACT_IMPORT_INVALID", "RAM 范围无效")
    with image.open("rb") as stream:
        header = stream.read(8)
    if len(header) != 8:
        raise ToolError("ARTIFACT_IMPORT_INVALID", "BIN 不含完整向量表头")
    sp, reset = struct.unpack("<II", header)
    if not (start < sp <= start + size and sp % 4 == 0 and reset & 1 and
            address <= (reset & ~1) < address + image.stat().st_size):
        raise ToolError("ARTIFACT_IMPORT_INVALID", "初始栈指针或复位向量不合理")
    return {"initial_sp": sp, "reset_vector": reset, "ram_start": start,
            "ram_size": size, "ram_source": flash["ram_source"]}


def validate_import_metadata(image, manifest, config):
    if (config["target"].get("status") != "verified" or
        not isinstance(manifest.get("source_description"), str) or
        not manifest["source_description"].strip() or
        not isinstance(manifest.get("imported_at"), str) or not manifest["imported_at"] or
        not isinstance(manifest.get("config_sources"), dict) or
        not all(isinstance(manifest["config_sources"].get(k), str) and manifest["config_sources"][k]
                for k in ("app", "target")) or
        manifest.get("config_sources") != config.get("sources") or
        manifest.get("run_id") != image.parent.name or
        any(key in manifest for key in ("build_success", "command", "build_started_at_ns"))):
        raise ToolError("ARTIFACT_IMPORT_INVALID", "导入清单来源或配置身份无效")
    if manifest["vector_check"] != _vectors(image, config):
        raise ToolError("ARTIFACT_IMPORT_INVALID", "导入向量检查与清单不一致")
    try:
        expected = (image.parent / "artifact_manifest.sha256").read_text(encoding="ascii").strip()
    except OSError as exc:
        raise ToolError("ARTIFACT_IMPORT_INVALID", "缺少导入清单完整性记录") from exc
    if expected != _manifest_digest(manifest):
        raise ToolError("ARTIFACT_IMPORT_INVALID", "导入清单完整性不匹配")


def _manifest_digest(manifest):
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def import_artifact(config, ctx, source, address, description):
    try:
        source = Path(source).resolve()
        text(description, "source")
        flash = config["flash"]
        for key in ("address", "region_start", "region_size"):
            flash[key] = integer(flash.get(key), f"flash.{key}")
        if integer(address, "address") != flash["address"]:
            raise ToolError("ARTIFACT_ADDRESS_MISMATCH", "声明地址与目标配置不一致")
        if config["target"].get("status") != "verified":
            raise ToolError("ARTIFACT_IMPORT_INVALID", "目标配置尚未核验")
        if source.suffix.lower() != ".bin" or not source.is_file():
            raise ToolError("ARTIFACT_IMPORT_INVALID", "输入必须是存在的普通 BIN 文件")
        before = _fingerprint(source)
        start, size = flash["region_start"], flash["region_size"]
        if (before["size"] <= 0 or size <= 0 or start < 0 or start + size > 2**32 or
                flash["address"] < start or flash["address"] + before["size"] > start + size):
            raise ToolError("ARTIFACT_IMPORT_INVALID", "输入为空或超出目标写入区域")
        snapshot = Path(ctx.path).resolve() / "firmware.bin"
        manifest_path = snapshot.parent / "artifact_manifest.json"
        digest_path = snapshot.parent / "artifact_manifest.sha256"
        if snapshot.exists() or manifest_path.exists() or digest_path.exists() or source == snapshot:
            raise ToolError("ARTIFACT_RUN_CONFLICT", "运行目录已有快照或清单")
        ctx.check()
        with source.open("rb") as src, snapshot.open("xb") as dst:
            shutil.copyfileobj(src, dst)
        copied = _fingerprint(snapshot)
        if _fingerprint(source) != before or any(copied[k] != before[k] for k in ("sha256", "size")):
            raise ToolError("ARTIFACT_CHANGED", "复制期间源文件改变")
        ctx.check()
        manifest = {"source_type": "external_import", "run_id": ctx.run_id,
                    "target_id": config["target"]["id"], "exact_part_number": config["target"]["exact_part_number"],
                    "address": flash["address"], "address_source": flash["address_source"],
                    "sha256": copied["sha256"], "size": copied["size"],
                    "source_path": str(source), "snapshot_path": str(snapshot),
                    "imported_at": utc_now(), "source_description": description,
                    "config_sources": config.get("sources"), "vector_check": _vectors(snapshot, config)}
        ctx.check()
        with manifest_path.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
        with digest_path.open("x", encoding="ascii") as stream:
            stream.write(_manifest_digest(manifest) + "\n")
        validate_artifact(snapshot, manifest, config)
        ctx.check()
        ctx.event("ARTIFACT_IMPORTED", sha256=copied["sha256"], size=copied["size"])
        result = OperationResult(True, "artifact import", message="导入快照已创建；不证明设备兼容性",
                                 data={"artifact": str(snapshot), "manifest_path": str(manifest_path),
                                       "manifest": manifest, "integrity_verified": True,
                                       "device_compatibility_verified": False})
    except (ToolError, OSError, ValueError, TypeError, KeyError) as exc:
        code = exc.code if isinstance(exc, ToolError) else "ARTIFACT_IMPORT_INVALID"
        result = OperationResult(False, "artifact import", code, str(exc), data=getattr(exc, "data", {}))
    try:
        return ctx.finish(result)
    except (OSError, ValueError, TypeError) as exc:
        result.run_id = ctx.run_id
        result.data["archive_error"] = str(exc)
        result.data.setdefault("run_path", str(ctx.path))
        if result.success:
            result.success, result.error_code, result.message = False, "ARCHIVE_FAILED", "导入结果归档失败"
        return result
