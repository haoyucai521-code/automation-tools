"""显式授权与无硬件擦除阻断。"""
import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from embedded_auto.core.errors import ToolError
from embedded_auto.core.run_context import RunContext
from embedded_auto.firmware.build_service import BuildService
from embedded_auto.firmware.erase_policy import validate_erase_policy
from embedded_auto.firmware.flash_service import FlashService
from test_build_flash import FakeProgrammer, FakeReset, FakeSession
from test_workflow_cli import MemorySerial, config_for
from embedded_auto.application.composition import build_backend
from embedded_auto.application.workflow_engine import WorkflowEngine


class ErasePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = config_for(self.root)
        self.config["flash"].update(erase_policy="mass_erase", physical_flash_start=0x08000000,
                                    physical_flash_size=2048, physical_flash_source="fake only")
        self.artifact = {"sha256": hashlib.sha256(b"firmware").hexdigest()}
        self.auth = {"target_id": "fake", "exact_part_number": "FAKE001", "sha256": self.artifact["sha256"],
                     "address": 0x08000000, "erase_policy": "mass_erase",
                     "erase_start": 0x08000000, "erase_size": 2048}

    def test_exact_binding_and_conflicting_or_unsupported_policy(self):
        self.assertEqual(validate_erase_policy(self.config, self.artifact, self.auth), self.auth)
        for key in self.auth:
            bad = dict(self.auth, **{key: "different"})
            with self.subTest(key=key), self.assertRaises(ToolError) as caught:
                validate_erase_policy(self.config, self.artifact, bad)
            self.assertEqual(caught.exception.code, "FLASH_DESTRUCTIVE_AUTH_REQUIRED")
        for key in ("address", "erase_start", "erase_size"):
            with self.subTest(float_field=key), self.assertRaises(ToolError) as caught:
                validate_erase_policy(self.config, self.artifact, dict(self.auth, **{key: float(self.auth[key])}))
            self.assertEqual(caught.exception.code, "FLASH_DESTRUCTIVE_AUTH_REQUIRED")
        for policy in (None, "scoped_erase", "none"):
            config = copy.deepcopy(self.config)
            config["flash"]["erase_policy"] = policy
            with self.subTest(policy=policy), self.assertRaises(ToolError) as caught:
                validate_erase_policy(config, self.artifact, self.auth)
            self.assertEqual(caught.exception.code, "FLASH_ERASE_SCOPE_UNVERIFIED")
        self.config["flash"]["preserve_regions"] = [{"start": 0x08000700, "size": 4}]
        with self.assertRaises(ToolError) as caught:
            validate_erase_policy(self.config, self.artifact, self.auth)
        self.assertEqual(caught.exception.code, "FLASH_REGION_CONFLICT")

    def test_no_authorization_stops_before_reset_and_serial(self):
        ctx = RunContext(self.config, "build")
        build = BuildService(backend_factory=build_backend).build(self.config, ctx)
        self.assertTrue(build.success, build.to_dict())
        image = Path(build.data["artifact"])
        reset = FakeReset()
        programmer = FakeProgrammer("print('CONNECTED WRITTEN VERIFIED')", "print('CONNECTED ERASED')")
        result = FlashService(programmer).flash(self.config, ctx, FakeSession(), reset, image, build.data["manifest"])
        self.assertEqual(result.error_code, "FLASH_DESTRUCTIVE_AUTH_REQUIRED")
        self.assertEqual(reset.actions, [])
        engine = WorkflowEngine(session_factory=lambda config: self.fail("opened serial"))
        result = engine.execute(self.config, "flash", artifact=image)
        self.assertEqual(result.error_code, "FLASH_DESTRUCTIVE_AUTH_REQUIRED")
        self.assertEqual(result.data["diagnostics"]["phase"], "preflight")
        self.assertEqual(result.data["diagnostics"]["destructive_effect"], "NOT_STARTED")
        self.assertEqual(result.data["diagnostics"]["confirmed_actions"], [])
        self.assertTrue(result.data["diagnostics"]["manual_review"])
        self.assertEqual(result.data["diagnostics"]["tool_version_status"], "NOT_VERIFIED_THIS_RUN")
        plan = engine.execute(self.config, "flash", artifact=image, dry_run=True)
        self.assertFalse(plan.data["ready_for_execution"])
        self.assertTrue(plan.data["diagnostics"]["plan_only"])
        self.assertTrue(any("FLASH_DESTRUCTIVE_AUTH_REQUIRED" in s for s in plan.data["blocking_issues"]))

    def test_write_marker_and_verify_failure_keep_erasure_evidence(self):
        ctx = RunContext(self.config, "build")
        build = BuildService(backend_factory=build_backend).build(self.config, ctx)
        self.assertTrue(build.success, build.to_dict())
        programmer = FakeProgrammer("print('CONNECTED\\nWRITTEN')")
        result = FlashService(programmer).flash(self.config, ctx, FakeSession(), FakeReset(),
                                                Path(build.data["artifact"]), build.data["manifest"],
                                                authorization=self.auth)
        self.assertEqual(result.error_code, "FLASH_VERIFY_FAILED")
        self.assertIn("mass_erase_succeeded", result.data["confirmed_actions"])
        self.assertIn("write_success_pattern_observed", result.data["confirmed_actions"])
        self.assertTrue(result.data["device_may_be_unbootable"])

    def test_write_uses_bound_bytes_even_if_original_snapshot_changes_during_erase(self):
        ctx = RunContext(self.config, "build")
        build = BuildService(backend_factory=build_backend).build(self.config, ctx)
        self.assertTrue(build.success, build.to_dict())
        image = Path(build.data["artifact"])

        class ReadingProgrammer(FakeProgrammer):
            def command(self, config, image=None):
                return [*super().command(config, image), str(image)]

        # 由单独子进程实际打开传给烧录器的路径。
        programmer = ReadingProgrammer("import sys\nfrom pathlib import Path\n"
                                       "assert Path(sys.argv[1]).read_bytes() == b'firmware'\n"
                                       "print('CONNECTED WRITTEN VERIFIED')")
        service = FlashService(programmer)
        strategy_run = service.strategy.run

        def change_source_during_erase(command, cwd, timeout, ctx, log_path, *args):
            if log_path.name == "erase.log":
                image.write_bytes(b"unauthorized replacement")
                if os.name == "nt":
                    flash_input = programmer.images[0]
                    replacement = flash_input.with_name("replacement.bin")
                    replacement.write_bytes(b"unauthorized replacement")
                    with self.assertRaises(OSError):
                        replacement.replace(flash_input)
                    with self.assertRaises(OSError):
                        flash_input.write_bytes(b"unauthorized replacement")
            return strategy_run(command, cwd, timeout, ctx, log_path, *args)

        with patch.object(service.strategy, "run", side_effect=change_source_during_erase):
            result = service.flash(self.config, ctx, FakeSession(), FakeReset(), image,
                                   build.data["manifest"], authorization=self.auth)
        self.assertTrue(result.success, result.to_dict())
        self.assertNotEqual(programmer.images[0], image)
        self.assertEqual(image.read_bytes(), b"unauthorized replacement")
        self.assertEqual(programmer.images[0].read_bytes(), b"firmware")
        programmer.images[0].write_bytes(b"after cleanup")  # 操作结束后句柄应已释放。

    def test_corrupted_flash_input_is_rejected_before_erase(self):
        ctx = RunContext(self.config, "build")
        build = BuildService(backend_factory=build_backend).build(self.config, ctx)
        image = Path(build.data["artifact"])
        programmer = FakeProgrammer("unexpected execution")
        service = FlashService(programmer)
        reset = FakeReset()
        with patch("embedded_auto.firmware.flash_service.shutil.copyfileobj",
                   side_effect=lambda source, destination: destination.write(b"bad firmware")):
            result = service.flash(self.config, ctx, FakeSession(), reset, image,
                                   build.data["manifest"], authorization=self.auth)
        self.assertEqual(result.error_code, "ARTIFACT_HASH_MISMATCH")
        self.assertEqual(result.data["destructive_effect"], "NOT_STARTED")
        self.assertEqual(reset.actions, [])
        self.assertEqual(programmer.images, [])

    def test_keyboard_interrupt_after_erase_preserves_confirmed_effect(self):
        ctx = RunContext(self.config, "build")
        build = BuildService(backend_factory=build_backend).build(self.config, ctx)
        service = FlashService(FakeProgrammer("raise AssertionError('write must not run')"))
        engine = WorkflowEngine(flasher=service, session_factory=MemorySerial)
        event = RunContext.event

        def interrupt(ctx, state, **data):
            event(ctx, state, **data)
            if state == "FLASH_ERASE_SUCCEEDED":
                raise KeyboardInterrupt

        erased = SimpleNamespace(output="CONNECTED ERASED", returncode=0, duration_ms=0)
        with patch.object(RunContext, "event", interrupt), patch.object(service.strategy, "run", return_value=erased) as run:
            result = engine.execute(self.config, "flash", artifact=build.data["artifact"], authorization=self.auth)
        run.assert_called_once()
        self.assertEqual(result.error_code, "CANCELLED")
        diagnostics = result.data["diagnostics"]
        self.assertEqual(diagnostics["phase"], "erase")
        self.assertEqual(diagnostics["destructive_effect"], "ERASE_CONFIRMED")
        self.assertEqual(diagnostics["confirmed_actions"], ["mass_erase_succeeded"])
        self.assertIn("写入阶段尚未进入", diagnostics["manual_review"])
        path = Path(result.data["run_path"])
        flash_result = json.loads((path / "flash_result.json").read_text(encoding="utf-8"))
        self.assertTrue(flash_result["data"]["device_may_be_unbootable"])
        (path / "flash_input.bin").open("ab").close()  # 中断路径也应释放保护句柄。

    def test_device_check_blocks_invalid_static_scope_without_requiring_authorization(self):
        with patch("embedded_auto.application.composition.serial_ports", return_value=[{"device": "FAKE"}]):
            ready = WorkflowEngine.inspect(self.config)
            self.assertTrue(ready.success, ready.to_dict())
            self.config["flash"].pop("erase_policy")
            blocked = WorkflowEngine.inspect(self.config)
        self.assertFalse(blocked.success)
        for operation in ("flash", "run"):
            issues, = blocked.data["validation_issues"][operation]
            self.assertIn("FLASH_ERASE_SCOPE_UNVERIFIED", issues)
        self.assertEqual(blocked.data["validation_issues"]["connect"], [])

    def test_each_bad_binding_stops_before_serial(self):
        ctx = RunContext(self.config, "build")
        build = BuildService(backend_factory=build_backend).build(self.config, ctx)
        image = Path(build.data["artifact"])
        engine = WorkflowEngine(session_factory=lambda config: self.fail("opened serial"))
        for key in self.auth:
            with self.subTest(key=key):
                bad = dict(self.auth, **{key: "different"})
                result = engine.execute(self.config, "flash", artifact=image, authorization=bad)
                self.assertEqual(result.error_code, "FLASH_DESTRUCTIVE_AUTH_REQUIRED")

    def test_missing_tool_or_excessive_release_delay_stops_before_serial(self):
        ctx = RunContext(self.config, "build")
        build = BuildService(backend_factory=build_backend).build(self.config, ctx)
        image = Path(build.data["artifact"])
        engine = WorkflowEngine(session_factory=lambda config: self.fail("opened serial"))
        self.config["flash"]["executable"] = str(self.root / "missing.exe")
        result = engine.execute(self.config, "flash", artifact=image, authorization=self.auth)
        self.assertEqual(result.error_code, "FLASH_TOOL_NOT_FOUND")
        self.config["flash"]["executable"] = __import__("sys").executable
        self.config["reset"]["release_delay_ms"] = 3000
        result = engine.execute(self.config, "flash", artifact=image, authorization=self.auth)
        self.assertEqual(result.error_code, "FLASH_CONFIG_INVALID")

    def test_missing_or_mismatched_cli_version_evidence_blocks_before_serial(self):
        ctx = RunContext(self.config, "build")
        build = BuildService(backend_factory=build_backend).build(self.config, ctx)
        image = Path(build.data["artifact"])
        engine = WorkflowEngine(session_factory=lambda config: self.fail("opened serial"))
        for key, value in (("cli_version_source", ""), ("required_cli_version", "different")):
            config = copy.deepcopy(self.config)
            config["flash"][key] = value
            with self.subTest(key=key):
                result = engine.execute(config, "flash", artifact=image, authorization=self.auth)
                self.assertEqual(result.error_code, "CONFIG_INVALID")
                self.assertEqual(result.data["diagnostics"]["destructive_effect"], "NOT_STARTED")


if __name__ == "__main__":
    unittest.main()
