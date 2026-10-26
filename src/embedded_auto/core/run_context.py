import json
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .errors import ToolError
from .models import OperationResult, json_value
from .paths import identifier


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(json_value(value), ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


class RunContext:
    def __init__(self, config: dict, operation: str, timeout_s: float | None = None,
                 *, run_label: str | None = None, evidence_hint: str | None = None):
        self.config = config
        self.operation = operation
        self.evidence_hint = evidence_hint
        self.started = time.monotonic()
        self.started_at = utc_now()
        timeout_s = config["app"].get("default_timeout_s", 60) if timeout_s is None else timeout_s
        from .config import number
        number(timeout_s, "timeout", 0.001)
        self.deadline = self.started + timeout_s
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        # 旧调用继续以 target.id 命名；新调用可直接给运行标签，无需伪造 MCU 配置。
        target = identifier(run_label if run_label is not None else config.get("target", {}).get("id", operation))
        self.run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}_{target}_{uuid.uuid4().hex[:8]}"
        self.path = Path(config["app"]["runs_dir"]) / self.run_id
        self.path.mkdir(parents=True, exist_ok=False)
        (self.path / "run_config.yaml").write_text(yaml.safe_dump(json_value(config), allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.event("IDLE", operation=operation)

    @property
    def remaining(self):
        return max(0.0, self.deadline - time.monotonic())

    def check(self):
        if self.cancelled.is_set():
            raise ToolError("CANCELLED", "操作已取消")
        if self.remaining <= 0:
            raise ToolError("WORKFLOW_TIMEOUT", "运行总超时")

    def sleep(self, seconds):
        end = time.monotonic() + seconds
        while True:
            self.check()
            left = end - time.monotonic()
            if left <= 0:
                return
            self.cancelled.wait(min(left, self.remaining, 0.05))

    def event(self, state, **data):
        event = {"timestamp": utc_now(), "elapsed_ms": int((time.monotonic() - self.started) * 1000), "state": str(state), **json_value(data)}
        with self._lock, (self.path / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def snapshot_git(self, destination: Path | None = None):
        destination = destination or self.path
        # 保留固件历史 cwd 选择，未配置构建的普通运行使用 workspace。
        cwd = self.config.get("build", {}).get("cwd", self.config["app"]["workspace"])
        metadata = {}
        for name, args in (("commit", ["rev-parse", "HEAD"]), ("status", ["status", "--porcelain=v1"]), ("diff", ["diff", "HEAD", "--", "."])):
            try:
                proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=min(5, max(.01, self.remaining)))
                output = proc.stdout.decode("utf-8", "replace")
                if name == "diff":
                    if proc.returncode:
                        # 空仓库没有 HEAD，保留已跟踪的工作区和暂存区差异。
                        proc = subprocess.run(["git", "diff", "--", "."], cwd=cwd, capture_output=True, timeout=5)
                        output = proc.stdout.decode("utf-8", "replace")
                    (destination / "git_diff.patch").write_text(output, encoding="utf-8")
                else:
                    metadata[name] = output.strip() if proc.returncode == 0 else None
            except (OSError, subprocess.SubprocessError) as exc:
                metadata[name] = {"unavailable": str(exc)}
        write_json(destination / "git_state.json", metadata)
        return metadata

    def finish(self, result: OperationResult):
        result.run_id = self.run_id
        result.duration_ms = int((time.monotonic() - self.started) * 1000)
        result.data.setdefault("run_path", str(self.path))
        result.data.setdefault("started_at", self.started_at)
        result.data["finished_at"] = utc_now()
        self.event("ARCHIVE", success=result.success, error_code=result.error_code)
        write_json(self.path / "run_summary.json", result.to_dict())
        evidence_line = f"- {self.evidence_hint}\n" if self.evidence_hint is not None else ""
        (self.path / "agent_summary.md").write_text(
            f"# {self.operation}: {'PASS' if result.success else 'FAIL'}\n\n"
            f"- run_id: {self.run_id}\n- error_code: {result.error_code or 'none'}\n"
            f"- {result.message}\n- 完整证据目录: {self.path}\n"
            f"{evidence_line}"
            f"- 逐步结果和清理状态: run_summary.json / events.jsonl\n", encoding="utf-8")
        return result
