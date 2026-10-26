"""结构与兼容性回归；只使用临时目录、Fake 和受控子进程。"""
import ast
import copy
import importlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from embedded_auto.application import composition
from embedded_auto.application.config import load_config, validate_config, validation_issues
from embedded_auto.application.workflow_engine import WorkflowEngine
from embedded_auto.core.errors import ToolError
from embedded_auto.core.models import OperationResult
from embedded_auto.core.run_context import RunContext
from test_workflow_cli import config_for


SOURCE = Path(__file__).resolve().parents[1] / "src" / "embedded_auto"


class StructureTests(unittest.TestCase):
    def test_dependency_directions(self):
        # 相对导入也解析为绝对名称，避免迁移后改写导入形式绕过边界。
        for layer in ("core", "serial", "firmware"):
            forbidden = {"application", "adapters", "services", "agent",
                         "serial", "firmware"} - {layer}
            for path in (SOURCE / layer).rglob("*.py"):
                package = ".".join(("embedded_auto", *path.relative_to(SOURCE).parts[:-1]))
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        modules = [item.name for item in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        name = "." * node.level + (node.module or "")
                        modules = [importlib.util.resolve_name(name, package) if node.level else name]
                        modules += [modules[0] + "." + item.name for item in node.names]
                    else:
                        continue
                    for module in modules:
                        with self.subTest(path=path.name, module=module):
                            self.assertFalse(any(
                                module == f"embedded_auto.{name}" or
                                module.startswith(f"embedded_auto.{name}.")
                                for name in forbidden
                            ))
                            if layer in ("serial", "firmware"):
                                self.assertFalse(module == "serial" or module.startswith("serial."))

    def test_core_import_does_not_load_upper_layers(self):
        script = """
import sys
import embedded_auto.core.config
import embedded_auto.core.run_context
for name in sys.modules:
    assert not any(name == 'embedded_auto.' + layer or
                   name.startswith('embedded_auto.' + layer + '.')
                   for layer in ('application', 'firmware', 'serial', 'adapters', 'services')), name
"""
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_each_domain_import_is_isolated(self):
        for layer, forbidden in (("serial", "firmware"), ("firmware", "serial")):
            script = f"""
import importlib
import sys
for suffix in {{"serial": ("config", "interfaces", "serial_session", "test_runner"),
                "firmware": ("config", "interfaces", "build_service", "flash_service")}}[{layer!r}]:
    importlib.import_module('embedded_auto.' + {layer!r} + '.' + suffix)
for name in sys.modules:
    assert not any(name == 'embedded_auto.' + other or
                   name.startswith('embedded_auto.' + other + '.')
                   for other in ({forbidden!r}, 'application', 'adapters', 'services')), name
"""
            with self.subTest(layer=layer):
                result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                        text=True, timeout=10, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_names_resolve_in_either_import_order(self):
        for legacy_first in (True, False):
            imports = [
                "from embedded_auto.core.interfaces import SerialPort as old",
                "from embedded_auto.serial.interfaces import SerialPort as new",
            ]
            if not legacy_first:
                imports.reverse()
            script = "\n".join(imports) + """
assert old is new
from embedded_auto.core.config import *
from embedded_auto.application.config import load_config as canonical
assert load_config is canonical
"""
            with self.subTest(legacy_first=legacy_first):
                result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                        text=True, timeout=10, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_new_implementation_does_not_use_legacy_entries(self):
        legacy_config_names = {"load_config", "validate_config", "validation_issues"}
        for layer in ("core", "serial", "firmware", "application", "adapters", "agent"):
            for path in (SOURCE / layer).rglob("*.py"):
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    if isinstance(node, ast.ImportFrom):
                        self.assertFalse((node.module or "").startswith("embedded_auto.services"), path)
                        self.assertNotEqual(node.module, "embedded_auto.core.interfaces", path)
                        if node.module == "embedded_auto.core.config":
                            self.assertFalse(legacy_config_names & {n.name for n in node.names}, path)

    def test_legacy_config_names_resolve_to_application(self):
        legacy = importlib.import_module("embedded_auto.core.config")
        self.assertIs(legacy.load_config, load_config)
        self.assertIs(legacy.validate_config, validate_config)
        self.assertIs(legacy.validation_issues, validation_issues)
        with self.assertRaises(AttributeError):
            getattr(legacy, "not_a_supported_interface")

    def test_legacy_interfaces_and_state_keep_identity(self):
        legacy = importlib.import_module("embedded_auto.core.interfaces")
        firmware = importlib.import_module("embedded_auto.firmware.interfaces")
        serial = importlib.import_module("embedded_auto.serial.interfaces")
        for name in ("BuildBackend", "Builder", "Programmer", "Flasher"):
            self.assertIs(getattr(legacy, name), getattr(firmware, name))
        self.assertIs(legacy.SerialPort, serial.SerialPort)
        from embedded_auto.core.models import WorkflowState as old_state
        from embedded_auto.application.models import WorkflowState
        self.assertIs(old_state, WorkflowState)
        self.assertEqual(WorkflowState.ERASE_FLASH.value, "ERASE_FLASH")
        with self.assertRaises(AttributeError):
            getattr(legacy, "UniversalSession")

    def test_legacy_service_names(self):
        expected = {
            "build_service": {"BuildService": composition.BuildService},
            "flash_service": {"FlashService": composition.FlashService},
            "serial_session": {"SerialSession": composition.SerialSession},
            "workflow_engine": {"WorkflowEngine": WorkflowEngine},
        }
        for name in ("reset_controller", "log_service", "log_analyzer", "test_runner"):
            canonical = importlib.import_module("embedded_auto.serial." + name)
            symbols = {
                "reset_controller": ["ResetController"], "log_service": ["LogService"],
                "log_analyzer": ["LogAnalyzer"], "test_runner": ["TestRunner", "_RegexMatcher", "validate_suite"],
            }[name]
            expected[name] = {symbol: getattr(canonical, symbol) for symbol in symbols}
        for name, symbols in expected.items():
            legacy = importlib.import_module("embedded_auto.services." + name)
            for symbol, obj in symbols.items():
                self.assertIs(getattr(legacy, symbol), obj)
        from embedded_auto.services.build_service import validate_artifact as old_artifact
        from embedded_auto.firmware.build_service import validate_artifact
        self.assertIs(old_artifact, validate_artifact)
        from embedded_auto.services.flash_service import ResetAssistedConnectStrategy as old_strategy
        from embedded_auto.firmware.flash_service import ResetAssistedConnectStrategy
        self.assertIs(old_strategy, ResetAssistedConnectStrategy)

    def test_domain_constructors_only_use_injected_backends(self):
        from embedded_auto.firmware.build_service import BuildService
        from embedded_auto.firmware.flash_service import FlashService
        from embedded_auto.serial.serial_session import SerialSession
        runner, programmer = object(), object()
        backend_factory = lambda name: None
        serial_factory = lambda **kwargs: None
        self.assertIs(BuildService(runner, backend_factory=backend_factory).backend_factory, backend_factory)
        flash = FlashService(programmer, runner)
        self.assertIs(flash.programmer, programmer)
        self.assertIs(flash.strategy.runner, runner)
        config = {"serial": {"port": "FAKE"}, "reset": {"asserted_rts_value": True}}
        self.assertIs(SerialSession(config, serial_factory)._factory, serial_factory)

    def test_legacy_default_constructors_are_assembled_without_opening_hardware(self):
        from embedded_auto.adapters.build import GCCBackend, IARBackend
        from embedded_auto.adapters.programmer import CubeProgrammer
        from embedded_auto.adapters.serial import create_serial
        engine = WorkflowEngine()
        self.assertIs(engine.builder.backend_factory, composition.build_backend)
        self.assertIsInstance(engine.flasher.programmer, CubeProgrammer)
        config = {"serial": {"port": "FAKE"}, "reset": {"asserted_rts_value": True}}
        self.assertIs(engine.session_factory(config)._factory, create_serial)
        self.assertIsInstance(composition.build_backend("gcc"), GCCBackend)
        self.assertIsInstance(composition.build_backend("iar"), IARBackend)
        self.assertIsNone(composition.build_backend("unknown"))


class RunContextStructureTests(unittest.TestCase):
    def test_run_without_target_build_or_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {"app": {"runs_dir": str(root / "runs"), "workspace": str(root)}}
            with patch("embedded_auto.core.run_context.subprocess.run") as run:
                ctx = RunContext(config, "inspection", run_label="independent")
                self.assertRegex(ctx.run_id, r"^\d{8}T\d{12}Z_independent_[0-9a-f]{8}$")
                finished = ctx.finish(OperationResult(True, "inspection"))
                run.assert_not_called()  # 创建/归档上下文本身不执行 Git 快照。
            self.assertEqual(yaml.safe_load((ctx.path / "run_config.yaml").read_text(encoding="utf-8")), config)
            self.assertEqual(json.loads((ctx.path / "run_summary.json").read_text(encoding="utf-8")), finished.to_dict())
            summary = (ctx.path / "agent_summary.md").read_text(encoding="utf-8")
            self.assertNotIn("serial.bin", summary)
            self.assertIn("run_summary.json / events.jsonl", summary)

    def test_operation_is_default_identity_without_target(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {"app": {"runs_dir": directory}}
            first = RunContext(config, "inspection")
            second = RunContext(config, "inspection")
            self.assertIn("_inspection_", first.run_id)
            self.assertNotEqual(first.run_id, second.run_id)
            self.assertNotEqual(first.path, second.path)

    def test_old_firmware_identity_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            legacy = RunContext(config, "build")
            self.assertRegex(legacy.run_id, r"^\d{8}T\d{12}Z_fake_[0-9a-f]{8}$")
            explicit = RunContext(config, "build", run_label="inspection")
            self.assertIn("_inspection_", explicit.run_id)
            self.assertEqual(explicit.config, config)

    def test_invalid_explicit_label_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {"app": {"runs_dir": str(Path(directory) / "runs")}}
            for label in ("", "../bad", "with space"):
                with self.subTest(label=label), self.assertRaises(ToolError):
                    RunContext(config, "inspection", run_label=label)
            self.assertFalse((Path(directory) / "runs").exists())

    def test_explicit_evidence_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = RunContext({"app": {"runs_dir": directory}}, "inspection",
                             evidence_hint="原始证据: sample.jsonl")
            ctx.finish(OperationResult(True, "inspection"))
            self.assertIn("- 原始证据: sample.jsonl\n",
                          (ctx.path / "agent_summary.md").read_text(encoding="utf-8"))

    def test_firmware_workflow_keeps_original_summary_and_dry_run_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            with patch.object(composition.SerialSession, "open", side_effect=AssertionError("hardware")):
                result = WorkflowEngine().execute(config, "build", dry_run=True)
            self.assertTrue(result.success)
            self.assertIn("_fake_", result.run_id)
            summary = (Path(result.data["run_path"]) / "agent_summary.md").read_text(encoding="utf-8")
            self.assertIn("- 原始串口数据（若打开串口）: serial.bin / serial.jsonl\n", summary)

    def test_explicit_git_snapshot_keeps_legacy_cwd_and_supports_no_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configs = [
                ({"app": {"runs_dir": directory, "workspace": str(root)}}, str(root)),
                (config_for(root), str(root)),
            ]
            configs[1][0]["build"]["cwd"] = str(root / "firmware")
            configs[1] = (configs[1][0], str(root / "firmware"))
            for config, expected_cwd in configs:
                ctx = RunContext(config, "inspection")
                with patch("embedded_auto.core.run_context.subprocess.run",
                           return_value=SimpleNamespace(returncode=0, stdout=b"evidence")) as run:
                    ctx.snapshot_git()
                self.assertEqual(run.call_count, 3)
                self.assertTrue(all(call.kwargs["cwd"] == expected_cwd for call in run.call_args_list))
                self.assertTrue((ctx.path / "git_state.json").exists())


class ConfigurationStructureTests(unittest.TestCase):
    def test_existing_loader_paths_sources_and_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "targets").mkdir()
            config = config_for(root)
            config["app"]["workspace"] = "."
            config["app"]["runs_dir"] = "runs"
            config["build"]["cwd"] = "firmware"
            config["build"]["artifact"] = "out.bin"
            app = {"app": config.pop("app")}
            (root / "app.yaml").write_text(yaml.safe_dump(app), encoding="utf-8")
            (root / "targets/fake.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
            loaded = load_config(root / "app.yaml", "fake", {"serial": {"port": "OVERRIDE"}})
            self.assertEqual(loaded["app"]["workspace"], str(root))
            self.assertEqual(loaded["build"]["artifact"], str(root / "firmware/out.bin"))
            self.assertEqual(loaded["serial"]["port"], "OVERRIDE")
            self.assertEqual(loaded["sources"], {"app": str(root / "app.yaml"), "target": str(root / "targets/fake.yaml")})

    def test_validation_order_and_monitor_scope_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            config["build"]["backend"] = "bad"
            config["serial"]["port"] = ""
            with self.assertRaises(ToolError) as caught:
                validate_config(config, "run")
            self.assertEqual(caught.exception.code, "CONFIG_INVALID")
            self.assertEqual(str(caught.exception), "build.backend 仅支持 gcc/iar")
            with self.assertRaises(ToolError) as caught:
                validate_config(config, "monitor")
            self.assertEqual(str(caught.exception), "serial.port 尚未配置或确认")
            config["serial"]["port"] = "FAKE"
            config["flash"]["address"] = "not-an-address"
            validate_config(config, "monitor")

    def test_address_normalization_and_validation_issues_copy_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            for key in ("address", "region_start", "region_size"):
                config["flash"][key] = hex(config["flash"][key])
            before = copy.deepcopy(config)
            self.assertEqual(validation_issues(config, "build"), [])
            self.assertEqual(config, before)
            validate_config(config, "build")
            self.assertEqual(config["flash"]["address"], 0x08000000)
            self.assertIs(type(config["flash"]["address"]), int)


if __name__ == "__main__":
    unittest.main()
