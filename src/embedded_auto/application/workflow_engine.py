import copy
import shutil
import time
from pathlib import Path

from embedded_auto.core.config import number
from .config import validate_config, validation_issues
from .serial_actions import SERIAL_ACTIONS, check_serial_budget, perform_serial_action, serial_options
from embedded_auto.core.errors import ToolError
from embedded_auto.core.models import OperationResult
from embedded_auto.core.run_context import RunContext, write_json


MCU_OPERATIONS = {"mcu connect": "connect", "mcu reset": "reset", "mcu program": "flash"}


class WorkflowEngine:
    """单目标编排。依赖可注入，无硬件测试不需要替换业务逻辑。"""

    def __init__(self, builder=None, flasher=None, session_factory=None,
                 reset_factory=None, log_factory=None, runner_factory=None):
        from .composition import BuildService, FlashService, SerialSession
        from embedded_auto.serial.reset_controller import ResetController
        from embedded_auto.serial.log_service import LogService
        from embedded_auto.serial.test_runner import TestRunner
        self.builder = builder or BuildService()
        self.flasher = flasher or FlashService()
        self.session_factory = session_factory or SerialSession
        self.reset_factory = reset_factory or ResetController
        self.log_factory = log_factory or LogService
        self.runner_factory = runner_factory or TestRunner

    @staticmethod
    def plan(config, operation, suite=None):
        issues = validation_issues(config, operation)
        if operation in ("flash", "run"):
            issues.append("FLASH_DESTRUCTIVE_AUTH_REQUIRED: 本次镜像和擦除范围须单独授权")
        if operation in ("run", "test"):
            from embedded_auto.serial.test_runner import validate_suite
            try:
                validate_suite(suite)
            except ToolError as exc:
                issues.append(str(exc))
        commands = {}
        if operation in ("build", "run"):
            from .composition import build_backend
            backend = build_backend(config["build"].get("backend"))
            try:
                commands["build"] = backend.command(config) if backend else {"unresolved": "未知构建后端"}
            except ToolError as exc:
                commands["build"] = {"unresolved": str(exc)}
        if operation in ("connect", "flash", "run"):
            from .composition import programmer
            candidate = copy.deepcopy(config)
            try:
                from embedded_auto.core.config import integer
                if operation != "connect":
                    candidate["flash"]["address"] = integer(candidate["flash"].get("address"), "flash.address")
                commands["programmer"] = programmer().command(candidate, None if operation == "connect" else Path("<artifact_snapshot>.bin"))
                if operation != "connect":
                    commands["erase"] = programmer().erase_command(candidate)
            except ToolError as exc:
                commands["programmer"] = {"unresolved": str(exc)}
        steps = {
            "build": ["VALIDATE_CONFIG", "BUILD", "VALIDATE_ARTIFACT"],
            "run": ["VALIDATE_CONFIG", "BUILD", "VALIDATE_ARTIFACT", "OPEN_SERIAL", "ERASE_FLASH", "FLASH_ERASE_SUCCEEDED", "ASSERT_RESET", "START_PROGRAMMER", "RELEASE_RESET_AT_CONFIGURED_TIME", "WAIT_FLASH_AND_VERIFY", "PULSE_RESET", "WAIT_BOOT", "RUN_TESTS"],
            "flash": ["VALIDATE_CONFIG", "VALIDATE_ARTIFACT", "OPEN_SERIAL", "ERASE_FLASH", "FLASH_ERASE_SUCCEEDED", "RESET_ASSISTED_FLASH", "PULSE_RESET"],
            "connect": ["VALIDATE_CONFIG", "OPEN_SERIAL", "RESET_ASSISTED_CONNECT_ONLY"],
            "reset": ["VALIDATE_CONFIG", "OPEN_SERIAL", "PULSE_RESET"],
            "monitor": ["VALIDATE_CONFIG", "OPEN_SERIAL", "CAPTURE_SERIAL"],
            "serial read": ["VALIDATE_CONFIG", "OPEN_SERIAL", "SERIAL_READ"],
            "serial send": ["VALIDATE_CONFIG", "OPEN_SERIAL", "SERIAL_SEND"],
            "serial capture": ["VALIDATE_CONFIG", "OPEN_SERIAL", "SERIAL_CAPTURE"],
            "test": ["VALIDATE_CONFIG", "OPEN_SERIAL", "RUN_TESTS"],
            "device check": ["INSPECT_CONFIG", "LIST_TOOLS_AND_PORTS_WITHOUT_OPENING"],
        }
        return OperationResult(True, operation, message="仅生成计划；未执行构建、串口或烧录", data={
            "dry_run": True, "ready_for_execution": not issues, "blocking_issues": issues,
            "steps": steps.get(operation, []) + ["CLEANUP", "ARCHIVE"],
            "build": config.get("build"), "flash": config.get("flash"),
            "reset": config["reset"], "serial": config["serial"], "suite": suite,
            "commands": commands,
            "destructive_warning": "全片擦除将清除整个内部 Flash，包括参数、KV 和 Bootloader" if operation in ("flash", "run") else None,
            "validation_scope": "配置与套件预检；不证明工具、板卡、产物或身份清单可用",
        })

    @staticmethod
    def inspect(config):
        from .composition import serial_ports
        tools = {}
        b, f = config["build"], config["flash"]
        build_tool = b.get("executable") if b.get("backend") == "iar" else (b.get("command") or [None])[0]
        for label, executable in (("build", build_tool), ("programmer", f.get("executable"))):
            tools[label] = {"configured": executable, "resolved": shutil.which(executable) if executable else None}
        issues = {op: validation_issues(config, op) for op in ("build", "monitor", "connect", "flash", "run")}
        ports = serial_ports()
        missing_port = not any(p["device"].casefold() == str(config["serial"].get("port", "")).casefold() for p in ports)
        ready = not any(issues.values()) and all(item["resolved"] for item in tools.values()) and not missing_port
        return OperationResult(ready, "device check", None if ready else "CONFIG_INVALID",
                               "检查完成；未打开串口，未连接目标", data={"tools": tools, "ports": ports,
                               "validation_issues": issues, "configured_port_missing": missing_port,
                               "hardware_verified_by_this_check": False})

    def execute(self, config, operation, *, suite=None, artifact=None, manifest_path=None,
                duration=30, timeout_s=None, dry_run=False, authorization=None,
                send_text=None, send_hex=None, listen_after=0):
        requested_operation = operation
        operation = MCU_OPERATIONS.get(operation, operation)
        serial_action = operation in SERIAL_ACTIONS
        if operation not in ("device check", "build", "flash", "connect", "reset", "monitor", "test", "run",
                             "mcu preflight", *SERIAL_ACTIONS):
            raise ToolError("CONFIG_INVALID", f"不支持的操作: {operation}")
        ctx = RunContext(config, requested_operation, timeout_s,
                         evidence_hint=None if operation == "mcu preflight" else
                         "原始串口数据（若打开串口）: serial.bin / serial.jsonl")
        session = reset = logger = None
        opened = False
        steps, cleanup = {}, []
        phase = "plan" if dry_run else "preflight"
        result = OperationResult(False, operation, "INTERNAL_ERROR", "操作未完成")
        try:
            if operation == "mcu preflight":
                from .mcu_preflight import preflight
                ctx.event("MCU_PREFLIGHT")
                result = preflight(config, ctx, artifact, manifest_path)
            elif dry_run:
                result = self.plan(config, operation, suite)
                if serial_action:
                    try:
                        window, payload = serial_options(operation, duration, send_text, send_hex, listen_after)
                        result.data.update(window_s=window, tx_bytes_planned=len(payload) if payload else 0)
                        if result.data["ready_for_execution"]:
                            check_serial_budget(config, ctx, window, payload)
                    except ToolError as exc:
                        result.data["blocking_issues"].append(f"{exc.code}: {exc}")
                        result.data["ready_for_execution"] = False
            elif operation == "device check":
                result = self.inspect(config)
            else:
                ctx.event("VALIDATE_CONFIG")
                validate_config(config, operation)
                if operation == "monitor":
                    number(duration, "duration", 0.001)
                if serial_action:
                    window, payload = serial_options(operation, duration, send_text, send_hex, listen_after)
                if operation in ("run", "test"):
                    from embedded_auto.serial.test_runner import validate_suite
                    validate_suite(suite)
                    write_json(ctx.path / "test_suite.json", suite)
                ctx.check()
                if not serial_action:
                    ctx.snapshot_git()
                if operation in ("build", "run"):
                    phase = "build"
                    ctx.event("BUILD")
                    build = self.builder.build(config, ctx)
                    steps["build"] = build.to_dict()
                    if not build.success:
                        raise ToolError(build.error_code or "BUILD_FAILED", build.message, build.data)
                    artifact = ctx.path / "firmware.bin"
                    manifest_path = ctx.path / "artifact_manifest.json"
                image, manifest = None, None
                if operation in ("flash", "run"):
                    phase = "preflight"
                    from .mcu_preflight import load_flash_artifact
                    ctx.event("VALIDATE_ARTIFACT")
                    image, manifest, steps["artifact"] = load_flash_artifact(config, artifact, manifest_path)
                    from embedded_auto.firmware.erase_policy import validate_erase_policy
                    steps["erase_authorization"] = validate_erase_policy(config, steps["artifact"], authorization)
                    write_json(ctx.path / "input_artifact_manifest.json", manifest)
                if operation == "build":
                    result = OperationResult(True, operation, message="构建成功，产物已独立归档", data=steps)
                else:
                    if operation in ("connect", "flash", "run"):
                        from .mcu_preflight import check_programmer_ready
                        check_programmer_ready(config, ctx.remaining)
                    if serial_action:
                        check_serial_budget(config, ctx, window, payload)
                    ctx.event("OPEN_SERIAL")
                    phase = "open_serial"
                    session = self.session_factory(config)
                    logger = self.log_factory(session, ctx, encoding=config["serial"].get("encoding", "utf-8"))
                    logger.start()
                    if serial_action:
                        # 日志初始化可能耗尽总预算，不在预算不足时再触碰 COM。
                        check_serial_budget(config, ctx, window, payload)
                    session.open()
                    opened = True
                    # 复位控制器构造不触碰硬件，尽早创建，使 check 失败也走 release_reset 清理。
                    reset = self.reset_factory(session, config, ctx)
                    session.check()
                    if operation in ("run", "flash", "connect"):
                        phase = "flash" if operation != "connect" else "connect"
                        kwargs = {"connect_only": operation == "connect"}
                        if operation != "connect":
                            kwargs["authorization"] = authorization
                        flashed = self.flasher.flash(config, ctx, session, reset, image, manifest, **kwargs)
                        steps["flash"] = flashed.to_dict()
                        if not flashed.success:
                            raise ToolError(flashed.error_code or "FLASH_FAILED", flashed.message, flashed.data)
                    if operation in ("run", "flash", "reset"):
                        phase = "reset"
                        # NORMAL 连接可能停核；校验后复位启动新固件，并从脉冲前捕获启动证据。
                        boot_start = session.mark()
                        ctx.event("PULSE_RESET")
                        reset.pulse()
                    if operation == "run":
                        phase = "boot"
                        ctx.event("WAIT_BOOT")
                        startup = config["startup"]
                        if startup.get("ready_pattern"):
                            # 病态启动正则复用协议测试的独立工作进程：受启动超时与总截止时间约束，
                            # 超时后回收工作进程并继续硬件清理；保持按 serial.encoding 解码的文本正则语义。
                            from embedded_auto.serial.test_runner import _RegexMatcher
                            encoding = config["serial"].get("encoding", "utf-8")
                            timeout_s = startup.get("timeout_s", 5)
                            # 匹配器自身故障不是固件协议失败，也不可进入修复循环。
                            matcher = _RegexMatcher(ctx, error_code="BOOT_MATCHER_FAILED")
                            evaluated, matched = None, False
                            try:
                                matcher.start()
                                deadline = time.monotonic() + min(timeout_s, ctx.remaining)

                                def startup_ready(data):
                                    # 数据增长才重新匹配，避免忙碌串流下重复回溯。
                                    nonlocal evaluated, matched
                                    if data and data != evaluated:
                                        evaluated = data
                                        matched = matcher.search(startup["ready_pattern"],
                                                                 data.decode(encoding, "replace"), deadline) is not None
                                    return matched

                                received = session.wait_for(startup_ready, timeout_s, ctx, start=boot_start)
                                if received != evaluated:
                                    startup_ready(received)  # wait_for 最终返回的数据可能晚于上一次评估
                            finally:
                                try:
                                    matcher.close()
                                except Exception as exc:
                                    cleanup.append({"action": "close_regex_worker", "success": False, "message": str(exc)})
                            if not matched:
                                raise ToolError("BOOT_TIMEOUT", "未在超时内检测到启动标志", {"received_hex": received.hex(), "start_offset": boot_start})
                            steps["boot"] = {"method": "ready_pattern", "start_offset": boot_start, "received_hex": received.hex()}
                        else:
                            ctx.sleep(startup.get("settle_delay_s", 1))
                            steps["boot"] = {"method": "delay", "warning": "未通过启动标志确认，仅等待配置延时"}
                    if operation in ("run", "test"):
                        phase = "test"
                        ctx.event("RUN_TESTS")
                        tested = self.runner_factory(session, ctx).run(suite)
                        steps["tests"] = tested.to_dict()
                        if not tested.success:
                            raise ToolError(tested.error_code or "PROTOCOL_TEST_FAILED", tested.message, tested.data)
                    if operation == "monitor":
                        ctx.event("CAPTURE_SERIAL", duration_s=duration)
                        ctx.sleep(duration)
                    if serial_action:
                        phase = operation.replace(" ", "_")
                        perform_serial_action(config, ctx, session, operation, window, payload, steps)
                        ctx.check()
                    session.check()
                    result = OperationResult(True, operation, message="操作完成，证据已归档", data=steps)
                    phase = "complete"
                ctx.event("PASS")
        except ToolError as exc:
            result = OperationResult(False, operation, exc.code, str(exc), data={**steps, "failure": exc.data})
        except KeyboardInterrupt:
            ctx.cancelled.set()
            result = OperationResult(False, operation, "CANCELLED", "用户中断", data=steps)
        except Exception as exc:
            result = OperationResult(False, operation, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", data=steps)
        finally:
            if not result.success and not dry_run:
                try:
                    ctx.event("FAILED", error_code=result.error_code, message=result.message)
                except Exception as exc:
                    cleanup.append({"action": "failure_event", "success": False, "message": str(exc)})
            try:
                ctx.event("CLEANUP")
            except Exception as exc:
                # 事件归档故障只记录为清理失败，绝不跳过剩余硬件清理动作。
                cleanup.append({"action": "cleanup_event", "success": False, "message": str(exc)})
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
            facts = getattr(session, "cleanup_facts", None)
            result.data["reset_cleanup"] = dict(facts) if isinstance(facts, dict) else {
                "release_attempted": False if session is None else None,
                "release_api_succeeded": None, "port_close_succeeded": None,
                "physical_reset_state": "UNKNOWN"}
            flash_data = steps.get("flash", {}).get("data", {})
            result.data["diagnostics"] = {
                "phase": flash_data.get("phase", phase) if phase == "flash" else phase,
                "destructive_effect": flash_data.get("destructive_effect", "UNKNOWN" if phase == "flash" else "NOT_STARTED"),
                "confirmed_actions": flash_data.get("confirmed_actions", []),
                "manual_review": flash_data.get("manual_review", "按错误、证据和清理状态人工复核；未测量的物理状态未知"),
                "evidence_path": str(ctx.path), "cleanup": cleanup,
                "tool_version_status": "NOT_VERIFIED_THIS_RUN" if operation in ("flash", "run", "connect") else "NOT_APPLICABLE",
                "plan_only": dry_run}
            if any(not action["success"] for action in cleanup) and result.success:
                result.success, result.error_code, result.message = False, "CLEANUP_FAILED", "操作成功但清理失败；硬件状态可能不确定"
            if not serial_action and logger is not None and (ctx.path / "serial.log").exists():
                from embedded_auto.serial.log_analyzer import LogAnalyzer
                try:
                    analysis = LogAnalyzer.analyze(ctx.path / "serial.log")
                    write_json(ctx.path / "log_analysis.json", analysis)
                    result.data["log_analysis"] = analysis
                except Exception as exc:
                    result.data["log_analysis_error"] = str(exc)
        result.operation = requested_operation
        try:
            return ctx.finish(result)
        except (OSError, ValueError, TypeError) as exc:
            result.run_id = ctx.run_id
            result.data["archive_error"] = str(exc)
            result.data.setdefault("run_path", str(ctx.path))
            if result.success:
                result.success, result.error_code, result.message = False, "ARCHIVE_FAILED", "运行结果归档失败"
            return result
