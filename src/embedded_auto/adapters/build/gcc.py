"""Project-defined GCC/Make/CMake/Ninja command, without shell interpretation."""
from embedded_auto.core.errors import ToolError


class GCCBackend:
    def command(self, config: dict) -> list[str]:
        command = config["build"].get("command")
        if not isinstance(command, list) or not command or not all(
            isinstance(arg, str) and arg for arg in command
        ):
            raise ToolError("BUILD_CONFIG_INVALID", "build.command must be a nonempty string list")
        return list(command)
