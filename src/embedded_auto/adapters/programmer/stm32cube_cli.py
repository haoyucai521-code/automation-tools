"""ST CLI argument construction only; this adapter never controls RTS.

Parameter reference (not evidence of installed-tool or target compatibility):
https://dev.st.com/stm32cube-docs/prog/2.23.0/en/docs/markup/CubeProg_Command_Lines.html
"""
from pathlib import Path

from embedded_auto.core.errors import ToolError


class CubeProgrammer:
    def command(self, config: dict, image: Path | None = None) -> list[str]:
        flash = config["flash"]
        executable = flash.get("executable")
        interface = flash.get("interface", "SWD")
        mode = flash.get("mode", "NORMAL")
        reset_mode = flash.get("reset_mode", "SWrst")
        if not isinstance(executable, str) or not executable:
            raise ToolError("FLASH_CONFIG_INVALID", "flash.executable is required")
        if interface != "SWD" or mode not in ("NORMAL", "UR", "HOTPLUG") or reset_mode not in ("SWrst", "HWrst", "Crst"):
            raise ToolError("FLASH_CONFIG_INVALID", "Unsupported SWD connection parameters")
        command = [executable, "-c", f"port={interface}", f"mode={mode}", f"reset={reset_mode}"]
        if image is not None:
            address = flash.get("address")
            if type(address) is not int or address < 0 or address >= 2**32:
                raise ToolError("FLASH_CONFIG_INVALID", "A valid explicit BIN address is required")
            if flash.get("verify") is not True:
                raise ToolError("FLASH_CONFIG_INVALID", "BIN writes require verification")
            command.extend(["-w", str(image), f"0x{address:08X}", "-v"])
        return command
