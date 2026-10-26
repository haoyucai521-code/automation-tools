import contextlib
import copy
import io
import json
import multiprocessing
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from embedded_auto.agent.repair_policy import RepairLoop, RepairPolicy
from embedded_auto.cli import main, parser
from embedded_auto.core.config import load_config, validate_config
from embedded_auto.core.errors import ToolError
from embedded_auto.core.models import OperationResult
from embedded_auto.core.run_context import RunContext
from embedded_auto.services.flash_service import FlashService
from embedded_auto.services.serial_session import SerialSession
from embedded_auto.services.workflow_engine import WorkflowEngine


def config_for(root):
    return {
        "app": {"workspace": str(root), "runs_dir": str(root / "runs"), "default_timeout_s": 10},
        "target": {"id": "fake", "exact_part_number": "FAKE001", "status": "verified"},
        "build": {"backend": "gcc", "cwd": str(root), "artifact": str(root / "source.bin"),
                  "command": [sys.executable, "-c", "from pathlib import Path; Path('source.bin').write_bytes(b'firmware')"], "timeout_s": 3},
        "flash": {"executable": sys.executable, "interface": "SWD", "mode": "NORMAL", "reset_mode": "SWrst",
                  "address": 0x08000000, "region_start": 0x08000000, "region_size": 1024, "address_source": "fake-layout-v1",
                  "verify": True, "timeout_s": 3, "cli_verified": True, "compatibility_verified": True,
                  "connect_success_pattern": "CONNECTED", "erase_success_pattern": "ERASED",
                  "write_success_pattern": "WRITTEN", "verify_success_pattern": "VERIFIED"},
        "reset": {"asserted_rts_value": True, "pulse_ms": 1, "release_delay_ms": 1,
                  "polarity_verified": True, "voltage_verified": True, "wiring_verified": True,
                  "open_transient_verified": True, "timing_verified": True},
        "serial": {"port": "FAKE", "baudrate": 115200, "read_timeout_s": .01, "encoding": "utf-8"},
        "startup": {"ready_pattern": "BOOT READY", "timeout_s": .1, "settle_delay_s": 0},
    }


SUITE = {"version": 1, "tests": [{"name": "ping", "send_text": "PING\n", "expect": {"contains_text": "PONG"}, "timeout_s": .1}]}


class MemorySerial:
    def __init__(self, config):
        self.data = bytearray()
        self.callbacks = []
        self.opened = False
        self.closed = False
        self.rts = False

    def open(self):
        assert self.callbacks, "归档订阅必须先于端口打开"
        self.opened = True

    def close(self):
        self.closed = True

    def check(self):
        pass

    def emit(self, direction, data):
        if direction == "rx":
            self.data.extend(data)
        for cb in list(self.callbacks):
            cb(direction, data, time.time())

    def set_rts(self, value):
        old, self.rts = self.rts, value
        if old and not value:
            self.emit("rx", b"BOOT READY\n")

    def subscribe(self, callback):
        self.callbacks.append(callback)
        return lambda: self.callbacks.remove(callback)

    def write(self, data):
        self.emit("tx", data)
        self.emit("rx", b"PONG\r\n")
        return len(data)

    def mark(self):
        return len(self.data)

    def received_since(self, mark):
        return bytes(self.data[mark:])

    def wait_for(self, predicate, timeout_s, ctx, start=0):
        ctx.check()
        data = self.received_since(start)
        predicate(data)
        return data


class PythonProgrammer:
    def erase_command(self, config):
        return [sys.executable, "-c", "print('CONNECTED ERASED')"]

    def command(self, config, image=None):
        return [sys.executable, "-c", "print('CONNECTED WRITTEN VERIFIED')"]


class FeedSerial:
    """真实 SerialSession 用的最小假端口：RTS 释放沿灌入启动字节，不触碰硬件。"""

    def __init__(self, **kwargs):
        self.is_open = False
        self._rts = True
        self.on_release = None
        self.on_write = None
        self._incoming = bytearray()
        self._condition = threading.Condition()
        self.timeout = kwargs.get("timeout", 0.01)

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, value):
        old, self._rts = self._rts, value
        if self.is_open and old and not value and self.on_release:
            self.on_release()

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False

    def feed(self, data):
        with self._condition:
            self._incoming.extend(data)
            self._condition.notify_all()

    def read(self, size):
        with self._condition:
            if not self._incoming:
                self._condition.wait(self.timeout)
            data = bytes(self._incoming[:size])
            del self._incoming[:size]
            return data

    def write(self, data):
        if self.on_write:
            self.on_write(bytes(data))
        return len(data)


