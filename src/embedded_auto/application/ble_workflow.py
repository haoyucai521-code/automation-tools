"""BLE 的应用编排与归档；硬件后端仅由装配入口显式注入。"""
import asyncio
import sys
from pathlib import Path

from embedded_auto.ble.config import (
    authorize, has_writes, hex_bytes, positive, prepare_config, require,
    uuid128, validate_expect, validate_profile, validate_suite,
)
from embedded_auto.ble.evidence import BleEvidence
from embedded_auto.ble.models import record
from embedded_auto.ble.session import BleSession
from embedded_auto.ble.test_runner import TestRunner, assertions
from embedded_auto.core.config import load_yaml
from embedded_auto.core.errors import ToolError
from embedded_auto.core.models import OperationResult
from embedded_auto.core.run_context import RunContext, write_json

from .composition import ble_backend, check_ble_dependency


def result_data(backend):
    return {"backend": backend, "simulated": backend == "fake",
            "validation_level": "L1_FAKE" if backend == "fake" else "L3_UNACCEPTED",
            "verdict": "NOT_EVALUATED", "cases": [], "cleanup": [], "evidence_complete": False}


class BleWorkflow:
    def __init__(self, backend_factory=None, notice=None):
        self.backend_factory = backend_factory or ble_backend
        self.notice = notice or (lambda message: None)

    def execute(self, config, command, *, options=None, suite=None, suite_path=None,
                profile=None, timeout_s=None, dry_run=False):
        try:
            with asyncio.Runner() as runner:
                return runner.run(self._execute(config, command, options or {}, suite, suite_path,
                                                profile, timeout_s, dry_run))
        except OSError as exc:
            return OperationResult(False, f"ble {command}", "LOG_WRITE_FAILED", str(exc),
                                   data=result_data(config["ble"]["backend"]))

    async def _execute(self, config, command, options, suite, suite_path, profile, timeout_s, dry_run):
        operation = f"ble {command}"
        ctx = RunContext(config, operation, timeout_s, run_label=f"ble_{command}",
                         evidence_hint="BLE 原始事件: ble_events.jsonl；不是串口数据或实机验收")
        data = result_data(config["ble"]["backend"])
        data["evidence_paths"] = {name: str(ctx.path / name) for name in
                                 ("run_config.yaml", "events.jsonl", "run_summary.json",
                                  "agent_summary.md", "ble_events.jsonl", "ble_assertions.json")}
        error, evidence, session, test_runner = None, None, None, None
        try:
            require(command in ("scan", "inspect", "read", "write", "notify", "test"), "未知 BLE 命令")
            ctx.event("BLE_PREFLIGHT")
            if command == "test":
                suite = validate_suite(load_yaml(Path(suite_path).resolve()) if suite_path else suite,
                                       config["ble"]["operation_timeout_s"])
                write_json(ctx.path / "ble_suite.json", suite)
            if config["ble"]["backend"] == "fake":
                profile = validate_profile(profile if profile is not None else
                                           load_yaml(Path(config["ble"]["fake_profile"])))
                write_json(ctx.path / "ble_profile.json", profile)
            selector, expect, payload, response = self.preflight(command, options)
            issues = []
            try:
                if command != "scan":
                    require(bool(config["ble"].get("device")), "连接命令必须明确指定 --device 或 ble.device")
                authorize(config, command == "write" or (command == "test" and has_writes(suite)),
                          options.get("allow_write", False), options.get("confirm_device"))
                if config["ble"]["backend"] == "bleak" and self.backend_factory is ble_backend:
                    check_ble_dependency()
            except ToolError as exc:
                if not dry_run:
                    raise
                issues.append({"error_code": exc.code, "message": str(exc), "data": exc.data})
            if dry_run:
                data.update(dry_run=True, blocking_issues=issues, plan={
                    "command": command, "device": config["ble"].get("device"),
                    "selector": selector, "write_bytes": len(payload) if payload is not None else 0,
                    "write_mode": options.get("write_mode"), "suite": suite,
                    "hardware_actions_executed": False})
            else:
                ctx.check()
                evidence = BleEvidence(ctx, config["ble"]["backend"])
                data["evidence_complete"] = True
                backend = self.backend_factory(config, profile)
                session = BleSession(backend, config, ctx, evidence, self.notice)
                candidates = await session.scan()
                data["candidates"] = [record(c) for c in candidates]
                if command != "scan":
                    await session.connect(candidates)
                    data["device"] = record(session.device)
                    if command == "test":
                        test_runner = TestRunner(session, suite)
                        await test_runner.run()
                        data.update(test_runner.data())
                        if test_runner.error:
                            raise test_runner.error
                        if data["verdict"] != "PASS":
                            raise ToolError("BLE_SUITE_NOT_EXECUTED", "未完成所有必需用例和断言")
                    else:
                        await self.perform(command, session, selector, expect, payload, response, options, data)
                session.check()
                data["evidence_complete"] = True
        except ToolError as exc:
            if dry_run:
                data.update(dry_run=True, blocking_issues=[
                    {"error_code": exc.code, "message": str(exc), "data": exc.data}])
            else:
                error = exc
        except (KeyboardInterrupt, asyncio.CancelledError):
            ctx.cancelled.set()
            error = ToolError("CANCELLED", "用户取消 BLE 操作")
        except Exception as exc:
            error = ToolError("INTERNAL_ERROR", str(exc), {"exception_type": type(exc).__name__})
        finally:
            if test_runner:
                data.update(test_runner.data())
            if error and evidence:
                try:
                    fields = {"stage": "workflow", **error.data,
                              "error_code": error.code, "message": str(error)}
                    evidence.emit("error", **fields)
                except ToolError as log_error:
                    data["cleanup"].append({"action": "error_evidence", "success": False,
                                            "error_code": log_error.code, "message": str(log_error)})
                    data["evidence_complete"] = False
            if session:
                data["cleanup"].extend(await session.close())
                data["notification_counters"] = session.buffer.stats()
                if session.buffer.error:
                    error = error or session.buffer.error
                if session.buffer.rejected_total:
                    data["evidence_complete"] = False
                    if evidence and not evidence.error:
                        try:
                            evidence.emit("error", error_code="BLE_EVENT_OVERFLOW", stage="notification_ingress",
                                          message="通知被拒收，原始证据不完整",
                                          rejected_total=session.buffer.rejected_total)
                        except ToolError as exc:
                            data["cleanup"].append({"action": "overflow_evidence", "success": False,
                                                    "error_code": exc.code, "message": str(exc)})
                if session.buffer.discarded_total:
                    data["evidence_complete"] = False
                    error = error or ToolError("LOG_WRITE_FAILED", "清理结束仍有已接收但未归档的通知",
                                               {"discarded_total": session.buffer.discarded_total})
            if evidence:
                try:
                    evidence.close()
                except ToolError as exc:
                    data["cleanup"].append({"action": "close_evidence", "success": False,
                                            "error_code": exc.code, "message": str(exc)})
            if data["cleanup"] and any(not c["success"] for c in data["cleanup"]):
                error = error or ToolError("CLEANUP_FAILED", "BLE 清理未完整成功")
            if (error and error.code == "LOG_WRITE_FAILED") or any(
                    c.get("error_code") == "LOG_WRITE_FAILED" for c in data["cleanup"]):
                data["evidence_complete"] = False
            if error:
                data["error_data"] = error.data
            message = str(error) if error else ("仅生成计划，未执行断言" if dry_run else "BLE 操作完成")
            mode = "模拟 Fake" if data["simulated"] else "真实后端，未实机验收"
            message = f"{message}；断言 {data['verdict']}；{mode}"
            result = OperationResult(error is None, operation, error.code if error else None, message,
                                     log_path=ctx.path / "ble_events.jsonl" if evidence else None, data=data)
            try:
                write_json(ctx.path / "ble_assertions.json", {
                    key: data[key] for key in ("verdict", "cases", "preflight", "assertions") if key in data})
                return ctx.finish(result)
            except OSError as exc:
                data["evidence_complete"] = False
                data["cleanup"].append({"action": "archive", "success": False,
                                        "error_code": "LOG_WRITE_FAILED", "message": str(exc)})
                result.success = False
                result.error_code = result.error_code or "LOG_WRITE_FAILED"
                result.message = result.message if error else str(exc)
                result.run_id = ctx.run_id
                data["run_path"] = str(ctx.path)
                return result

    @staticmethod
    def preflight(command, options):
        selector = None
        if command in ("read", "write", "notify"):
            selector = {"service_uuid": uuid128(options.get("service")),
                        "uuid": uuid128(options.get("characteristic"))}
        expect = validate_expect({"equals_hex": options["expect_hex"]}) if options.get("expect_hex") is not None else None
        payload, response = None, None
        if command == "write":
            payload = hex_bytes(options.get("hex"), empty=False)
            require(options.get("write_mode") in ("with-response", "without-response"), "必须显式指定 --write-mode")
            response = options["write_mode"] == "with-response"
        if command == "notify":
            positive(options.get("duration", 2), "duration")
        return selector, expect, payload, response

    @staticmethod
    async def perform(command, session, selector, expect, payload, response, options, data):
        if command == "inspect":
            data["services"] = [record(s) for s in session.services]
            return
        char = session.resolve(selector)
        if command == "read":
            actual, event_id = await session.read(char)
            data.update(actual_hex=actual.hex(), event_ids=[event_id])
            if expect:
                data["assertions"] = assertions(actual, expect)
        elif command == "write":
            data["write"] = await session.write(char, payload, response)
        elif command == "notify":
            entry = await session.subscribe(char)
            mark = entry["start_checkpoint"]
            deadline_ns = entry["start_monotonic_ns"] + int(options.get("duration", 2) * 1_000_000_000)
            event = await session.wait(mark, options.get("duration", 2),
                                       (lambda value: all(a["success"] for a in assertions(value, expect))) if expect else None,
                                       deadline_ns=deadline_ns)
            data.update(checkpoint=record(mark), event_ids=[e["event_id"] for e in session.last_wait_events])
            if expect:
                data["assertions"] = assertions(bytes.fromhex(event["payload_hex"]), expect) if event else [
                    {"rule": "matching_notification", "expected": expect, "success": False,
                     "observed_hex": [e["payload_hex"] for e in session.last_wait_events]}]
        if data.get("assertions"):
            data["verdict"] = "PASS" if all(a["success"] for a in data["assertions"]) else "FAIL"
            if data["verdict"] == "FAIL":
                raise ToolError("BLE_ASSERTION_FAILED", "BLE 字节断言不满足")


