"""可选真实 ARM GCC 集成；不连接串口、不调用烧录器。"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from embedded_auto.core.config import validate_config
from embedded_auto.core.run_context import RunContext
from embedded_auto.services.build_service import BuildService


@unittest.skipUnless(shutil.which("arm-none-eabi-gcc") and shutil.which("arm-none-eabi-objcopy"), "本机未安装 ARM GCC/objcopy")
class RealGCCIntegrationTests(unittest.TestCase):
    def test_cortex_m0_bin_and_manifest(self):
        with tempfile.TemporaryDirectory(prefix="embedded gcc ") as directory:
            root = Path(directory)
            (root / "main.c").write_text("void main(void) { for (;;) { __asm__(\"nop\"); } }\n", encoding="utf-8")
            compile_command = [shutil.which("arm-none-eabi-gcc"), "-mcpu=cortex-m0", "-mthumb", "-nostdlib",
                               "-Wl,-Ttext=0x08000000", "-Wl,--entry=main", "main.c", "-o", "firmware.elf"]
            convert_command = [shutil.which("arm-none-eabi-objcopy"), "-O", "binary", "firmware.elf", "firmware.bin"]
            driver = f"import subprocess; subprocess.run({compile_command!r}, check=True); subprocess.run({convert_command!r}, check=True)"
            config = {
                "app": {"runs_dir": str(root / "runs"), "workspace": str(root), "default_timeout_s": 15},
                "target": {"id": "gcc_fixture", "exact_part_number": "TEST_ONLY_CORTEX_M0", "status": "template"},
                "build": {"backend": "gcc", "cwd": str(root), "artifact": str(root / "firmware.bin"), "timeout_s": 10,
                          "command": [sys.executable, "-c", driver]},
                "flash": {"address": "0x08000000", "region_start": "0x08000000", "region_size": 1024,
                          "address_source": "test fixture -Ttext (NOT a real board layout)"},
                "reset": {}, "serial": {}, "startup": {},
            }
            validate_config(config, "build")
            ctx = RunContext(config, "build")
            result = BuildService().build(config, ctx)
            self.assertTrue(result.success, (result.to_dict(), (ctx.path / "build.log").read_text(encoding="utf-8", errors="replace")))
            self.assertGreater(result.data["size"], 0)
            self.assertEqual(result.data["manifest"]["address"], 0x08000000)
            self.assertEqual(len(result.data["sha256"]), 64)
            self.assertEqual((ctx.path / "firmware.bin").read_bytes(), (root / "firmware.bin").read_bytes())


if __name__ == "__main__":
    unittest.main()