class OkFlash:
    """跳过真实烧录子进程，直接走到 WAIT_BOOT。"""

    def flash(self, *args, **kwargs):
        return OperationResult(True, "flash", message="fake flash success")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = config_for(self.root)
        self.sessions = []

    def session(self, config):
        serial = MemorySerial(config)
        self.sessions.append(serial)
        return serial

    def engine(self, **kwargs):
        return WorkflowEngine(session_factory=self.session,
                              flasher=kwargs.pop("flasher", FlashService(PythonProgrammer())), **kwargs)

    def test_full_workflow_real_child_processes_no_hardware(self):
        result = self.engine().execute(self.config, "run", suite=SUITE)
        self.assertTrue(result.success, result.to_dict())
        path = Path(result.data["run_path"])
        for name in ("run_summary.json", "run_config.yaml", "events.jsonl", "artifact_manifest.json",
                     "firmware.bin", "build.log", "flash.log", "serial.bin", "serial.log",
                     "serial.jsonl", "test_report.json", "agent_summary.md", "git_diff.patch"):
            self.assertTrue((path / name).is_file(), name)
        self.assertIn(b"PONG", (path / "serial.bin").read_bytes())
        self.assertTrue(self.sessions[0].closed)
        self.assertFalse(self.sessions[0].rts)
        self.assertEqual(result.data["boot"]["method"], "ready_pattern")
        events = [json.loads(line)["state"] for line in (path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertLess(events.index("BUILD"), events.index("OPEN_SERIAL"))
        self.assertLess(events.index("OPEN_SERIAL"), events.index("RELEASE_RESET_AT_CONFIGURED_TIME"))
        self.assertLess(events.index("FLASH_ERASE_SUCCEEDED"), events.index("WRITE_FLASH"))
        self.assertLess(events.index("CLEANUP"), len(events) - 1)
        self.assertEqual(events[-1], "ARCHIVE")

    def test_build_failure_does_not_open_serial_or_flash_old_bin(self):
        (self.root / "source.bin").write_bytes(b"old")
        self.config["build"]["command"] = [sys.executable, "-c", "raise SystemExit(2)"]
        result = self.engine().execute(self.config, "run", suite=SUITE)
        self.assertEqual(result.error_code, "BUILD_FAILED")
        self.assertFalse(self.sessions)
        self.assertEqual((self.root / "source.bin").read_bytes(), b"old")

    def test_flash_failure_prevents_protocol_tests(self):
        class FailedFlash:
            def flash(self, *args, **kwargs):
                return OperationResult(False, "flash", "FLASH_VERIFY_FAILED", "failed")
        runner = unittest.mock.Mock()
        result = self.engine(flasher=FailedFlash(), runner_factory=runner).execute(self.config, "run", suite=SUITE)
        self.assertEqual(result.error_code, "FLASH_VERIFY_FAILED")
        runner.assert_not_called()
        self.assertTrue(self.sessions[0].closed)

    def test_cleanup_preserves_primary_failure(self):
        class FailedFlash:
            def flash(self, *args, **kwargs):
                return OperationResult(False, "flash", "FLASH_FAILED", "original")
        class BadReset:
            state = "unknown"
            def __init__(self, *args): pass
            def release_reset(self): raise RuntimeError("wire failure")
        result = self.engine(flasher=FailedFlash(), reset_factory=BadReset).execute(self.config, "run", suite=SUITE)
        self.assertEqual(result.error_code, "FLASH_FAILED")
        self.assertFalse(result.data["cleanup"][0]["success"])

    def test_cleanup_event_failure_still_releases_and_closes_hardware(self):
        class FailedFlash:
            def flash(self, *args, **kwargs):
                return OperationResult(False, "flash", "FLASH_FAILED", "original")
        releases = []
        class RecordingReset:
            state = "ASSERTED"
            def __init__(self, *args):
                pass
            def pulse(self, duration_ms=None):
                pass
            def release_reset(self):
                releases.append(True)
        original_event = RunContext.event
        def broken_event(self, state, **data):
            if state == "CLEANUP":
                raise OSError("events.jsonl unwritable")
            return original_event(self, state, **data)
        with patch.object(RunContext, "event", broken_event):
            result = self.engine(flasher=FailedFlash(), reset_factory=RecordingReset).execute(self.config, "run", suite=SUITE)
        self.assertEqual(result.error_code, "FLASH_FAILED")  # 主错误保留，不被清理故障覆盖
        self.assertTrue(releases)
        self.assertTrue(self.sessions[0].closed)
        actions = {item["action"]: item["success"] for item in result.data["cleanup"]}
        self.assertIs(actions["release_reset"], True)
        self.assertIs(actions["close_serial"], True)
        self.assertIs(actions["close_logs"], True)
        self.assertIs(actions["cleanup_event"], False)

    def test_cleanup_event_failure_fails_otherwise_successful_run(self):
        original_event = RunContext.event
        def broken_event(self, state, **data):
            if state == "CLEANUP":
                raise OSError("events.jsonl unwritable")
            return original_event(self, state, **data)
        with patch.object(RunContext, "event", broken_event):
            result = self.engine().execute(self.config, "run", suite=SUITE)
        self.assertFalse(result.success)  # 操作成功但清理失败仍应失败
        self.assertEqual(result.error_code, "CLEANUP_FAILED")
        self.assertTrue(self.sessions[0].closed)
        self.assertFalse(self.sessions[0].rts)
        actions = {item["action"]: item["success"] for item in result.data["cleanup"]}
        self.assertIs(actions["close_serial"], True)
        self.assertIs(actions["cleanup_event"], False)

    def test_session_check_failure_after_open_releases_reset_and_closes_serial(self):
        class CheckFails(MemorySerial):
            def check(self):
                raise ToolError("SERIAL_READ_FAILED", "reader died right after open")
        made = []
        def factory(config):
            made.append(CheckFails(config))
            return made[-1]
        result = WorkflowEngine(session_factory=factory).execute(self.config, "monitor", duration=0.05)
        self.assertEqual(result.error_code, "SERIAL_READ_FAILED")
        self.assertTrue(made[0].closed)
        self.assertFalse(made[0].rts)  # 已确认释放值（asserted_rts_value=True）
        release = [item for item in result.data["cleanup"] if item["action"] == "release_reset"]
        self.assertEqual(release[0]["state"], "RELEASED")
        self.assertTrue(release[0]["success"])
        actions = {item["action"]: item["success"] for item in result.data["cleanup"]}
        self.assertIn("close_serial", actions)
        self.assertTrue(all(actions.values()))

    def test_session_open_failure_closes_logs_and_session_without_release(self):
        class OpenFails(MemorySerial):
            def open(self):
                raise ToolError("SERIAL_OPEN_FAILED", "port vanished")
        made = []
        def factory(config):
            made.append(OpenFails(config))
            return made[-1]
        result = WorkflowEngine(session_factory=factory).execute(self.config, "monitor", duration=0.05)
        self.assertEqual(result.error_code, "SERIAL_OPEN_FAILED")
        self.assertTrue(made[0].closed)
        actions = {item["action"]: item["success"] for item in result.data["cleanup"]}
        self.assertIn("close_logs", actions)
        self.assertNotIn("release_reset", actions)
        self.assertTrue(all(actions.values()))

    def test_startup_pathological_ready_pattern_is_bounded_and_reclaims_worker(self):
        port = FeedSerial()
        port.on_release = lambda: port.feed(b"x" * 23)
        engine = WorkflowEngine(session_factory=lambda config: SerialSession(config, factory=lambda **kwargs: port),
                                 flasher=OkFlash())
        config = copy.deepcopy(self.config)
        config["startup"] = {"ready_pattern": "(x+x+)+y", "timeout_s": 0.05, "settle_delay_s": 0}
        began = time.monotonic()
        result = engine.execute(config, "run", suite=SUITE)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "BOOT_TIMEOUT")
        self.assertEqual(result.data["failure"]["received_hex"], (b"x" * 23).hex())
        self.assertLess(time.monotonic() - began, 6)  # 硬上界：启动超时 50ms，不允许同步回溯拖垮流程
        self.assertTrue(all(item["success"] for item in result.data["cleanup"]))
        self.assertFalse(port.rts)
        self.assertFalse(port.is_open)
        self.assertFalse(any(p.name == "protocol-regex" for p in multiprocessing.active_children()))

    def test_total_deadline_interrupts_pathological_startup_and_still_cleans_up(self):
        port = FeedSerial()
        port.on_release = lambda: port.feed(b"x" * 26)
        engine = WorkflowEngine(session_factory=lambda config: SerialSession(config, factory=lambda **kwargs: port),
                                 flasher=OkFlash())
        config = copy.deepcopy(self.config)
        config["startup"] = {"ready_pattern": "(x+x+)+y", "timeout_s": 30, "settle_delay_s": 0}
        began = time.monotonic()
        result = engine.execute(config, "run", suite=SUITE, timeout_s=2)
        self.assertEqual(result.error_code, "WORKFLOW_TIMEOUT")
        self.assertLess(time.monotonic() - began, 8)  # 硬上界：总截止 2s + 回收/清理
        self.assertFalse(port.rts)
        self.assertFalse(port.is_open)
        self.assertFalse(any(p.name == "protocol-regex" for p in multiprocessing.active_children()))
        self.assertTrue(all(item["success"] for item in result.data["cleanup"]))

    def test_startup_ready_pattern_keeps_text_regex_semantics(self):
        port = FeedSerial()
        port.on_release = lambda: port.feed("你".encode("utf-8"))
        port.on_write = lambda data: port.feed(b"PONG\r\n")
        engine = WorkflowEngine(session_factory=lambda config: SerialSession(config, factory=lambda **kwargs: port),
                                 flasher=OkFlash())
        config = copy.deepcopy(self.config)
        config["startup"] = {"ready_pattern": "^.$", "timeout_s": 2, "settle_delay_s": 0}
        result = engine.execute(config, "run", suite=SUITE)
        self.assertTrue(result.success, result.to_dict())
        self.assertEqual(result.data["boot"]["method"], "ready_pattern")
        # 单个多字节字符按文本计一个字符；若退化为字节匹配，^.$ 会误判 BOOT_TIMEOUT。
        self.assertEqual(result.data["boot"]["received_hex"], "你".encode("utf-8").hex())

    def test_startup_matcher_failure_is_boot_matcher_failed_not_repairable(self):
        from multiprocessing.context import SpawnProcess
        port = FeedSerial()
        port.on_release = lambda: port.feed(b"BOOT READY\n")
        engine = WorkflowEngine(session_factory=lambda config: SerialSession(config, factory=lambda **kwargs: port),
                                 flasher=OkFlash())
        config = copy.deepcopy(self.config)
        config["startup"] = {"ready_pattern": "BOOT READY", "timeout_s": 2, "settle_delay_s": 0}
        with patch.object(SpawnProcess, "start", side_effect=OSError("spawn failed")):
            result = engine.execute(config, "run", suite=SUITE)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "BOOT_MATCHER_FAILED")  # 不误报为协议测试失败
        self.assertNotIn(result.error_code, {"BOOT_TIMEOUT", "PROTOCOL_TEST_FAILED"})
        self.assertFalse(port.is_open)
        self.assertFalse(port.rts)

    def test_invalid_suite_blocked_before_build(self):
        builder = unittest.mock.Mock()
        result = self.engine(builder=builder).execute(self.config, "run", suite={"tests": []})
        self.assertEqual(result.error_code, "CONFIG_INVALID")
        builder.build.assert_not_called()
        self.assertFalse(self.sessions)

    def test_unverified_hardware_blocked(self):
        self.config["reset"]["polarity_verified"] = False
        result = self.engine().execute(self.config, "monitor")
        self.assertEqual(result.error_code, "CONFIG_INVALID")
        self.assertFalse(self.sessions)

    def test_dry_run_does_not_call_hardware_or_build(self):
        self.config["reset"]["asserted_rts_value"] = "TODO_MEASURE"
        builder = unittest.mock.Mock()
        result = self.engine(builder=builder).execute(self.config, "run", suite=SUITE, dry_run=True)
        self.assertTrue(result.success)
        self.assertFalse(result.data["ready_for_execution"])
        builder.build.assert_not_called()
        self.assertFalse(self.sessions)

    def test_overall_timeout_closes_monitor(self):
        result = self.engine().execute(self.config, "monitor", duration=1, timeout_s=.02)
        self.assertEqual(result.error_code, "WORKFLOW_TIMEOUT")
        if self.sessions:
            self.assertTrue(self.sessions[0].closed)

    def test_keyboard_interrupt_closes_monitor(self):
        def interrupt(*args): raise KeyboardInterrupt()
        with patch.object(RunContext, "sleep", interrupt):
            result = self.engine().execute(self.config, "monitor")
        self.assertEqual(result.error_code, "CANCELLED")
        self.assertTrue(self.sessions[0].closed)

    def test_runs_do_not_overwrite(self):
        one = self.engine().execute(self.config, "monitor", dry_run=True)
        two = self.engine().execute(self.config, "monitor", dry_run=True)
        self.assertNotEqual(one.run_id, two.run_id)

    def test_build_without_hardware_verification(self):
        self.config["target"]["status"] = "template"
        self.config["reset"]["polarity_verified"] = False
        self.config["flash"]["compatibility_verified"] = False
        result = self.engine().execute(self.config, "build")
        self.assertTrue(result.success, result.to_dict())
        self.assertFalse(self.sessions)


