"""IAR command-line builder adapter."""
from pathlib import Path
from embedded_auto.core.errors import ToolError


class IARBackend:
    def command(self, config: dict) -> list[str]:
        build = config["build"]
        executable = build.get("executable")
        project = build.get("project_file")
        configuration = build.get("configuration", "Release")
        if not all(isinstance(value, str) and value for value in
                   (executable, project, configuration)) or not Path(project).is_absolute():
            raise ToolError("BUILD_CONFIG_INVALID", "IAR requires executable, absolute project_file and configuration")
        return [executable, project, "-build", configuration]
