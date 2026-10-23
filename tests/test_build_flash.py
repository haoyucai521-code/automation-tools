"""No compiler, programmer, serial port or hardware is required by these tests."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from embedded_auto.adapters.build import GCCBackend, IARBackend
from embedded_auto.adapters.programmer import CubeProgrammer
from embedded_auto.core.errors import ToolError
from embedded_auto.core.process import ProcessRunner
from embedded_auto.services.build_service import BuildService, validate_artifact
from embedded_auto.services.flash_service import FlashService


class FakeContext:
    def __init__(self, path, config):
        self.path = Path(path)
        self.path.mkdir()
        self.run_id = self.path.name
        self.config = config
        self.events = []
        self.sleeps = []
        self.cancelled = threading.Event()
        self.deadline = time.monotonic() + 30

    @property
    def remaining(self):
        return max(0, self.deadline - time.monotonic())

    def check(self):
        if self.cancelled.is_set():
            raise ToolError("CANCELLED", "Cancelled")
        if not self.remaining:
            raise ToolError("WORKFLOW_TIMEOUT", "Timed out")

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check()
            time.sleep(min(.005, max(0, end - time.monotonic())))
        self.check()

    def event(self, state, **data):
        self.events.append((state, data))


class FakeSession:
    def check(self):
        pass


class FakeReset:
    def __init__(self, fail_assert=False, fail_release=False, fail_first_release=False):
        self.actions = []
        self.fail_assert = fail_assert
        self.fail_release = fail_release
        self.fail_first_release = fail_first_release

    def assert_reset(self):
        self.actions.append("assert")
        if self.fail_assert:
            raise OSError("assert failed")

    def release_reset(self):
        self.actions.append("release")
        if self.fail_release or (self.fail_first_release and self.actions.count("release") == 1):
            raise OSError("release failed")


class FakeProgrammer:
    def __init__(self, code, erase_code="print('CONNECTED\\nERASED')"):
        self.code = code
        self.erase_code = erase_code
        self.images = []

    def erase_command(self, config):
        return [sys.executable, "-u", "-c", self.erase_code]

    def command(self, config, image=None):
        self.images.append(image)
        return [sys.executable, "-u", "-c", self.code]


def process_alive(pid):
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_uint32()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class BuildFlashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="embedded fake ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {
            "target": {"id": "demo", "exact_part_number": "FAKE-MCU", "status": "verified"},
            "build": {"backend": "gcc", "cwd": str(self.root), "command": [],
                      "artifact": str(self.root / "user firmware.bin"), "timeout_s": 5},
            "flash": {"executable": "cube-not-installed", "interface": "SWD", "mode": "NORMAL",
                      "reset_mode": "SWrst", "address": 0x08000000,
                      "region_start": 0x08000000, "region_size": 1024,
                      "address_source": "fake linker script", "verify": True, "timeout_s": 5,
                      "connect_success_pattern": r"^CONNECTED$", "write_success_pattern": r"^WRITTEN$",
                      "erase_success_pattern": r"^ERASED$",
                      "verify_success_pattern": r"^VERIFIED$", "cli_verified": True,
                      "compatibility_verified": True},
            "reset": {"asserted_rts_value": False, "release_delay_ms": 10, "pulse_ms": 100},
        }
        self.counter = 0

    def context(self):
        self.counter += 1
        return FakeContext(self.root / f"run-{self.counter}", self.config)

    def build(self, code=None):
        if code is None:
            code = f"from pathlib import Path; Path({self.config['build']['artifact']!r}).write_bytes(b'new firmware')"
        self.config["build"]["command"] = [sys.executable, "-u", "-c", code]
        ctx = self.context()
        result = BuildService().build(self.config, ctx)
        return result, ctx

    def artifact(self):
        result, ctx = self.build()
        self.assertTrue(result.success, result.to_dict())
        return Path(result.data["artifact"]), result.data["manifest"], ctx

    def flash(self, code="print('CONNECTED\\nWRITTEN\\nVERIFIED')", reset=None, connect_only=False,
              erase_code="print('CONNECTED\\nERASED')"):
        if connect_only:
            image, manifest, ctx = None, None, self.context()
        else:
            image, manifest, ctx = self.artifact()
        programmer = FakeProgrammer(code, erase_code)
        reset = reset if reset is not None else FakeReset()
        result = FlashService(programmer).flash(self.config, ctx, FakeSession(), reset,
                                                image, manifest, connect_only=connect_only)
        return result, ctx, reset, programmer

    def test_backend_commands_and_official_cube_arguments(self):
        self.config["build"]["command"] = ["cmake", "--build", "a path"]
        self.assertEqual(GCCBackend().command(self.config), ["cmake", "--build", "a path"])
        self.config["build"].update(executable="iarbuild.exe", project_file=str(self.root / "x.ewp"), configuration="Release")
        self.assertEqual(IARBackend().command(self.config), ["iarbuild.exe", str(self.root / "x.ewp"), "-build", "Release"])
        expected = ["cube-not-installed", "-c", "port=SWD", "mode=NORMAL", "reset=SWrst"]
        self.assertEqual(CubeProgrammer().command(self.config), expected)
        self.assertEqual(CubeProgrammer().erase_command(self.config), expected + ["-e", "all"])
        self.assertEqual(CubeProgrammer().command(self.config, self.root / "a.bin"),
                         expected + ["-w", str(self.root / "a.bin"), "0x08000000", "-v"])

    def test_build_snapshot_and_manifest(self):
        result, ctx = self.build()
        self.assertTrue(result.success, result.to_dict())
        image = Path(result.data["artifact"])
        self.assertEqual(image, ctx.path / "firmware.bin")
        self.assertEqual(result.data["image"], str(image))
        manifest = json.loads((ctx.path / "artifact_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(result.data["manifest"], manifest)
        self.assertEqual(manifest["sha256"], hashlib.sha256(image.read_bytes()).hexdigest())
        self.assertTrue(manifest["build_success"])
        self.assertIn("mtime_ns", result.data["artifact_after"])
        self.assertEqual(validate_artifact(image, manifest, self.config)["run_id"], ctx.run_id)
        archived = json.loads((ctx.path / "build_result.json").read_text(encoding="utf-8"))
        self.assertTrue(archived["success"])
        self.assertEqual(archived["run_id"], ctx.run_id)

    def test_build_identity_does_not_require_hardware_status_verified(self):
        self.config["target"]["status"] = "unverified"
        result, _ = self.build()
        self.assertTrue(result.success, result.to_dict())
        self.config["target"]["exact_part_number"] = "TODO_MEASURE"
        result, _ = self.build()
        self.assertEqual(result.error_code, "ARTIFACT_TARGET_MISMATCH")

    def test_fake_iar_builds_release_without_real_iar(self):
        project = self.root / "fake project.py"
        project.write_text("import sys\nfrom pathlib import Path\n"
                           "assert sys.argv[1:] == ['-build', 'Release']\n"
                           f"Path({self.config['build']['artifact']!r}).write_bytes(b'iar firmware')\n", encoding="utf-8")
        self.config["build"].update(backend="iar", executable=sys.executable,
                                    project_file=str(project), configuration="Release")
        result = BuildService().build(self.config, self.context())
        self.assertTrue(result.success, result.to_dict())

    def test_old_artifact_is_not_deleted_on_failed_build(self):
        original = Path(self.config["build"]["artifact"])
        original.write_bytes(b"user old firmware")
        result, ctx = self.build("import sys; print('compile failed'); sys.exit(7)")
        self.assertEqual(result.error_code, "BUILD_FAILED")
        self.assertEqual(original.read_bytes(), b"user old firmware")
        self.assertFalse((ctx.path / "artifact_manifest.json").exists())
        self.assertIn("compile failed", (ctx.path / "build.log").read_text())
        self.assertFalse(json.loads((ctx.path / "build_result.json").read_text())["success"])

    def test_build_timeout_is_archived(self):
        self.config["build"]["timeout_s"] = .2
        result, ctx = self.build("import time; time.sleep(20)")
        self.assertEqual(result.error_code, "BUILD_TIMEOUT")
        self.assertFalse(process_alive(result.data["pid"]))
        self.assertEqual(json.loads((ctx.path / "build_result.json").read_text())["error_code"], "BUILD_TIMEOUT")

    def test_unchanged_legacy_output_is_stale(self):
        original = Path(self.config["build"]["artifact"])
        original.write_bytes(b"old firmware")
        result, _ = self.build("print('build says success but did nothing')")
        self.assertEqual(result.error_code, "ARTIFACT_STALE")
        self.assertEqual(original.read_bytes(), b"old firmware")

    def test_new_same_bytes_with_fresh_mtime_are_accepted(self):
        original = Path(self.config["build"]["artifact"])
        original.write_bytes(b"new firmware")
        result, _ = self.build()
        self.assertTrue(result.success, result.to_dict())

    def test_missing_empty_and_tool_missing_builds(self):
        for code, expected in [("pass", "ARTIFACT_MISSING"),
                               (f"open({self.config['build']['artifact']!r}, 'wb').close()", "ARTIFACT_EMPTY")]:
            with self.subTest(expected=expected):
                result, _ = self.build(code)
                self.assertEqual(result.error_code, expected)
        self.config["build"]["command"] = [str(self.root / "no compiler.exe")]
        result = BuildService().build(self.config, self.context())
        self.assertEqual(result.error_code, "BUILD_TOOL_NOT_FOUND")
        self.assertIn("log_path", result.data)

    def test_run_directory_collision_never_overwrites_snapshot(self):
        image, _, ctx = self.artifact()
        image.write_bytes(b"keep this user data")
        result = BuildService().build(self.config, ctx)
        self.assertEqual(result.error_code, "ARTIFACT_RUN_CONFLICT")
        self.assertEqual(image.read_bytes(), b"keep this user data")

    def test_manifest_identity_metadata_and_region_rejection(self):
        image, manifest, _ = self.artifact()
        for key, value in [("target_id", "wrong"), ("exact_part_number", "wrong"),
                           ("build_success", False), ("address", 0), ("address_source", ""),
                           ("sha256", "0" * 64), ("size", 999), ("run_id", ""),
                           ("command", []), ("source_path", "relative.bin"),
                           ("snapshot_path", str(self.root / "other.bin"))]:
            with self.subTest(key=key):
                changed = dict(manifest, **{key: value})
                with self.assertRaises(ToolError):
                    validate_artifact(image, changed, self.config)
        for value in (None, {}, {"build_success": True}):
            with self.assertRaises(ToolError):
                validate_artifact(image, value, self.config)
        self.config["flash"]["region_size"] = 2
        with self.assertRaisesRegex(ToolError, "outside"):
            validate_artifact(image, manifest, self.config)
        self.config["flash"]["region_size"] = manifest["size"]
        validate_artifact(image, manifest, self.config)
        self.config["flash"]["region_start"] += 1
        with self.assertRaises(ToolError):
            validate_artifact(image, manifest, self.config)

    def test_modified_empty_and_non_bin_snapshots_rejected(self):
        image, manifest, _ = self.artifact()
        image.write_bytes(b"tampered")
        with self.assertRaises(ToolError) as error:
            validate_artifact(image, manifest, self.config)
        self.assertEqual(error.exception.code, "ARTIFACT_HASH_MISMATCH")
        image.write_bytes(b"")
        with self.assertRaises(ToolError):
            validate_artifact(image, manifest, self.config)
        non_bin = image.with_suffix(".hex")
        non_bin.write_bytes(b"firmware")
        with self.assertRaises(ToolError):
            validate_artifact(non_bin, dict(manifest, snapshot_path=str(non_bin)), self.config)

    def test_flash_success_events_logs_and_one_attempt(self):
        result, ctx, reset, programmer = self.flash()
        self.assertTrue(result.success, result.to_dict())
        self.assertEqual(reset.actions, ["assert", "release", "release"] * 2)
        self.assertEqual(ctx.sleeps, [.01, .01])
        states = [state for state, _ in ctx.events]
        expected = ["ASSERT_RESET", "START_PROGRAMMER", "RELEASE_RESET_AT_CONFIGURED_TIME", "WAIT_FLASH_AND_VERIFY"]
        self.assertEqual([state for state in states if state in expected], expected * 2)
        self.assertLess(states.index("FLASH_ERASE_SUCCEEDED"), states.index("WRITE_FLASH"))
        self.assertTrue(result.data["erase"]["success"])
        self.assertEqual(result.data["erase"]["scope"], "all")
        self.assertIn("ERASED", (ctx.path / "erase.log").read_text())
        self.assertEqual(len(programmer.images), 1)
        self.assertEqual(len(result.data["attempts"]), 1)
        self.assertIn("VERIFIED", (ctx.path / "flash.log").read_text())
        self.assertIn("sha256", result.data)
        self.assertTrue(json.loads((ctx.path / "flash_result.json").read_text())["success"])

    def test_erase_failure_or_missing_evidence_prevents_write(self):
        marker = self.root / "write_started"
        write_code = f"from pathlib import Path; Path({str(marker)!r}).touch(); print('CONNECTED\\nWRITTEN\\nVERIFIED')"
        for erase_code in ("print('CONNECTED')",
                           "import sys; print('CONNECTED\\nERASED'); sys.exit(7)"):
            with self.subTest(erase_code=erase_code):
                result, ctx, reset, _ = self.flash(write_code, erase_code=erase_code)
                self.assertEqual(result.error_code, "FLASH_ERASE_FAILED")
                self.assertFalse(marker.exists())
                self.assertFalse(result.data["erase"]["success"])
                self.assertEqual(reset.actions, ["assert", "release", "release"])
                self.assertNotIn("WRITE_FLASH", [state for state, _ in ctx.events])
                self.assertFalse(json.loads((ctx.path / "flash_result.json").read_text())["success"])

    def test_erase_connect_failure_prevents_write(self):
        result, ctx, _, _ = self.flash(erase_code="print('no target')")
        self.assertEqual(result.error_code, "CONNECT_FAILED")
        self.assertNotIn("WRITE_FLASH", [state for state, _ in ctx.events])

    def test_invalid_erase_pattern_prevents_all_hardware_operations(self):
        for value in ("", "["):
            with self.subTest(value=value):
                self.config["flash"]["erase_success_pattern"] = value
                result, _, reset, _ = self.flash()
                self.assertEqual(result.error_code, "FLASH_CONFIG_INVALID")
                self.assertFalse(reset.actions)

    def test_cancellation_after_erase_prevents_write(self):
        image, manifest, ctx = self.artifact()
        class CancelAfterErase(ProcessRunner):
            def run(self, *args, **kwargs):
                result = super().run(*args, **kwargs)
                ctx.cancelled.set()
                return result
        marker = self.root / "write_started"
        programmer = FakeProgrammer(f"from pathlib import Path; Path({str(marker)!r}).touch()")
        result = FlashService(programmer, CancelAfterErase()).flash(
            self.config, ctx, FakeSession(), FakeReset(), image, manifest)
        self.assertEqual(result.error_code, "CANCELLED")
        self.assertTrue(result.data["erase"]["success"])
        self.assertFalse(marker.exists())

    def test_nonbase_address_is_rejected_before_erase(self):
        image, manifest, ctx = self.artifact()
        self.config["flash"].update(address=0x08002000, region_size=0x10000)
        manifest["address"] = 0x08002000
        reset = FakeReset()
        result = FlashService().flash(self.config, ctx, FakeSession(), reset, image, manifest)
        self.assertEqual(result.error_code, "FLASH_CONFIG_INVALID")
        self.assertFalse(reset.actions)

    def test_connect_only_skips_artifact_and_unverified_write_gate(self):
        self.config["flash"].update(cli_verified=False, compatibility_verified=False, verify=False)
        self.config["flash"].pop("write_success_pattern")
        self.config["flash"].pop("verify_success_pattern")
        self.config["flash"].pop("erase_success_pattern")
        result, _, _, programmer = self.flash("print('CONNECTED')", connect_only=True)
        self.assertTrue(result.success, result.to_dict())
        self.assertEqual(programmer.images, [None])
        self.assertFalse(result.data["cli_verified"])
        self.assertFalse(result.data["compatibility_verified"])

    def test_crlf_patterns_match_without_changing_archived_bytes(self):
        code = "import sys; sys.stdout.buffer.write(b'CONNECTED\\r\\nWRITTEN\\r\\nVERIFIED\\r\\n')"
        result, ctx, _, _ = self.flash(code)
        self.assertTrue(result.success, result.to_dict())
        self.assertTrue(all(result.data["matched"].values()))
        self.assertIn(b"CONNECTED\r\nWRITTEN\r\nVERIFIED\r\n", (ctx.path / "flash.log").read_bytes())

    def test_no_manifest_or_unverified_full_flash_never_touches_reset(self):
        image, manifest, ctx = self.artifact()
        reset = FakeReset()
        service = FlashService(FakeProgrammer("raise RuntimeError('must not launch')"))
        result = service.flash(self.config, ctx, FakeSession(), reset, image, None)
        self.assertEqual(result.error_code, "ARTIFACT_MANIFEST_MISSING")
        self.assertEqual(reset.actions, [])
        self.config["flash"]["compatibility_verified"] = True
        self.config["target"]["status"] = "unverified"
        result = service.flash(self.config, ctx, FakeSession(), reset, image, manifest)
        self.assertEqual(result.error_code, "FLASH_UNVERIFIED")
        self.assertEqual(reset.actions, [])
        self.config["target"]["status"] = "verified"
        self.config["flash"]["compatibility_verified"] = False
        result = service.flash(self.config, ctx, FakeSession(), reset, image, manifest)
        self.assertEqual(result.error_code, "FLASH_UNVERIFIED")
        self.assertEqual(reset.actions, [])

    def test_success_requires_exit_zero_and_all_positive_patterns(self):
        cases = [("print('')", "CONNECT_FAILED"),
                 ("print('CONNECTED')", "FLASH_FAILED"),
                 ("print('CONNECTED\\nWRITTEN')", "FLASH_VERIFY_FAILED"),
                 ("import sys; print('CONNECTED\\nWRITTEN\\nVERIFIED'); sys.exit(9)", "FLASH_FAILED")]
        for code, expected in cases:
            with self.subTest(expected=expected):
                result, _, _, programmer = self.flash(code)
                self.assertEqual(result.error_code, expected)
                self.assertEqual(len(programmer.images), 1)

    def test_missing_and_invalid_patterns_fail_before_reset(self):
        for value in ("", "["):
            self.config["flash"]["connect_success_pattern"] = value
            result, _, reset, programmer = self.flash(connect_only=True)
            self.assertEqual(result.error_code, "FLASH_CONFIG_INVALID")
            self.assertEqual(reset.actions, [])
            self.assertEqual(programmer.images, [])

    def test_missing_flash_executable_still_releases_reset(self):
        ctx = self.context()
        reset = FakeReset()
        self.config["flash"]["executable"] = str(self.root / "missing cube.exe")
        result = FlashService().flash(self.config, ctx, FakeSession(), reset, None, None, True)
        self.assertEqual(result.error_code, "FLASH_TOOL_NOT_FOUND")
        self.assertEqual(reset.actions, ["assert", "release"])
        self.assertIn("log_path", result.data)

    def test_assert_failure_does_not_start_programmer(self):
        result, ctx, reset, _ = self.flash(reset=FakeReset(fail_assert=True), connect_only=True)
        self.assertEqual(result.error_code, "RESET_CONTROL_FAILED")
        self.assertEqual(reset.actions, ["assert", "release"])
        self.assertNotIn("START_PROGRAMMER", [state for state, _ in ctx.events])

    def test_release_failure_stops_running_process_and_finally_retries_release(self):
        result, _, reset, _ = self.flash("import time; time.sleep(20)",
                                        reset=FakeReset(fail_first_release=True), connect_only=True)
        self.assertEqual(result.error_code, "RESET_CONTROL_FAILED")
        self.assertFalse(process_alive(result.data["pid"]))
        self.assertEqual(reset.actions, ["assert", "release", "release"])

    def test_finally_release_failure_is_not_hidden(self):
        reset = FakeReset(fail_release=True)
        result, _, _, _ = self.flash("import time; time.sleep(20)", reset=reset, connect_only=True)
        self.assertEqual(result.error_code, "RESET_CONTROL_FAILED")
        self.assertEqual(result.data["hardware_state"], "UNKNOWN")
        self.assertIn("original_error_code", result.data)

    def test_timeout_stops_process_and_finally_releases(self):
        self.config["flash"]["timeout_s"] = .3
        result, _, reset, _ = self.flash("import time; time.sleep(20)", connect_only=True)
        self.assertEqual(result.error_code, "CONNECT_TIMEOUT")
        self.assertFalse(process_alive(result.data["pid"]))
        self.assertEqual(reset.actions[-1], "release")

    def test_cancel_during_release_delay_stops_process_and_releases(self):
        ctx = self.context()
        self.config["reset"]["release_delay_ms"] = 1000
        reset = FakeReset()
        timer = threading.Timer(.15, ctx.cancelled.set)
        timer.start()
        try:
            result = FlashService(FakeProgrammer("import time; time.sleep(20)")).flash(
                self.config, ctx, FakeSession(), reset, None, None, True)
        finally:
            timer.cancel()
            timer.join()
        self.assertEqual(result.error_code, "CANCELLED")
        self.assertFalse(process_alive(result.data["pid"]))
        self.assertEqual(reset.actions[-1], "release")

    def test_workflow_timeout_during_delay_still_releases(self):
        ctx = self.context()
        self.config["reset"]["release_delay_ms"] = 50
        ctx.deadline = time.monotonic() + .3
        original_sleep = ctx.sleep

        def expire_during_delay(seconds):
            ctx.deadline = time.monotonic() + .001
            original_sleep(seconds)

        ctx.sleep = expire_during_delay
        reset = FakeReset()
        result = FlashService(FakeProgrammer("import time; time.sleep(20)")).flash(
            self.config, ctx, FakeSession(), reset, None, None, True)
        self.assertEqual(result.error_code, "WORKFLOW_TIMEOUT")
        self.assertFalse(process_alive(result.data["pid"]))
        self.assertEqual(reset.actions[-1], "release")
        self.assertEqual(json.loads((ctx.path / "flash_result.json").read_text())["error_code"], "WORKFLOW_TIMEOUT")

    def test_runner_merges_large_output_without_pipe_deadlock_and_no_shell(self):
        ctx = self.context()
        marker = self.root / "must not exist"
        code = "import sys; print('x' * 200000); print('stderr marker', file=sys.stderr); print(sys.argv[1])"
        argument = f"; echo unsafe > {marker}"
        result = ProcessRunner().run([sys.executable, "-c", code, argument], self.root, 5, ctx, ctx.path / "process.log")
        self.assertEqual(result.returncode, 0)
        self.assertIn("stderr marker", result.output)
        self.assertIn(argument, result.output)
        self.assertGreater(len(result.output), 200000)
        self.assertFalse(marker.exists())

    def test_runner_interrupt_callback_waits_for_child(self):
        ctx = self.context()
        pids = []

        def interrupt(process):
            pids.append(process.pid)
            raise KeyboardInterrupt()

        with self.assertRaises(ToolError) as error:
            ProcessRunner().run([sys.executable, "-c", "import time; time.sleep(20)"],
                                self.root, 5, ctx, ctx.path / "process.log", interrupt)
        self.assertEqual(error.exception.code, "CANCELLED")
        self.assertFalse(process_alive(pids[0]))
        self.assertTrue(Path(error.exception.data["log_path"]).exists())

    def test_runner_timeout_reclaims_spawned_child_tree(self):
        ctx = self.context()
        pid_file = self.root / "child.pid"
        code = ("import subprocess, sys, time; from pathlib import Path; "
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)']); "
                f"Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(20)")
        with self.assertRaises(ToolError) as error:
            ProcessRunner().run([sys.executable, "-c", code], self.root, .5, ctx, ctx.path / "process.log")
        self.assertEqual(error.exception.code, "PROCESS_TIMEOUT")
        self.assertTrue(pid_file.exists(), "fake CLI did not start before the test timeout")
        self.assertFalse(process_alive(int(pid_file.read_text())))
        self.assertFalse(process_alive(error.exception.data["pid"]))

    def test_cleanup_failure_is_reported_with_original_error_and_log(self):
        ctx = self.context()
        original = ProcessRunner._stop_tree

        def cleanup_then_report(process):
            original(process)
            raise ToolError("PROCESS_CLEANUP_FAILED", "fake taskkill failure")

        with patch.object(ProcessRunner, "_stop_tree", side_effect=cleanup_then_report):
            with self.assertRaises(ToolError) as error:
                ProcessRunner().run([sys.executable, "-c", "import time; time.sleep(20)"],
                                    self.root, .2, ctx, ctx.path / "process.log")
        self.assertEqual(error.exception.code, "PROCESS_CLEANUP_FAILED")
        self.assertIn("original_error", error.exception.data)
        self.assertEqual(error.exception.data["original_error_code"], "PROCESS_TIMEOUT")
        self.assertIn("cleanup_error", error.exception.data)
        self.assertTrue(Path(error.exception.data["log_path"]).exists())


if __name__ == "__main__":
    unittest.main()
