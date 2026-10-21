"""有限、可审计的回调循环；不启动 Agent，不替用户授权硬件或修改代码。"""
import hashlib
from dataclasses import dataclass
from pathlib import Path

from embedded_auto.core.errors import ToolError
from embedded_auto.core.models import OperationResult
from embedded_auto.core.run_context import write_json
from .contract import REPAIRABLE_ERRORS


@dataclass(frozen=True)
class RepairPolicy:
    allowed_targets: tuple[str, ...]
    max_repairs: int = 3

    def __post_init__(self):
        if type(self.max_repairs) is not int or not 0 <= self.max_repairs <= 100:
            raise ToolError("CONFIG_INVALID", "max_repairs 必须是 0..100 的有限整数")
        if not self.allowed_targets or not isinstance(self.allowed_targets, tuple):
            raise ToolError("CONFIG_INVALID", "必须明确指定不可变目标白名单")

    def decision(self, target, result, repairs):
        if target not in self.allowed_targets:
            return "TARGET_NOT_ALLOWED"
        if result.success:
            return "PASS"
        if result.error_code not in REPAIRABLE_ERRORS:
            return "ENVIRONMENT_OR_HARDWARE_FAILURE"
        if repairs >= self.max_repairs:
            return "MAX_REPAIRS"
        return "REPAIR"


def fingerprint(paths):
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None for path in paths}


class RepairLoop:
    def __init__(self, policy: RepairPolicy, protected_paths=()):
        self.policy = policy
        # 工具源码、安全策略与调用方指定的断言/目标配置均不能被修复回调静默改写。
        tool_files = tuple(Path(__file__).resolve().parents[1].rglob("*.py"))
        self.protected_paths = tuple(dict.fromkeys((*tool_files, *(Path(p).resolve() for p in protected_paths))))

    def run(self, target, execute, repair, ctx):
        """execute()->OperationResult；repair(round, previous) 由外层显式提供。"""
        records = []
        result = OperationResult(False, "repair", "REPAIR_POLICY_VIOLATION", "目标不在本任务白名单内")
        if target not in self.policy.allowed_targets:
            return result
        sources = tuple(Path(p).resolve() for p in ctx.config.get("sources", {}).values())
        protected = tuple(dict.fromkeys((*self.protected_paths, *sources)))
        baseline = fingerprint(protected)
        repairs = 0
        reason = None
        try:
            while True:
                ctx.check()
                if fingerprint(protected) != baseline:
                    raise ToolError("REPAIR_POLICY_VIOLATION", "工具、目标配置或测试断言已改变，停止执行")
                result = execute()
                records.append({"verification": result.to_dict(), "repair_number": repairs})
                reason = self.policy.decision(target, result, repairs)
                if reason != "REPAIR":
                    break
                repairs += 1
                before = ctx.path / f"repair_{repairs:02d}_before"
                before.mkdir()
                ctx.snapshot_git(before)
                ctx.event("AGENT_REPAIR", repair_number=repairs, previous_run_id=result.run_id)
                repair(repairs, result)
                after = ctx.path / f"repair_{repairs:02d}_after"
                after.mkdir()
                ctx.snapshot_git(after)
                if fingerprint(protected) != baseline:
                    raise ToolError("REPAIR_POLICY_VIOLATION", "修复修改了受保护文件；保留现场并停止，不自动回滚用户工作")
        except ToolError as exc:
            result = OperationResult(False, "repair", exc.code, str(exc))
            reason = exc.code
        except KeyboardInterrupt:
            result = OperationResult(False, "repair", "CANCELLED", "用户中断修复循环")
            reason = "CANCELLED"
        except Exception as exc:
            result = OperationResult(False, "repair", "INTERNAL_ERROR", f"修复回调失败: {exc}")
            reason = "CALLBACK_FAILED"
        if reason == "MAX_REPAIRS":
            result = OperationResult(False, "repair", "REPAIR_LIMIT_REACHED", "达到最大修复轮数", data={"last_result": result.to_dict()})
        result.data.update({"repairs": repairs, "stop_reason": reason, "iterations": records})
        write_json(ctx.path / "repair_report.json", result.to_dict())
        return result
