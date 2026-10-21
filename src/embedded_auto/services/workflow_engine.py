import copy
import json
import re
import shutil
from pathlib import Path

from embedded_auto.core.config import number, validate_config, validation_issues
from embedded_auto.core.errors import ToolError
from embedded_auto.core.models import OperationResult
from embedded_auto.core.run_context import RunContext, write_json


class WorkflowEngine:
    """单目标编排。依赖可注入，无硬件测试不需要替换业务逻辑。"""

    def __init__(self, builder=None, flasher=None, session_factory=None,
                 reset_factory=None, log_factory=None, runner_factory=None):
        from .build_service import BuildService
        from .flash_service import FlashService
        from .serial_session import SerialSession
        from .reset_controller import ResetController
        from .log_service import LogService
        from .test_runner import TestRunner
        self.builder = builder or BuildService()
        self.flasher = flasher or FlashService()
        self.session_factory = session_factory or SerialSession
        self.reset_factory = reset_factory or ResetController
        self.log_factory = log_factory or LogService
        self.runner_factory = runner_factory or TestRunner

    @staticmethod
    def plan(config, operation, suite=None):
        issues = validation_issues(config, operation)
        if operation in ("run", "test"):
            from .test_runner import validate_suite
            try:
                validate_suite(suite)
            except ToolError as exc:
                issues.append(str(exc))
        commands = {}
        if operation in ("build", "run"):
            from embedded_auto.adapters.build import GCCBackend, IARBackend
            backend = {"gcc": GCCBackend, "iar": IARBackend}.get(config["build"].get("backend"))
            try:
                commands["build"] = backend().command(config) if backend else {"unresolved": "未知构建后端"}
            except ToolError as exc:
                commands["build"] = {"unresolved": str(exc)}
        if operation in ("connect", "flash", "run"):
            from embedded_auto.adapters.programmer import CubeProgrammer
            candidate = copy.deepcopy(config)
            try:
                from embedded_auto.core.config import integer
                if operation != "connect":
                    candidate["flash"]["address"] = integer(candidate["flash"].get("address"), "flash.address")
                commands["programmer"] = CubeProgrammer().command(candidate, None if operation == "connect" else Path("<artifact_snapshot>.bin"))
            except ToolError as exc:
                commands["programmer"] = {"unresolved": str(exc)}
        steps = {
            "build": ["VALIDATE_CONFIG", "BUILD", "VALIDATE_ARTIFACT"],
            "run": ["VALIDATE_CONFIG", "BUILD", "VALIDATE_ARTIFACT", "OPEN_SERIAL", "ASSERT_RESET", "START_PROGRAMMER", "RELEASE_RESET_AT_CONFIGURED_TIME", "WAIT_FLASH_AND_VERIFY", "PULSE_RESET", "WAIT_BOOT", "RUN_TESTS"],
            "flash": ["VALIDATE_CONFIG", "VALIDATE_ARTIFACT", "OPEN_SERIAL", "RESET_ASSISTED_FLASH", "PULSE_RESET"],
            "connect": ["VALIDATE_CONFIG", "OPEN_SERIAL", "RESET_ASSISTED_CONNECT_ONLY"],
            "reset": ["VALIDATE_CONFIG", "OPEN_SERIAL", "PULSE_RESET"],
            "monitor": ["VALIDATE_CONFIG", "OPEN_SERIAL", "CAPTURE_SERIAL"],
            "test": ["VALIDATE_CONFIG", "OPEN_SERIAL", "RUN_TESTS"],
            "device check": ["INSPECT_CONFIG", "LIST_TOOLS_AND_PORTS_WITHOUT_OPENING"],
        }
        return OperationResult(True, operation, message="仅生成计划；未执行构建、串口或烧录", data={
            "dry_run": True, "ready_for_execution": not issues, "blocking_issues": issues,
            "steps": steps.get(operation, []) + ["CLEANUP", "ARCHIVE"],
            "build": config["build"], "flash": config["flash"],
            "reset": config["reset"], "serial": config["serial"], "suite": suite,
            "commands": commands,
            "validation_scope": "配置与套件预检；不证明工具、板卡、产物或身份清单可用",
        })

    @staticmethod
    def inspect(config):
        from serial.tools import list_ports
        tools = {}
        b, f = config["build"], config["flash"]
        build_tool = b.get("executable") if b.get("backend") == "iar" else (b.get("command") or [None])[0]
        for label, executable in (("build", build_tool), ("programmer", f.get("executable"))):
            tools[label] = {"configured": executable, "resolved": shutil.which(executable) if executable else None}
        issues = {op: validation_issues(config, op) for op in ("build", "monitor", "connect", "run")}
        ports = [{"device": p.device, "description": p.description, "hwid": p.hwid} for p in list_ports.comports()]
        missing_port = not any(p["device"].casefold() == str(config["serial"].get("port", "")).casefold() for p in ports)
        ready = not any(issues.values()) and all(item["resolved"] for item in tools.values()) and not missing_port
        return OperationResult(ready, "device check", None if ready else "CONFIG_INVALID",
                               "检查完成；未打开串口，未连接目标", data={"tools": tools, "ports": ports,
                               "validation_issues": issues, "configured_port_missing": missing_port,
                               "hardware_verified_by_this_check": False})

    def execute(self, config, operation, *, suite=None, artifact=None, manifest_path=None,
                duration=30, timeout_s=None, dry_run=False):
        if operation not in ("device check", "build", "flash", "connect", "reset", "monitor", "test", "run"):
            raise ToolError("CONFIG_INVALID", f"不支持的操作: {operation}")
        ctx = RunContext(config, operation, timeout_s)
        session = reset = logger = None
        opened = False
        steps, cleanup = {}, []
        result = OperationResult(False, operation, "INTERNAL_ERROR", "操作未完成")
        try:
            if dry_run:
                result = self.plan(config, operation, suite)
            elif operation == "device check":
                result = self.inspect(config)
            else:
                ctx.event("VALIDATE_CONFIG")
                validate_config(config, operation)
                if operation == "monitor":
                    number(duration, "duration", 0.001)
                if operation in ("run", "test"):
                    from .test_runner import validate_suite
                    validate_suite(suite)
                    write_json(ctx.path / "test_suite.json", suite)
                ctx.check()
                ctx.snapshot_git()
                if operation in ("build", "run"):
                    ctx.event("BUILD")
                    build = self.builder.build(config, ctx)
                    steps["build"] = build.to_dict()
                    if not build.success:
                        raise ToolError(build.error_code or "BUILD_FAILED", build.message, build.data)
                    artifact = ctx.path / "firmware.bin"
                    manifest_path = ctx.path / "artifact_manifest.json"
                image, manifest = None, None
                if operation in ("flash", "run"):
                    from .build_service import validate_artifact
                    ctx.event("VALIDATE_ARTIFACT")
                    if artifact is None:
                        raise ToolError("ARTIFACT_MISSING", "必须明确指定产物")
                    image = Path(artifact).resolve()
                    manifest_file = Path(manifest_path).resolve() if manifest_path else image.parent / "artifact_manifest.json"
                    try:
                        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                    except (OSError, ValueError) as exc:
                        raise ToolError("ARTIFACT_INVALID", f"缺少或无效的产物身份清单: {exc}") from exc
                    steps["artifact"] = validate_artifact(image, manifest, config)
                    write_json(ctx.path / "input_artifact_manifest.json", manifest)
                if operation == "build":
                    result = OperationResult(True, operation, message="构建成功，产物已独立归档", data=steps)
                else:
                    ctx.event("OPEN_SERIAL")
                    session = self.session_factory(config)
                    logger = self.log_factory(session, ctx, encoding=config["serial"].get("encoding", "utf-8"))
                    logger.start()
                    session.open()
                    opened = True
                    session.check()
                    reset = self.reset_factory(session, config, ctx)
                    if operation in ("run", "flash", "connect"):
                        flashed = self.flasher.flash(config, ctx, session, reset, image, manifest,
                                                     connect_only=operation == "connect")
                        steps["flash"] = flashed.to_dict()
                        if not flashed.success:
                            raise ToolError(flashed.error_code or "FLASH_FAILED", flashed.message, flashed.data)
                    if operation in ("run", "flash", "reset"):
                        # NORMAL 连接可能停核；校验后复位启动新固件，并从脉冲前捕获启动证据。
                        boot_start = session.mark()
                        ctx.event("PULSE_RESET")
                        reset.pulse()
                    if operation == "run":
                        ctx.event("WAIT_BOOT")
                        startup = config["startup"]
                        if startup.get("ready_pattern"):
                            regex = re.compile(startup["ready_pattern"])
                            encoding = config["serial"].get("encoding", "utf-8")
                            predicate = lambda data: bool(regex.search(data.decode(encoding, "replace")))
                            received = session.wait_for(predicate, startup.get("timeout_s", 5), ctx, start=boot_start)
                            if not predicate(received):
                                raise ToolError("BOOT_TIMEOUT", "未在超时内检测到启动标志", {"received_hex": received.hex(), "start_offset": boot_start})
                            steps["boot"] = {"method": "ready_pattern", "start_offset": boot_start, "received_hex": received.hex()}
                        else:
                            ctx.sleep(startup.get("settle_delay_s", 1))
                            steps["boot"] = {"method": "delay", "warning": "未通过启动标志确认，仅等待配置延时"}
                    if operation in ("run", "test"):
                        ctx.event("RUN_TESTS")
                        tested = self.runner_factory(session, ctx).run(suite)
                        steps["tests"] = tested.to_dict()
                        if not tested.success:
                            raise ToolError(tested.error_code or "PROTOCOL_TEST_FAILED", tested.message, tested.data)
                    if operation == "monitor":
                        ctx.event("CAPTURE_SERIAL", duration_s=duration)
                        ctx.sleep(duration)
                    session.check()
                    result = OperationResult(True, operation, message="操作完成，证据已归档", data=steps)
                ctx.event("PASS")
        except ToolError as exc:
            result = OperationResult(False, operation, exc.code, str(exc), data={**steps, "failure": exc.data})
            ctx.event("FAILED", error_code=exc.code, message=str(exc))
        except KeyboardInterrupt:
            ctx.cancelled.set()
            result = OperationResult(False, operation, "CANCELLED", "用户中断", data=steps)
            ctx.event("FAILED", error_code="CANCELLED")
        except Exception as exc:
            result = OperationResult(False, operation, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", data=steps)
            ctx.event("FAILED", error_code="INTERNAL_ERROR", message=str(exc))
        finally:
            ctx.event("CLEANUP")
            if opened and reset is not None:
                try:
                    reset.release_reset()
                    cleanup.append({"action": "release_reset", "success": True, "state": reset.state})
                except Exception as exc:
                    cleanup.append({"action": "release_reset", "success": False, "message": str(exc), "hardware_state": "unknown"})
            if session is not None:
                try:
                    session.close()
                    cleanup.append({"action": "close_serial", "success": True})
                except Exception as exc:
                    cleanup.append({"action": "close_serial", "success": False, "message": str(exc)})
            if logger is not None:
                try:
                    logger.close()
                    cleanup.append({"action": "close_logs", "success": True})
                except Exception as exc:
                    cleanup.append({"action": "close_logs", "success": False, "message": str(exc)})
            result.data["cleanup"] = cleanup
            if any(not action["success"] for action in cleanup) and result.success:
                result.success, result.error_code, result.message = False, "CLEANUP_FAILED", "操作成功但清理失败；硬件状态可能不确定"
            if logger is not None and (ctx.path / "serial.log").exists():
                from .log_analyzer import LogAnalyzer
                try:
                    analysis = LogAnalyzer.analyze(ctx.path / "serial.log")
                    write_json(ctx.path / "log_analysis.json", analysis)
                    result.data["log_analysis"] = analysis
                except Exception as exc:
                    result.data["log_analysis_error"] = str(exc)
        return ctx.finish(result)
