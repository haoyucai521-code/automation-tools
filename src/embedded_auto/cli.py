"""薄 CLI：参数解析、配置加载、服务调用和结果输出。"""
import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .core.config import load_config, load_yaml, number
from .core.errors import ToolError
from .core.models import OperationResult
from .core.paths import identifier, resolve_path


def common(parser):
    # SUPPRESS 保证子命令不会覆盖在父命令指定的选项。
    for flag, options in (
        ("--config", {"help": "全局 YAML 配置路径，默认 configs/app.yaml"}),
        ("--target", {"help": "configs/targets 下的目标 ID"}),
        ("--json", {"action": "store_true", "help": "输出机器可读 JSON"}),
        ("--dry-run", {"action": "store_true", "help": "只生成计划，不执行构建或硬件动作"}),
        ("--verbose", {"action": "store_true"}),
        ("--timeout", {"type": float, "help": "整个操作的总超时（秒）"}),
        ("--port", {"help": "覆盖串口号；最终配置会归档"}),
        ("--baudrate", {"type": int, "help": "覆盖波特率"}),
    ):
        parser.add_argument(flag, default=argparse.SUPPRESS, **options)


def parser():
    root = argparse.ArgumentParser(prog="embedded-auto", description="本地嵌入式自动化：构建、复位、烧录和确定性测试")
    root.add_argument("--version", action="version", version=__version__)
    common(root)
    commands = root.add_subparsers(dest="command", required=True)
    device = commands.add_parser("device", help="无侵入检查配置、工具和串口")
    common(device)
    checks = device.add_subparsers(dest="device_command", required=True)
    common(checks.add_parser("check"))
    for name in ("build", "flash", "connect", "reset", "monitor", "test", "run", "report"):
        sub = commands.add_parser(name)
        common(sub)
        if name in ("build", "run"):
            sub.add_argument("--backend", choices=("iar", "gcc"))
        if name == "flash":
            sub.add_argument("--artifact", required=True, help="带构建身份清单的 BIN 快照")
            sub.add_argument("--manifest", help="默认读取 BIN 同目录的 artifact_manifest.json")
        if name in ("run", "test"):
            sub.add_argument("--suite", required=True, help="测试套件 ID 或 YAML 路径")
        if name == "monitor":
            sub.add_argument("--duration", type=float, default=30)
        if name == "report":
            sub.add_argument("--run-id", required=True)
    return root


def report(config_path, run_id):
    identifier(run_id)
    app_config = load_yaml(config_path)
    app = app_config.get("app", {})
    workspace = resolve_path(app.get("workspace", ".."), config_path.parent)
    runs = resolve_path(app.get("runs_dir", "runs"), workspace)
    path = runs / run_id / "run_summary.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError("RUN_NOT_FOUND", f"找不到有效运行报告: {path}") from exc


def main(argv=None):
    args = parser().parse_args(argv)
    operation = "device check" if args.command == "device" else args.command
    config_path = Path(getattr(args, "config", "configs/app.yaml")).resolve()
    try:
        if operation == "report":
            result = report(config_path, args.run_id)
        else:
            target = getattr(args, "target", None)
            if not target:
                raise ToolError("CONFIG_INVALID", "必须指定 --target")
            overrides = {}
            if getattr(args, "backend", None):
                overrides["build"] = {"backend": args.backend}
            for key in ("port", "baudrate"):
                if hasattr(args, key):
                    overrides.setdefault("serial", {})[key] = getattr(args, key)
            config = load_config(config_path, target, overrides)
            suite = None
            if operation in ("test", "run"):
                suite_path = Path(args.suite)
                if not suite_path.suffix and not suite_path.is_file():
                    identifier(args.suite)
                    suite_path = config_path.parent / "test_suites" / f"{args.suite}.yaml"
                suite = load_yaml(suite_path.resolve())
            if hasattr(args, "duration"):
                number(args.duration, "duration", 0.001)
            from .services.workflow_engine import WorkflowEngine
            result = WorkflowEngine().execute(
                config, operation, suite=suite,
                artifact=getattr(args, "artifact", None), manifest_path=getattr(args, "manifest", None),
                duration=getattr(args, "duration", 30), timeout_s=getattr(args, "timeout", None),
                dry_run=getattr(args, "dry_run", False)).to_dict()
    except ToolError as exc:
        result = OperationResult(False, operation, exc.code, str(exc), data=exc.data).to_dict()
    except KeyboardInterrupt:
        result = OperationResult(False, operation, "CANCELLED", "用户中断").to_dict()
    except (OSError, ValueError, TypeError) as exc:
        result = OperationResult(False, operation, "CONFIG_INVALID", str(exc)).to_dict()
    if getattr(args, "json", False):
        # ASCII JSON 可跨 Windows 控制台代码页传输，文件归档仍使用 UTF-8。
        print(json.dumps(result, ensure_ascii=True))
    else:
        print(f"{'PASS' if result['success'] else 'FAIL'} {operation}: {result['message']}")
        if result.get("error_code"):
            print(f"错误码: {result['error_code']}")
        if result.get("run_id"):
            print(f"run_id: {result['run_id']}")
        if getattr(args, "verbose", False) or getattr(args, "dry_run", False):
            print(json.dumps(result.get("data", {}), ensure_ascii=False, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