class ConfigAndCLITests(unittest.TestCase):
    def test_dry_run_exposes_argv_and_invalid_suite_blocks_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            result = WorkflowEngine.plan(config, "run", SUITE)
            self.assertTrue(result.data["ready_for_execution"])
            self.assertEqual(result.data["commands"]["build"], config["build"]["command"])
            self.assertIn("-v", result.data["commands"]["programmer"])
            self.assertEqual(result.data["commands"]["erase"][-2:], ["-e", "all"])
            self.assertIn("KV", result.data["destructive_warning"])
            self.assertLess(result.data["steps"].index("ERASE_FLASH"), result.data["steps"].index("WAIT_FLASH_AND_VERIFY"))
            invalid = WorkflowEngine.plan(config, "run", {"tests": []})
            self.assertFalse(invalid.data["ready_for_execution"])

    def test_session_dtr_control(self):
        from embedded_auto.services.serial_session import SerialSession
        with tempfile.TemporaryDirectory() as directory:
            session = SerialSession(config_for(Path(directory)))
            port = unittest.mock.Mock(is_open=True)
            session._port = port
            session.set_dtr(True)
            self.assertTrue(port.dtr)
            session.set_dtr(False)
            self.assertFalse(port.dtr)
            session.close()

    def test_module_entrypoint_safe_for_windows_multiprocessing(self):
        completed = subprocess.run([sys.executable, "-c", "import runpy; runpy.run_module('embedded_auto', run_name='__mp_main__')"], capture_output=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(completed.stdout)

    def test_templates_load_and_fail_closed(self):
        for target in ("stm32f030", "stm32f070", "apm32e030"):
            config = load_config("configs/app.yaml", target)
            self.assertEqual(config["target"]["status"], "template")
            self.assertEqual(int(config["flash"]["address"], 0), 0x08000000)
            self.assertIn("erase_success_pattern", config["flash"])
            for operation in ("run", "connect", "reset", "monitor"):
                with self.assertRaises(ToolError):
                    validate_config(config, operation)

    def test_stm32_and_apm32_require_uniform_base_address(self):
        with tempfile.TemporaryDirectory() as directory:
            for family in ("STM32", "APM32"):
                config = config_for(Path(directory))
                config["target"]["family"] = family
                validate_config(config, "run")
                config["flash"]["address"] = 0x08002000
                with self.assertRaises(ToolError) as error:
                    validate_config(config, "run")
                self.assertEqual(error.exception.code, "CONFIG_INVALID")

    def test_connect_dry_run_has_no_erase(self):
        with tempfile.TemporaryDirectory() as directory:
            result = WorkflowEngine.plan(config_for(Path(directory)), "connect")
            self.assertNotIn("erase", result.data["commands"])
            self.assertNotIn("ERASE_FLASH", result.data["steps"])
            self.assertNotIn("-e", result.data["commands"]["programmer"])
            self.assertIsNone(result.data["destructive_warning"])

    def test_common_options_before_or_after_subcommand(self):
        for argv in (("--json", "--target", "fake", "build"), ("build", "--target", "fake", "--json"),
                     ("--json", "device", "--target", "fake", "check")):
            args = parser().parse_args(argv)
            self.assertTrue(args.json)
            self.assertEqual(args.target, "fake")

    def test_json_config_error(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["run", "--json", "--suite", "example_protocol"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["error_code"], "CONFIG_INVALID")

    def test_identifiers_reject_traversal(self):
        for target in ("../secret", "a/b", "", ".."):
            with self.assertRaises(ToolError):
                load_config("configs/app.yaml", target)

    def test_cli_dry_run_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_for(root)
            (root / "targets").mkdir()
            (root / "app.yaml").write_text(yaml.safe_dump({"app": config.pop("app")}), encoding="utf-8")
            (root / "targets" / "fake.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["monitor", "--target", "fake", "--config", str(root / "app.yaml"), "--dry-run", "--json"])
            self.assertEqual(code, 0)
            run = json.loads(output.getvalue())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["report", "--run-id", run["run_id"], "--config", str(root / "app.yaml"), "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["run_id"], run["run_id"])

    def test_nan_timeout_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ToolError):
                RunContext(config_for(Path(directory)), "monitor", float("nan"))


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ctx = RunContext(config_for(self.root), "repair")

    def test_limit_stops_after_three_repairs(self):
        repairs = []
        loop = RepairLoop(RepairPolicy(("fake",)))
        result = loop.run("fake", lambda: OperationResult(False, "run", "PROTOCOL_TEST_FAILED"),
                          lambda number, previous: repairs.append(number), self.ctx)
        self.assertEqual(repairs, [1, 2, 3])
        self.assertEqual(result.error_code, "REPAIR_LIMIT_REACHED")
        self.assertEqual(len(result.data["iterations"]), 4)

    def test_hardware_failure_does_not_repair(self):
        repair = unittest.mock.Mock()
        result = RepairLoop(RepairPolicy(("fake",))).run("fake", lambda: OperationResult(False, "run", "FLASH_FAILED"), repair, self.ctx)
        repair.assert_not_called()
        self.assertEqual(result.data["stop_reason"], "ENVIRONMENT_OR_HARDWARE_FAILURE")

    def test_success_stops_immediately(self):
        repair = unittest.mock.Mock()
        result = RepairLoop(RepairPolicy(("fake",))).run("fake", lambda: OperationResult(True, "run"), repair, self.ctx)
        self.assertTrue(result.success)
        repair.assert_not_called()

    def test_protected_expectation_cannot_silently_change(self):
        expectation = self.root / "suite.yaml"
        expectation.write_text("PONG", encoding="utf-8")
        def change(number, previous): expectation.write_text("ANYTHING", encoding="utf-8")
        result = RepairLoop(RepairPolicy(("fake",)), [expectation]).run(
            "fake", lambda: OperationResult(False, "run", "PROTOCOL_TEST_FAILED"), change, self.ctx)
        self.assertEqual(result.error_code, "REPAIR_POLICY_VIOLATION")
        self.assertEqual(expectation.read_text(encoding="utf-8"), "ANYTHING")  # 不擅自回滚用户修改

    def test_invalid_limits_and_target(self):
        for value in (-1, 101, True, float("inf")):
            with self.assertRaises(ToolError): RepairPolicy(("fake",), value)
        execute = unittest.mock.Mock()
        result = RepairLoop(RepairPolicy(("other",))).run("fake", execute, lambda *args: None, self.ctx)
        self.assertEqual(result.error_code, "REPAIR_POLICY_VIOLATION")
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