def execute_ble(args, config_path):
    """CLI 路由必须在固件 target 校验之前进入此处。"""
    backend = getattr(args, "backend", None)
    dry_run = getattr(args, "dry_run", False)
    try:
        overrides = {key: getattr(args, key) for key in ("backend", "device", "name") if hasattr(args, key)}
        if hasattr(args, "scan_timeout"):
            overrides["scan_timeout_s"] = args.scan_timeout
        config = prepare_config(load_yaml(config_path), config_path, overrides)
        backend = config["ble"]["backend"]
        options = {key: getattr(args, key) for key in
                   ("service", "characteristic", "hex", "write_mode", "expect_hex", "duration",
                    "allow_write", "confirm_device") if hasattr(args, key)}
        return BleWorkflow(notice=lambda text: print(text, file=sys.stderr)).execute(
            config, args.ble_command, options=options, suite_path=getattr(args, "suite", None),
            timeout_s=getattr(args, "timeout", None), dry_run=dry_run)
    except ToolError as exc:
        data = result_data(backend)
        data["error_data"] = exc.data
        if dry_run:
            data.update(dry_run=True, blocking_issues=[{"error_code": exc.code, "message": str(exc)}])
        return OperationResult(dry_run, f"ble {args.ble_command}", None if dry_run else exc.code,
                               "仅生成阻断计划，未执行" if dry_run else str(exc), data=data)
