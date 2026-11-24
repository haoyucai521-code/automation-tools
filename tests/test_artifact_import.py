"""外部 BIN 离线导入和已有产物身份校验链。"""
import copy
import contextlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import yaml

from embedded_auto.core.errors import ToolError
from embedded_auto.core.run_context import RunContext
from embedded_auto.firmware.artifact_import import import_artifact
from embedded_auto.firmware.build_service import validate_artifact
from embedded_auto.cli import main
from embedded_auto.application.workflow_engine import WorkflowEngine
from embedded_auto.serial.serial_session import SerialSession
from test_workflow_cli import config_for


class ArtifactImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"app": {"runs_dir": str(self.root / "runs"), "default_timeout_s": 5},
                       "target": {"id": "fake", "exact_part_number": "FAKE-123", "status": "verified"},
                       "flash": {"address": 0x08000000, "address_source": "fake layout",
                                 "region_start": 0x08000000, "region_size": 1024,
                                 "ram_start": 0x20000000, "ram_size": 1024, "ram_source": "fake layout"},
                       "sources": {"app": "fake-app", "target": "fake-target"}}
        self.source = self.root / "input.bin"
        self.source.write_bytes(struct.pack("<II", 0x20000400, 0x08000009) + b"data")

    def run_import(self, source=None, address="0x08000000"):
        ctx = RunContext(self.config, "artifact import")
        return import_artifact(self.config, ctx, source or self.source, address, "fake compiler output")

    def test_snapshot_survives_source_change_and_manifest_is_not_build(self):
        result = self.run_import()
        self.assertTrue(result.success, result.to_dict())
        data = result.data
        manifest = data["manifest"]
        self.assertNotIn("build_success", manifest)
        self.assertNotIn("command", manifest)
        self.assertFalse(data["device_compatibility_verified"])
        self.source.write_bytes(b"changed")
        validate_artifact(Path(data["artifact"]), manifest, self.config)
        json.dumps(result.to_dict())

    def test_invalid_files_vectors_bounds_and_address(self):
        for content in (b"", b"tiny", b"\0" * 12, b"x" * 1025):
            with self.subTest(size=len(content)):
                self.source.write_bytes(content)
                self.assertFalse(self.run_import().success)
        self.assertFalse(self.run_import(self.root / "missing.bin").success)
        self.assertFalse(self.run_import(address="0x08001000").success)

    def test_tamper_and_cross_target_rejected(self):
        result = self.run_import()
        image, manifest = Path(result.data["artifact"]), result.data["manifest"]
        for key, value in (("target_id", "other"), ("address", 0x08001000),
                           ("sha256", "0" * 64), ("vector_check", {}),
                           ("config_sources", {}), ("source_description", "different"),
                           ("imported_at", "different"), ("run_id", "other")):
            with self.subTest(key=key), self.assertRaises(ToolError):
                validate_artifact(image, dict(manifest, **{key: value}), self.config)
        other = copy.deepcopy(self.config)
        other["target"]["id"] = "other"
        with self.assertRaises(ToolError):
            validate_artifact(image, manifest, other)
        image.write_bytes(b"tampered")
        with self.assertRaises(ToolError):
            validate_artifact(image, manifest, self.config)

    def test_unverified_offset_ram_overflow_and_existing_snapshot(self):
        for change in ({"address": 0x08000100}, {"ram_start": 0xfffffff0, "ram_size": 128},
                       {"ram_size": 0}, {"region_start": 0xfffffff0, "region_size": 128}):
            saved = self.config["flash"].copy()
            self.config["flash"].update(change)
            try:
                self.assertFalse(self.run_import(address=str(self.config["flash"]["address"])).success)
            finally:
                self.config["flash"] = saved
        ctx = RunContext(self.config, "artifact import")
        (ctx.path / "firmware.bin").write_bytes(b"existing")
        result = import_artifact(self.config, ctx, self.source, "0x08000000", "fake source")
        self.assertEqual(result.error_code, "ARTIFACT_RUN_CONFLICT")
        self.assertEqual((ctx.path / "firmware.bin").read_bytes(), b"existing")

    def test_copy_budget_expiry_stops_before_manifest(self):
        ctx = RunContext(self.config, "artifact import")
        checks = 0
        def check():
            nonlocal checks
            checks += 1
            if checks == 2:
                raise ToolError("WORKFLOW_TIMEOUT", "expired during copy")
        with patch.object(ctx, "check", side_effect=check):
            result = import_artifact(self.config, ctx, self.source, "0x08000000", "fake source")
        self.assertEqual(result.error_code, "WORKFLOW_TIMEOUT")
        self.assertFalse((ctx.path / "artifact_manifest.json").exists())

    def test_cli_import_never_builds_opens_serial_or_runs_process(self):
        config = config_for(self.root)
        config["flash"].update(self.config["flash"])
        (self.root / "targets").mkdir()
        (self.root / "app.yaml").write_text(yaml.safe_dump({"app": config.pop("app")}), encoding="utf-8")
        (self.root / "targets" / "fake.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        output = io.StringIO()
        with patch.object(WorkflowEngine, "execute", side_effect=AssertionError("workflow invoked")), \
             patch.object(SerialSession, "open", side_effect=AssertionError("serial opened")), \
             patch("subprocess.Popen", side_effect=AssertionError("process started")), \
             contextlib.redirect_stdout(output):
            code = main(["artifact", "import", "--target", "fake", "--config", str(self.root / "app.yaml"),
                         "--file", str(self.source), "--address", "0x08000000", "--source", "fake", "--json"])
        self.assertEqual(code, 0, output.getvalue())
        self.assertTrue(json.loads(output.getvalue())["success"])


if __name__ == "__main__":
    unittest.main()
