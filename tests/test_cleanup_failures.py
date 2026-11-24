"""主错误与清理/日志错误分别保留，不打开真实硬件。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from embedded_auto.cli import report
from embedded_auto.core.errors import ToolError
from embedded_auto.core.process import ProcessRunner
from embedded_auto.firmware.flash_service import FlashService, ResetAssistedConnectStrategy
from embedded_auto.core.models import OperationResult
from embedded_auto.core.run_context import RunContext
from embedded_auto.application.workflow_engine import WorkflowEngine
from embedded_auto.serial.serial_session import SerialSession
from test_build_flash import FakeContext, FakeReset, FakeSession
from test_workflow_cli import FeedSerial, config_for


class CleanupFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ctx = FakeContext(self.root / "run", {})

    def test_reset_cleanup_does_not_replace_primary_error(self):
        class FailedRunner:
            def run(self, *args, **kwargs):
                raise ToolError("PROCESS_TIMEOUT", "original timeout", {"evidence": "original"})

        reset = FakeReset(fail_release=True)
        with self.assertRaises(ToolError) as caught:
            ResetAssistedConnectStrategy(FailedRunner()).run(
                ["fake"], self.root, 1, self.ctx, self.root / "log", FakeSession(), reset, 1)
        self.assertEqual(caught.exception.code, "PROCESS_TIMEOUT")
        self.assertEqual(caught.exception.data["evidence"], "original")
        self.assertIn("release failed", caught.exception.data["reset_release_error"])
        self.assertEqual(caught.exception.data["hardware_state"], "UNKNOWN")
        self.assertEqual(reset.actions, ["assert", "release"])

    def test_log_failure_does_not_replace_primary_error(self):
        self.ctx.cancelled.set()
        with patch.object(Path, "open", side_effect=OSError("log unavailable")):
            with self.assertRaises(ToolError) as caught:
                ProcessRunner().run([sys.executable, "-c", "pass"], self.root, 1,
                                    self.ctx, self.root / "process.log")
        self.assertEqual(caught.exception.code, "CANCELLED")
        self.assertIn("log unavailable", caught.exception.data["error_log_write_error"])

    def test_process_cleanup_error_preserves_primary_timeout(self):
        original = ProcessRunner._stop_tree
        def stop_then_fail(process):
            original(process)
            raise ToolError("PROCESS_CLEANUP_FAILED", "injected cleanup failure")
        with patch.object(ProcessRunner, "_stop_tree", side_effect=stop_then_fail):
            with self.assertRaises(ToolError) as caught:
                ProcessRunner().run([sys.executable, "-c", "import time;time.sleep(5)"], self.root,
                                    .2, self.ctx, self.root / "process.log")
        self.assertEqual(caught.exception.code, "PROCESS_TIMEOUT")
        self.assertIn("injected cleanup failure", caught.exception.data["cleanup_error"])

    def test_flash_archive_failure_does_not_replace_primary(self):
        primary = OperationResult(False, "flash", "FLASH_VERIFY_FAILED", "verify failed")
        with patch.object(Path, "open", side_effect=OSError("archive unavailable")):
            result = FlashService._archive(self.ctx, primary)
        self.assertEqual(result.error_code, "FLASH_VERIFY_FAILED")
        self.assertIn("archive unavailable", result.data["archive_error"])

    def test_workflow_event_and_archive_failure_preserve_primary(self):
        config = config_for(self.root)
        original_event = RunContext.event
        def event(ctx, state, **data):
            if state == "FAILED":
                raise OSError("failed event unavailable")
            return original_event(ctx, state, **data)
        with patch("embedded_auto.application.workflow_engine.validate_config",
                   side_effect=ToolError("SERIAL_OPEN_FAILED", "primary")), \
             patch.object(RunContext, "event", event), \
             patch.object(RunContext, "finish", side_effect=OSError("archive unavailable")):
            result = WorkflowEngine().execute(config, "monitor")
        self.assertEqual(result.error_code, "SERIAL_OPEN_FAILED")
        self.assertIn("archive unavailable", result.data["archive_error"])
        self.assertTrue(any(x["action"] == "failure_event" and not x["success"] for x in result.data["cleanup"]))

    def test_markdown_archive_failure_agrees_with_report_without_replacing_primary(self):
        config = config_for(self.root)
        config_path = self.root / "app.yaml"
        config_path.write_text("app:\n  workspace: .\n  runs_dir: runs\n", encoding="utf-8")
        write_text = Path.write_text

        def fail_markdown(path, *args, **kwargs):
            if path.name == "agent_summary.md.tmp":
                raise OSError("summary unavailable")
            return write_text(path, *args, **kwargs)

        with patch.object(Path, "write_text", fail_markdown):
            plan = WorkflowEngine().execute(config, "build", dry_run=True)
            with patch("embedded_auto.application.workflow_engine.validate_config",
                       side_effect=ToolError("SERIAL_OPEN_FAILED", "primary")):
                failed = WorkflowEngine().execute(config, "monitor")

        for result, code in ((plan, "ARCHIVE_FAILED"), (failed, "SERIAL_OPEN_FAILED")):
            with self.subTest(code=code):
                self.assertFalse(result.success)
                self.assertEqual(result.error_code, code)
                self.assertIn("summary unavailable", result.data["archive_error"])
                self.assertEqual(report(config_path, result.run_id), result.to_dict())
                run_path = Path(result.data["run_path"])
                self.assertFalse((run_path / "agent_summary.md").exists())
                events = (run_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
                archive = json.loads(events[-1])
                self.assertEqual((archive["state"], archive["success"], archive["error_code"]), ("ARCHIVE", False, code))

    def test_release_api_success_is_not_event_write_success(self):
        config = config_for(self.root)
        port = FeedSerial()
        original_event = RunContext.event
        def event(ctx, state, **data):
            if state == "RESET_RELEASED":
                raise OSError("event failed after RTS")
            return original_event(ctx, state, **data)
        engine = WorkflowEngine(session_factory=lambda c: SerialSession(c, factory=lambda **kwargs: port))
        with patch.object(RunContext, "event", event):
            result = engine.execute(config, "monitor", duration=.001)
        self.assertEqual(result.error_code, "CLEANUP_FAILED")
        self.assertEqual(result.data["reset_cleanup"], {"release_attempted": True,
                         "release_api_succeeded": True, "port_close_succeeded": True,
                         "physical_reset_state": "UNKNOWN"})

    def test_open_reader_failure_records_internal_release_and_close(self):
        config = config_for(self.root)
        port = FeedSerial()
        original = SerialSession._start_reader
        def start(session):
            if session._port is not None and session._port.is_open:
                raise OSError("reader start failed")
            return original(session)
        engine = WorkflowEngine(session_factory=lambda c: SerialSession(c, factory=lambda **kwargs: port))
        with patch.object(SerialSession, "_start_reader", start):
            result = engine.execute(config, "monitor", duration=.001)
        self.assertEqual(result.error_code, "SERIAL_OPEN_FAILED")
        self.assertEqual(result.data["reset_cleanup"], {"release_attempted": True,
                         "release_api_succeeded": True, "port_close_succeeded": True,
                         "physical_reset_state": "UNKNOWN"})
        self.assertFalse(port.is_open)


if __name__ == "__main__":
    unittest.main()
