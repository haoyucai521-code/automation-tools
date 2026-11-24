"""离线检查人工整理的单板重复运行记录，不执行或认证硬件操作。"""
from embedded_auto.core.models import OperationResult


def evaluate_records(records):
    issues = []
    if not isinstance(records, list):
        return OperationResult(False, "stability evaluate", "CONFIG_INVALID", "记录必须是 JSON 数组")
    if len(records) != 20:
        issues.append("需要恰好 20 条记录")
    identity = None
    seen = set()
    stopped = False
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict):
            issues.append(f"第 {index} 条不是对象")
            stopped = True
            continue
        run_id = record.get("run_id")
        target, device, digest = record.get("target_id"), record.get("device_id"), record.get("sha256")
        start, size = record.get("erase_start"), record.get("erase_size")
        valid_id = all(isinstance(x, str) and x.strip() for x in (run_id, target, device, digest))
        valid_id = valid_id and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
        valid_id = valid_id and type(start) is int and type(size) is int and 0 <= start < 2**32 and 0 < size <= 2**32 - start
        valid_id = valid_id and type(record.get("duration_ms")) is int and record["duration_ms"] >= 0
        if not valid_id:
            issues.append(f"第 {index} 条身份、范围或耗时无效")
        if isinstance(run_id, str):
            if run_id in seen:
                issues.append(f"第 {index} 条 run_id 重复")
            seen.add(run_id)
        current = (target, device, digest, start, size)
        if identity is None:
            identity = current
        elif current != identity:
            issues.append(f"第 {index} 条设备、镜像或擦除范围不一致")
        if stopped:
            issues.append(f"第 {index} 条位于失败后；应停止并保留现场")
        if any(record.get(key) is not True for key in ("success", "verified", "boot_confirmed", "cleanup_succeeded")):
            issues.append(f"第 {index} 条未全部成功")
            stopped = True
    return OperationResult(not issues, "stability evaluate", None if not issues else "STABILITY_RECORD_INVALID",
                           "仅检查输入记录；不证明设备身份、硬件操作或稳定性验收",
                           data={"record_checks_passed": not issues, "issues": issues,
                                 "count": len(records), "hardware_stability_verified": False})
