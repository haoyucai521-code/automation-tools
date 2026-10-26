"""CLI/编排/归档的离线回归；任何测试均不得触发真实蓝牙。"""
import asyncio
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from embedded_auto.adapters.ble.fake import FakeBackend
from embedded_auto.application.ble_workflow import BleWorkflow
from embedded_auto.ble.config import prepare_config
from embedded_auto.ble.session import BleSession
from embedded_auto.cli import main, parser, report
from embedded_auto.core.config import load_yaml
from embedded_auto.core.errors import ToolError


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "configs/ble/demo.yaml"
SERVICE = "12345678-1234-5678-1234-56789abc0000"
CHAR = "12345678-1234-5678-1234-56789abc0001"


class BleWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        raw = load_yaml(DEMO)
        raw["app"]["workspace"] = str(self.root)
        self.config = prepare_config(raw, DEMO)
        self.config["ble"]["operation_timeout_s"] = .1
        self.config["ble"]["cleanup_timeout_s"] = .1
        self.profile = load_yaml(Path(self.config["ble"]["fake_profile"]))
        self.suite = load_yaml(ROOT / "configs/ble/suites/demo.yaml")
        self.suite["tests"][0]["steps"][4]["timeout_s"] = .1
        self.options = {"service": SERVICE, "characteristic": CHAR}
        self.backends = []
        self.faults = {}

    def backend(self, config, profile):
        backend = FakeBackend(profile, config["ble"]["operation_timeout_s"], faults=self.faults)
        self.backends.append(backend)
        return backend

    def run_ble(self, command, **kwargs):
        options = {**self.options, **kwargs.pop("options", {})}
        return BleWorkflow(self.backend).execute(self.config, command, options=options,
                                                 profile=self.profile, **kwargs)

    def test_scan_is_operation_not_assertion(self):
        result = self.run_ble("scan")
        self.assertTrue(result.success)
        self.assertEqual(result.data["verdict"], "NOT_EVALUATED")
        self.assertTrue(result.data["simulated"])
        self.assertFalse(any(c[0] == "connect" for c in self.backends[0].calls))

    def test_scan_empty_is_success_but_connect_empty_is_not_found(self):
        class Empty(FakeBackend):
            async def scan(self, timeout_s):
                return []
        workflow = BleWorkflow(lambda config, profile: Empty(profile))
        self.assertTrue(workflow.execute(self.config, "scan", profile=self.profile).success)
        result = workflow.execute(self.config, "inspect", profile=self.profile)
        self.assertEqual(result.error_code, "BLE_DEVICE_NOT_FOUND")

    def test_inspect_preserves_diagnostic_handles(self):
        result = self.run_ble("inspect")
        self.assertTrue(result.success)
        self.assertIsInstance(result.data["services"][0]["characteristics"][0]["handle"], int)
        self.assertEqual(result.data["verdict"], "NOT_EVALUATED")

    def test_read_without_expect_is_not_pass(self):
        result = self.run_ble("read")
        self.assertTrue(result.success)
        self.assertEqual(result.data["actual_hex"], "0100")
        self.assertEqual(result.data["verdict"], "NOT_EVALUATED")

    def test_read_expect_pass_and_fail_from_same_profile(self):
        good = self.run_ble("read", options={"expect_hex": "01 00"})
        bad = self.run_ble("read", options={"expect_hex": "ff"})
        self.assertTrue(good.success)
        self.assertEqual(good.data["verdict"], "PASS")
        self.assertEqual(bad.error_code, "BLE_ASSERTION_FAILED")
        self.assertEqual(bad.data["verdict"], "FAIL")
        self.assertEqual(good.data["actual_hex"], bad.data["actual_hex"])
        self.assertTrue(bad.data["evidence_complete"])

    def test_explicit_empty_read_expect_passes(self):
        self.profile["services"][0]["characteristics"][0]["initial_hex"] = ""
        result = self.run_ble("read", options={"expect_hex": ""})
        self.assertTrue(result.success)
        self.assertEqual(result.data["verdict"], "PASS")

    def test_write_requires_per_call_authorization_before_backend(self):
        result = self.run_ble("write", options={"hex": "aa", "write_mode": "with-response"})
        self.assertEqual(result.error_code, "CONFIG_INVALID")
        self.assertEqual(self.backends, [])

    def test_write_submits_once_and_state_does_not_cross_runs(self):
        result = self.run_ble("write", options={"hex": "a5", "write_mode": "with-response", "allow_write": True})
        self.assertTrue(result.success)
        self.assertEqual(result.data["verdict"], "NOT_EVALUATED")
        self.assertEqual(len([c for c in self.backends[0].calls if c[0] == "write"]), 1)
        read = self.run_ble("read")
        self.assertEqual(read.data["actual_hex"], "0100")

    def test_write_limit_blocks_no_chunking_or_retry(self):
        result = self.run_ble("write", options={
            "hex": "aa" * 513, "write_mode": "with-response", "allow_write": True})
        self.assertEqual(result.error_code, "BLE_CAPABILITY_UNSUPPORTED")
        self.assertFalse(any(c[0] == "write" for c in self.backends[0].calls))

    def test_write_without_response_requires_its_own_property(self):
        result = self.run_ble("write", options={
            "hex": "aa", "write_mode": "without-response", "allow_write": True})
        self.assertEqual(result.error_code, "BLE_CAPABILITY_UNSUPPORTED")

    def test_notify_no_assertion_finishes_not_evaluated(self):
        result = self.run_ble("notify", options={"duration": .001})
        self.assertTrue(result.success)
        self.assertEqual(result.data["verdict"], "NOT_EVALUATED")

    def test_notify_absence_cannot_match_empty_expectation(self):
        result = self.run_ble("notify", options={"duration": .001, "expect_hex": ""})
        self.assertEqual(result.error_code, "BLE_ASSERTION_FAILED")
        self.assertEqual(result.data["verdict"], "FAIL")
        self.assertEqual(result.data["event_ids"], [])

    def test_notify_matching_callback_is_receipt_not_write_confirmation(self):
        original = BleSession.wait
        async def emit_then_wait(session, mark, timeout_s, predicate=None, **kwargs):
            session.backend.emit(mark.ref, b"\xa5")
            return await original(session, mark, timeout_s, predicate, **kwargs)
        with patch.object(BleSession, "wait", emit_then_wait):
            result = self.run_ble("notify", options={"duration": .05, "expect_hex": "a5"})
        self.assertTrue(result.success)
        self.assertEqual(result.data["verdict"], "PASS")
        self.assertFalse(any(c[0] == "write" for c in self.backends[0].calls))

    def test_notify_preserves_callback_during_subscribe_before_return(self):
        class Early(FakeBackend):
            async def subscribe(self, ref, callback):
                token = await super().subscribe(ref, callback)
                self.emit(ref, b"\xa5")
                return token
        workflow = BleWorkflow(lambda config, profile: Early(profile))
        result = workflow.execute(self.config, "notify", profile=self.profile,
                                  options={**self.options, "duration": .1, "expect_hex": "a5"})
        self.assertTrue(result.success, result.to_dict())
        self.assertEqual(result.data["verdict"], "PASS")
        self.assertEqual(result.data["notification_counters"]["accepted_total"], 1)

    def emit_overflow(self):
        backend = self.backends[0]
        ref = next(c[1] for c in backend.calls if c[0] == "subscribe")
        backend.emit(ref, b"\x01")
        backend.emit(ref, b"\x02")

    def test_ingress_overflow_marks_evidence_incomplete(self):
        self.config["ble"]["ingress_limits"]["max_events"] = 1
        self.faults["write"] = self.emit_overflow
        result = self.run_ble("test", suite=self.suite, options={"allow_write": True})
        self.assertEqual(result.error_code, "BLE_EVENT_OVERFLOW")
        self.assertFalse(result.data["evidence_complete"])
        self.assertGreaterEqual(result.data["notification_counters"]["rejected_total"], 1)

    def test_cleanup_overflow_cannot_report_overall_success(self):
        self.config["ble"]["ingress_limits"]["max_events"] = 1
        self.faults["unsubscribe"] = self.emit_overflow
        result = self.run_ble("test", suite=self.suite, options={"allow_write": True})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "BLE_EVENT_OVERFLOW")
        self.assertEqual(result.data["verdict"], "PASS")
        self.assertEqual(result.data["cases"][0]["verdict"], "PASS")
        self.assertFalse(result.data["evidence_complete"])
        self.assertEqual(result.data["notification_counters"]["rejected_total"], 1)

    def test_suite_offline_read_write_early_notification_loop(self):
        result = self.run_ble("test", suite=self.suite, options={"allow_write": True})
        self.assertTrue(result.success, result.to_dict())
        self.assertEqual(result.data["verdict"], "PASS")
        self.assertEqual(result.data["cases"][0]["steps"][4]["verdict"], "PASS")
        self.assertEqual([c["action"] for c in result.data["cleanup"]], ["unsubscribe", "disconnect"])

    def test_suite_entire_syntax_checked_before_first_backend(self):
        self.suite["tests"].append({"name": "bad_late_case", "steps": [{"action": "unknown", "characteristic": "echo"}]})
        result = self.run_ble("test", suite=self.suite, options={"allow_write": True})
        self.assertEqual(result.error_code, "CONFIG_INVALID")
        self.assertEqual(self.backends, [])

    def test_suite_property_preflight_fail_blocks_all_writes(self):
        self.suite["characteristics"]["echo"]["required_properties"].append("indicate")
        result = self.run_ble("test", suite=self.suite, options={"allow_write": True})
        self.assertEqual(result.error_code, "BLE_ASSERTION_FAILED")
        self.assertEqual(result.data["verdict"], "FAIL")
        self.assertFalse(any(c[0] in ("read", "write") for c in self.backends[0].calls))

    def test_all_pass_then_cleanup_failure_preserves_case_and_steps(self):
        self.faults["disconnect"] = OSError("controlled cleanup failure")
        result = self.run_ble("test", suite=self.suite, options={"allow_write": True})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "CLEANUP_FAILED")
        self.assertEqual(result.data["verdict"], "PASS")
        self.assertEqual(result.data["cases"][0]["verdict"], "PASS")
        self.assertTrue(all(s["status"] == "COMPLETED" for s in result.data["cases"][0]["steps"]))

    def test_assertion_failure_then_cleanup_failure_keeps_main_error(self):
        self.faults["disconnect"] = OSError("controlled cleanup failure")
        self.suite["tests"][0]["steps"][0]["expect"]["equals_hex"] = "ff"
        result = self.run_ble("test", suite=self.suite, options={"allow_write": True})
        self.assertEqual(result.error_code, "BLE_ASSERTION_FAILED")
        self.assertEqual(result.data["verdict"], "FAIL")
        self.assertTrue(any(not c["success"] for c in result.data["cleanup"]))
        self.assertFalse(any(c[0] == "write" for c in self.backends[0].calls))

    def test_main_operation_error_survives_cleanup_failure(self):
        self.faults.update(read=RuntimeError("controlled read failure"), disconnect=OSError("controlled cleanup failure"))
        result = self.run_ble("read")
        self.assertEqual(result.error_code, "BLE_GATT_FAILED")
        self.assertEqual(result.data["verdict"], "NOT_EVALUATED")
        self.assertTrue(any(not c["success"] for c in result.data["cleanup"]))

    def test_user_cancel_runs_cleanup(self):
        self.faults["read"] = ToolError("CANCELLED", "controlled cancellation")
        result = self.run_ble("read")
        self.assertEqual(result.error_code, "CANCELLED")
        self.assertTrue(any(c[0] == "disconnect" for c in self.backends[0].calls))

    def test_total_timeout_does_not_replace_local_budgets(self):
        self.config["ble"]["operation_timeout_s"] = .3
        self.faults["read"] = lambda: asyncio.sleep(1)
        result = self.run_ble("read", timeout_s=.03)
        self.assertEqual(result.error_code, "WORKFLOW_TIMEOUT")
        self.assertEqual(result.data["error_data"]["stage"], "read")
        self.assertEqual(result.data["error_data"]["local_budget_s"], .3)

    def test_dry_run_never_constructs_backend_and_lists_authorization_issue(self):
        result = self.run_ble("test", suite=self.suite, dry_run=True)
        self.assertTrue(result.success)
        self.assertEqual(self.backends, [])
        self.assertTrue(result.data["dry_run"])
        self.assertEqual(result.data["verdict"], "NOT_EVALUATED")
        self.assertTrue(result.data["blocking_issues"])

    def test_dry_run_invalid_suite_is_a_blocking_plan_not_fake_pass(self):
        result = self.run_ble("test", suite={"version": 1}, dry_run=True)
        self.assertTrue(result.success)
        self.assertEqual(result.data["verdict"], "NOT_EVALUATED")
        self.assertEqual(result.data["blocking_issues"][0]["error_code"], "CONFIG_INVALID")
        self.assertEqual(self.backends, [])

    def test_log_failure_blocks_assertion_pass_and_keeps_cleanup(self):
        from embedded_auto.ble.evidence import BleEvidence
        original = BleEvidence.emit
        def emit(evidence, kind, **fields):
            if kind == "read_result":
                raise ToolError("LOG_WRITE_FAILED", "controlled writer failure")
            return original(evidence, kind, **fields)
        with patch.object(BleEvidence, "emit", emit):
            result = self.run_ble("read", options={"expect_hex": "01 00"})
        self.assertEqual(result.error_code, "LOG_WRITE_FAILED")
        self.assertFalse(result.data["evidence_complete"])
        self.assertNotEqual(result.data["verdict"], "PASS")
        self.assertTrue(any(c[0] == "disconnect" for c in self.backends[0].calls))

    def test_archive_failure_is_returned_with_run_identity(self):
        from embedded_auto.application import ble_workflow
        original = ble_workflow.write_json
        def write(path, value):
            if path.name == "ble_assertions.json":
                raise OSError("controlled archive failure")
            return original(path, value)
        with patch.object(ble_workflow, "write_json", write):
            result = self.run_ble("read")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "LOG_WRITE_FAILED")
        self.assertTrue(result.run_id)
        self.assertFalse(result.data["evidence_complete"])

    def test_report_paths_and_archived_config_without_mcu_or_git_snapshot(self):
        result = self.run_ble("test", suite=self.suite, options={"allow_write": True})
        config_path = self.root / "app.yaml"
        config_path.write_text(yaml.safe_dump(self.config), encoding="utf-8")
        self.assertEqual(report(config_path, result.run_id), result.to_dict())
        path = Path(result.data["run_path"])
        for name in ("run_config.yaml", "run_summary.json", "events.jsonl", "agent_summary.md",
                     "ble_events.jsonl", "ble_assertions.json", "ble_profile.json", "ble_suite.json"):
            self.assertTrue((path / name).is_file(), name)
        self.assertNotIn("target", load_yaml(path / "run_config.yaml"))
        self.assertFalse((path / "git_state.json").exists())
        self.assertFalse((path / "serial.bin").exists())
        lines = [json.loads(line) for line in (path / "ble_events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["event_id"] for e in lines], sorted({e["event_id"] for e in lines}))
        self.assertTrue(any(e["event_type"] == "notification" and e["payload_hex"] == "a55a" for e in lines))

    def test_real_backend_dependency_version_mismatch_fails_before_import_or_scan(self):
        self.config["ble"].update(backend="bleak", device="AA:BB:CC:DD:EE:FF")
        with patch("importlib.metadata.version", return_value="2.0.0"), patch(
                "embedded_auto.adapters.ble.bleak_backend.BleakBackend",
                side_effect=AssertionError("must not construct real backend")):
            result = BleWorkflow().execute(self.config, "inspect")
        self.assertEqual(result.error_code, "BLE_DEPENDENCY_MISSING")
        self.assertEqual(result.data["error_data"]["installed_version"], "2.0.0")
        self.assertFalse(result.data["simulated"])

    def test_real_write_confirmation_mismatch_blocks_before_backend(self):
        self.config["ble"].update(backend="bleak", device="AA:BB:CC:DD:EE:FF")
        result = self.run_ble("write", options={
            "hex": "aa", "write_mode": "with-response", "allow_write": True,
            "confirm_device": "AA:BB:CC:DD:EE:00"})
        self.assertEqual(result.error_code, "CONFIG_INVALID")
        self.assertEqual(self.backends, [])

    def test_cli_json_route_requires_no_target_and_report_reuses_same_root(self):
        path = self.root / "app.yaml"
        path.write_text(yaml.safe_dump(self.config), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = main(["ble", "read", "--config", str(path), "--service", SERVICE,
                         "--characteristic", CHAR, "--expect-hex", "0100", "--json"])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["data"]["verdict"], "PASS")
        self.assertEqual(report(path, result["run_id"]), result)

    def test_cli_syntax_error_exit_two(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            parser().parse_args(["ble", "write", "--service", SERVICE, "--characteristic", CHAR, "--hex", "aa"])
        self.assertEqual(raised.exception.code, 2)

    def test_cli_text_does_not_label_plain_read_pass(self):
        path = self.root / "app.yaml"
        path.write_text(yaml.safe_dump(self.config), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = main(["ble", "read", "--config", str(path), "--service", SERVICE, "--characteristic", CHAR])
        self.assertEqual(code, 0)
        self.assertIn("NOT_EVALUATED", output.getvalue())
        self.assertIn("模拟", output.getvalue())
        self.assertFalse(output.getvalue().startswith("PASS"))

    def test_report_default_workspace_matches_execution_and_text_retains_verdict(self):
        raw = {"version": 1, "app": {}, "ble": {
            "backend": "fake", "device": "fake:demo", "fake_profile": self.config["ble"]["fake_profile"]}}
        path = self.root / "configs/ble/demo.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        config = prepare_config(raw, path)
        result = BleWorkflow(self.backend).execute(config, "read", options=self.options, profile=self.profile)
        self.assertTrue(result.success)
        self.assertEqual(report(path, result.run_id), result.to_dict())
        self.assertIn("NOT_EVALUATED", (Path(result.data["run_path"]) / "agent_summary.md").read_text(encoding="utf-8"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["report", "--config", str(path), "--run-id", result.run_id])
        self.assertEqual(code, 0)
        self.assertIn("NOT_EVALUATED", output.getvalue())
        self.assertIn("模拟", output.getvalue())
        self.assertFalse(output.getvalue().startswith("PASS"))

    def test_fake_import_and_run_with_bleak_import_forbidden(self):
        path = self.root / "app.yaml"
        path.write_text(yaml.safe_dump(self.config), encoding="utf-8")
        code = (
            "import sys,importlib.abc; "
            "exec('class Block(importlib.abc.MetaPathFinder):\\n"
            " def find_spec(self,fullname,path=None,target=None):\\n"
            "  if fullname == \"bleak\" or fullname.startswith(\"bleak.\"): raise AssertionError(\"Bleak imported\")'); "
            "sys.meta_path.insert(0,Block()); from embedded_auto.cli import main; "
            "raise SystemExit(main(sys.argv[1:]))"
        )
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        proc = subprocess.run([sys.executable, "-c", code, "ble", "scan", "--config", str(path), "--json"],
                              capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["data"]["simulated"])

    def test_ble_domain_and_core_import_boundaries(self):
        forbidden = ("serial", "firmware", "services", "application", "adapters")
        for path in (ROOT / "src/embedded_auto/ble").glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                elif isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                for name in names:
                    self.assertFalse(any(name == f"embedded_auto.{layer}" or
                                         name.startswith(f"embedded_auto.{layer}.") for layer in forbidden),
                                     (path, name))
        code = (
            "import sys; import embedded_auto.core.config, embedded_auto.core.run_context; "
            "import embedded_auto.ble.models, embedded_auto.ble.interfaces, embedded_auto.ble.config; "
            "import embedded_auto.ble.evidence, embedded_auto.ble.session, embedded_auto.ble.test_runner; "
            "bad=[n for n in sys.modules if n=='bleak' or n.startswith('bleak.') or "
            "any(n=='embedded_auto.'+p or n.startswith('embedded_auto.'+p+'.') "
            "for p in ('serial','firmware','services','application','adapters'))]; "
            "assert not bad,bad"
        )
        # -I 忽略 PYTHONPATH，显式仅插入当前源码目录，不借助已安装包掩盖缺失模块。
        code = f"import sys;sys.path.insert(0,{str(ROOT / 'src')!r});" + code
        proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
