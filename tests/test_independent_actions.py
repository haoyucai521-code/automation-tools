import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from embedded_auto.agent.contract import describe_contract
from embedded_auto.application.workflow_engine import WorkflowEngine
from embedded_auto.cli import main, parser
from embedded_auto.core.errors import ToolError
from embedded_auto.core.models import OperationResult
from embedded_auto.core.run_context import RunContext
from embedded_auto.serial.serial_session import SerialSession
from test_workflow_cli import FeedSerial, MemorySerial, config_for


class IndependentActionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = config_for(self.root)
        self.builder = Mock()
        self.flasher = Mock()
        self.sessions = []
        self.engine = WorkflowEngine(builder=self.builder, flasher=self.flasher, session_factory=self.fake_session)

    def fake_session(self, config):
        session = MemorySerial(config)
        self.sessions.append(session)
        return session

    def run_dir(self, result):
        return Path(result.data["run_path"])

    def build_snapshot(self):
        result = WorkflowEngine(session_factory=lambda _: self.fail("build opened serial")).execute(self.config, "build")
        self.assertTrue(result.success, result.to_dict())
        return self.run_dir(result) / "firmware.bin"

    def test_preflight_checks_concrete_artifact_without_connecting_or_authorizing(self):
        image = self.build_snapshot()
        with patch("subprocess.Popen", side_effect=AssertionError("preflight started a process")):
            result = self.engine.execute(self.config, "mcu preflight", artifact=image)
        denied = self.engine.execute(self.config, "mcu program", artifact=image)
        self.assertTrue(result.success, result.to_dict())
        self.assertEqual(result.operation, "mcu preflight")
        self.assertTrue(result.data["offline_checks_passed"])
        self.assertTrue(result.data["authorization_required"])
        self.assertFalse(result.data["authorization_granted"])
        self.assertFalse(result.data["device_identity_verified"])
        self.assertFalse(result.data["hardware_actions_executed"])
        self.assertEqual(result.data["artifact"]["image"], str(image))
        self.assertEqual(result.data["artifact"]["size"], 8)
        scope = result.data["erase_scope"]
        self.assertEqual(scope["erase_size"], 2048)
        self.assertEqual(scope["address"], 0x08000000)
        # 预检数据本身不进入执行路径；同一具体镜像无显式授权仍然失败。
        self.assertEqual(denied.error_code, "FLASH_DESTRUCTIVE_AUTH_REQUIRED", denied.to_dict())
        self.assertEqual(denied.operation, "mcu program")
        self.assertFalse(self.sessions)
        self.builder.build.assert_not_called()
        self.flasher.flash.assert_not_called()
        self.assertFalse((self.run_dir(result) / "serial.bin").exists())
        self.assertFalse((self.run_dir(result) / "git_state.json").exists())

    def test_preflight_reports_independent_blockers_for_tamper_preserve_and_tool(self):
        image = self.build_snapshot()
        image.write_bytes(b"tampered")
        self.config["flash"].update(preserve_regions=[{"start": 0x08000400, "size": 4}],
                                    executable=str(self.root / "missing.exe"))
        result = self.engine.execute(self.config, "mcu preflight", artifact=image, dry_run=True)
        issues = {item["stage"]: item["error_code"] for item in result.data["blocking_issues"]}
        self.assertEqual(issues, {"artifact": "ARTIFACT_HASH_MISMATCH", "erase_scope": "FLASH_REGION_CONFLICT",
                                  "configuration": "FLASH_TOOL_NOT_FOUND"})
        self.assertFalse(result.success)
        self.assertFalse(self.sessions)

    def test_mcu_reset_and_connect_do_not_build_or_program(self):
        reset = self.engine.execute(self.config, "mcu reset")
        self.assertTrue(reset.success, reset.to_dict())
        self.assertEqual(self.sessions[0].data, b"BOOT READY\n")
        self.assertTrue(self.sessions[0].closed)
        self.flasher.flash.assert_not_called()
        self.flasher.flash.return_value = OperationResult(True, "connect")
        connect = self.engine.execute(self.config, "mcu connect")
        self.assertTrue(connect.success, connect.to_dict())
        self.assertEqual(connect.operation, "mcu connect")
        self.assertEqual(self.flasher.flash.call_args.kwargs, {"connect_only": True})
        self.assertEqual(self.sessions[1].data, b"")
        self.assertTrue(self.sessions[1].closed)
        self.builder.build.assert_not_called()

    def test_read_and_capture_do_not_send_or_pulse_and_preserve_untrimmed_bytes(self):
        incoming = b"\0" + b"x" * 299 + b"\xff"

        class Incoming(MemorySerial):
            def open(self):
                super().open()
                self.emit("rx", incoming)

            def write(self, data):
                raise AssertionError("read/capture sent bytes")

            def set_rts(self, value):
                if value is not False:
                    raise AssertionError("read/capture asserted reset")
                super().set_rts(value)

        self.engine.session_factory = Incoming
        for operation in ("serial read", "serial capture"):
            with self.subTest(operation=operation):
                result = self.engine.execute(self.config, operation, duration=.005)
                self.assertTrue(result.success, result.to_dict())
                self.assertEqual(result.data["rx_bytes"], len(incoming))
                self.assertEqual((self.run_dir(result) / "serial.bin").read_bytes(), incoming)
                events = (self.run_dir(result) / "serial.jsonl").read_text().splitlines()
                self.assertEqual([json.loads(event)["direction"] for event in events], ["rx"])
                if operation == "serial read":
                    self.assertTrue(result.data["rx_preview_truncated"])
                    self.assertEqual(result.data["rx_preview_hex"], incoming[:256].hex())
                else:
                    self.assertNotIn("rx_preview_hex", result.data)

    def test_send_once_and_preserve_immediate_reply_in_same_session(self):
        class Incoming(MemorySerial):
            def open(self):
                super().open()
                self.emit("rx", b"prior")

        self.engine.session_factory = Incoming
        result = self.engine.execute(self.config, "serial send", send_text="你好", listen_after=.01)
        self.assertTrue(result.success, result.to_dict())
        self.assertEqual(result.data["tx_status"], "WRITE_COMPLETED")
        self.assertEqual(result.data["tx_bytes"], 6)
        self.assertEqual(result.data["rx_start_offset"], 5)
        self.assertEqual(result.data["rx_preview_text"], "PONG\r\n")
        self.assertEqual((self.run_dir(result) / "serial.bin").read_bytes(), b"priorPONG\r\n")
        events = [json.loads(line) for line in (self.run_dir(result) / "serial.jsonl").read_text().splitlines()]
        self.assertEqual([item["hex"] for item in events if item["direction"] == "tx"], ["你好".encode().hex()])

    def test_serial_prevalidation_and_dry_run_do_not_open(self):
        for options in ({"send_hex": "xyz"}, {"send_text": ""}, {"send_hex": "a0" * 4097},
                        {"send_hex": "00", "listen_after": -1}, {"send_text": "a", "send_hex": "00"},
                        {"send_hex": "00", "timeout_s": .5}, {"send_text": 123}):
            with self.subTest(options=options):
                result = self.engine.execute(self.config, "serial send", **options)
                self.assertFalse(result.success, result.to_dict())
        self.config["serial"]["write_timeout_s"] = "bad"
        result = self.engine.execute(self.config, "serial send", send_hex="aa", dry_run=True)
        self.assertTrue(result.success)
        self.assertFalse(result.data["ready_for_execution"])
        self.assertFalse(self.sessions)

    def test_read_and_capture_reject_impossible_budget_before_open(self):
        for operation in ("serial read", "serial capture"):
            for dry_run in (False, True):
                with self.subTest(operation=operation, dry_run=dry_run):
                    result = self.engine.execute(self.config, operation, duration=2, timeout_s=.1, dry_run=dry_run)
                    if dry_run:
                        self.assertTrue(result.success)
                        self.assertFalse(result.data["ready_for_execution"])
                        self.assertTrue(any("WORKFLOW_TIMEOUT" in item for item in result.data["blocking_issues"]))
                    else:
                        self.assertEqual(result.error_code, "WORKFLOW_TIMEOUT", result.to_dict())
        self.assertFalse(self.sessions)

    def test_invalid_text_codec_is_rejected_before_open(self):
        self.config["serial"]["encoding"] = "base64_codec"
        result = self.engine.execute(self.config, "serial read", duration=.01)
        self.assertEqual(result.error_code, "CONFIG_INVALID", result.to_dict())
        self.assertFalse(self.sessions)

    def test_short_reply_does_not_wait_for_full_read_or_driver_timeout(self):
        class FillReadPort(FeedSerial):
            def __init__(self):
                super().__init__()
                self.read_started = threading.Event()
                self.read_cancelled = False

            @property
            def in_waiting(self):
                with self._condition:
                    return len(self._incoming)

            def read(self, size):
                with self._condition:
                    self.read_started.set()
                    self._condition.wait_for(lambda: len(self._incoming) >= size or self.read_cancelled, self.timeout)
                    if self.read_cancelled:
                        return b""
                    data = bytes(self._incoming[:size])
                    del self._incoming[:size]
                    return data

            def cancel_read(self):
                with self._condition:
                    self.read_cancelled = True
                    self._condition.notify_all()

        port = FillReadPort()
        # 让假驱动等满请求的长度，或等待读超时；监听窗口应先于读超时结束。
        port.timeout = .5

        def reply(_):
            self.assertTrue(port.read_started.wait(1))
            port.feed(b"OK")

        port.on_write = reply
        self.engine.session_factory = lambda config: SerialSession(config, factory=lambda **_: port)
        result = self.engine.execute(self.config, "serial send", send_hex="A5", listen_after=.08)
        self.assertTrue(result.success, result.to_dict())
        self.assertEqual(result.data["rx_preview_hex"], "4f4b")
        self.assertEqual((self.run_dir(result) / "serial.bin").read_bytes(), b"OK")
        self.assertFalse(port.is_open)

    def test_interrupt_during_open_releases_and_closes_port(self):
        class InterruptOpenPort(FeedSerial):
            def open(self):
                super().open()
                raise KeyboardInterrupt

        port = InterruptOpenPort()
        self.engine.session_factory = lambda config: SerialSession(config, factory=lambda **_: port)
        result = self.engine.execute(self.config, "serial read", duration=.01)
        self.assertEqual(result.error_code, "CANCELLED", result.to_dict())
        self.assertFalse(port.is_open)
        self.assertTrue(result.data["reset_cleanup"]["release_api_succeeded"])

    def test_read_timeout_and_failed_send_close_the_real_session_without_retry(self):
        port = FeedSerial()
        self.engine.session_factory = lambda config: SerialSession(config, factory=lambda **_: port)
        with patch.object(RunContext, "sleep", side_effect=ToolError("WORKFLOW_TIMEOUT", "模拟采集期间总超时")):
            result = self.engine.execute(self.config, "serial read", duration=.1)
        self.assertEqual(result.error_code, "WORKFLOW_TIMEOUT", result.to_dict())
        self.assertFalse(port.is_open)
        self.assertTrue(result.data["reset_cleanup"]["port_close_succeeded"])
        self.assertEqual(result.data["reset_cleanup"]["physical_reset_state"], "UNKNOWN")
        port.on_write = Mock(side_effect=OSError("write failed"))
        result = self.engine.execute(self.config, "serial send", send_hex="A5")
        self.assertEqual(result.error_code, "SERIAL_WRITE_FAILED", result.to_dict())
        self.assertEqual(result.data["tx_status"], "UNKNOWN")
        port.on_write.assert_called_once_with(b"\xa5")
        self.assertFalse(port.is_open)

    def test_busy_port_does_not_open_a_second_handle(self):
        port = FeedSerial()
        port_factory = Mock(return_value=port)
        self.engine.session_factory = lambda config: SerialSession(config, factory=port_factory)
        owner = self.engine.session_factory(self.config)
        owner.open()
        try:
            result = self.engine.execute(self.config, "serial read", duration=.005)
            self.assertEqual(result.error_code, "SERIAL_PORT_BUSY", result.to_dict())
            self.assertTrue(port.is_open)
            port_factory.assert_called_once()
        finally:
            owner.close()

    def test_serial_cli_loads_target_without_firmware_sections(self):
        app = self.root / "app.yaml"
        targets = self.root / "targets"
        targets.mkdir()
        app.write_text(yaml.safe_dump({key: self.config[key] for key in ("app", "serial")}), encoding="utf-8")
        (targets / "fake.yaml").write_text(yaml.safe_dump({key: self.config[key] for key in ("target", "reset")}),
                                           encoding="utf-8")
        output = io.StringIO()
        with patch("embedded_auto.application.workflow_engine.WorkflowEngine", return_value=self.engine), contextlib.redirect_stdout(output):
            code = main(["--config", str(app), "serial", "--target", "fake", "send", "--hex", "00 FF", "--json"])
        self.assertEqual(code, 0, output.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual(result["operation"], "serial send")
        self.assertEqual(result["data"]["tx_requested_hex"], "00ff")
        self.assertNotIn("rx_preview_hex", result["data"])

    def test_mcu_cli_loads_only_sections_required_for_the_action(self):
        image = self.build_snapshot()
        app = self.root / "app.yaml"
        targets = self.root / "targets"
        targets.mkdir()
        app.write_text(yaml.safe_dump({key: self.config[key] for key in ("app", "serial")}), encoding="utf-8")
        target = targets / "fake.yaml"
        for action, sections, options in (
            ("preflight", ("target", "reset", "flash"), ["--artifact", str(image)]),
            ("reset", ("target", "reset"), []),
        ):
            with self.subTest(action=action):
                target.write_text(yaml.safe_dump({key: self.config[key] for key in sections}), encoding="utf-8")
                output = io.StringIO()
                with patch("embedded_auto.application.workflow_engine.WorkflowEngine", return_value=self.engine), contextlib.redirect_stdout(output):
                    code = main(["--config", str(app), "mcu", action, "--target", "fake", "--json", *options])
                self.assertEqual(code, 0, output.getvalue())
                self.assertEqual(json.loads(output.getvalue())["operation"], f"mcu {action}")
        self.assertEqual(len(self.sessions), 1)
        self.assertTrue(self.sessions[0].closed)

    def test_contract_prioritizes_single_actions_and_cli_has_no_unverified_commands(self):
        contract = describe_contract()
        self.assertEqual(set(contract["tool_groups"]), {"mcu", "serial", "ble"})
        self.assertEqual(sum(map(len, contract["tool_groups"].values())), 12)
        self.assertNotIn("run_workflow", contract["tools"])
        self.assertEqual(contract["optional_tools"]["run_workflow"], "run")
        for name in ("erase", "write", "verify"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                parser().parse_args(["mcu", name])
            self.assertEqual(exc.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
