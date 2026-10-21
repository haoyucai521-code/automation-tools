"""Single-attempt reset-assisted connection/flash with positive output evidence."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import time

from embedded_auto.adapters.programmer import CubeProgrammer
from embedded_auto.core.errors import ToolError
from embedded_auto.core.models import OperationResult
from embedded_auto.core.process import ProcessRunner
from .build_service import validate_artifact


class ResetAssistedConnectStrategy:
    def __init__(self, runner=None):
        self.runner = runner if runner is not None else ProcessRunner()

    @staticmethod
    def _reset_call(reset, method):
        try:
            getattr(reset, method)()
        except ToolError as exc:
            if exc.code in ("CANCELLED", "WORKFLOW_TIMEOUT"):
                raise
            raise ToolError("RESET_CONTROL_FAILED", str(exc), exc.data) from exc
        except Exception as exc:
            raise ToolError("RESET_CONTROL_FAILED", str(exc)) from exc

    def run(self, command, cwd, timeout_s, ctx, log_path, session, reset, release_delay_ms):
        original = None
        try:
            ctx.check()
            session.check()
            ctx.event("ASSERT_RESET")
            self._reset_call(reset, "assert_reset")

            def on_started(process):
                ctx.event("START_PROGRAMMER", pid=process.pid, command=command)
                ctx.sleep(release_delay_ms / 1000)
                self._reset_call(reset, "release_reset")
                ctx.event("RELEASE_RESET_AT_CONFIGURED_TIME", release_delay_ms=release_delay_ms,
                          monotonic_ns=time.monotonic_ns())
                session.check()
                ctx.event("WAIT_FLASH_AND_VERIFY")

            return self.runner.run(command, cwd, timeout_s, ctx, log_path, on_started=on_started)
        except BaseException as exc:
            original = exc
            raise
        finally:
            # No ctx.check here: release remains mandatory after cancellation.
            try:
                self._reset_call(reset, "release_reset")
            except BaseException as exc:
                data = dict(getattr(original, "data", None) or {})
                data.update(reset_release_error=str(exc), hardware_state="UNKNOWN")
                if original is not None:
                    data.update(original_error=str(original),
                                original_error_code=getattr(original, "code", type(original).__name__))
                raise ToolError("RESET_CONTROL_FAILED", "Failed to release reset during cleanup", data) from exc


class FlashService:
    def __init__(self, programmer=None, runner=None):
        self.programmer = programmer if programmer is not None else CubeProgrammer()
        self.strategy = ResetAssistedConnectStrategy(runner)

    def flash(self, config, ctx, session, reset, image: Path, manifest: dict,
              connect_only=False) -> OperationResult:
        started = time.monotonic()
        log_path = Path(ctx.path) / "flash.log"
        operation = "connect" if connect_only else "flash"
        attempts = []
        data = {"run_id": ctx.run_id, "connect_only": connect_only, "attempts": attempts}
        attempt = None
        try:
            ctx.check()
            flash = config["flash"]
            data.update(cli_verified=flash.get("cli_verified") is True,
                        compatibility_verified=flash.get("compatibility_verified") is True,
                        address=flash.get("address"))
            if not connect_only:
                artifact = validate_artifact(image, manifest, config)
                data.update(image=artifact["image"], sha256=artifact["sha256"], size=artifact["size"],
                            artifact_run_id=artifact["run_id"])
                if (config["target"].get("status") != "verified" or
                    flash.get("cli_verified") is not True or flash.get("compatibility_verified") is not True):
                    raise ToolError("FLASH_UNVERIFIED", "Installed CLI and target compatibility must be verified before writing")
                if flash.get("verify") is not True:
                    raise ToolError("FLASH_CONFIG_INVALID", "Flash verification is mandatory")
            pattern_names = ["connect_success_pattern"]
            if not connect_only:
                pattern_names.extend(["write_success_pattern", "verify_success_pattern"])
            patterns = {}
            for name in pattern_names:
                value = flash.get(name)
                if not isinstance(value, str) or not value.strip():
                    raise ToolError("FLASH_CONFIG_INVALID", f"A measured {name} is required")
                try:
                    patterns[name] = re.compile(value, re.MULTILINE)
                except re.error as exc:
                    raise ToolError("FLASH_CONFIG_INVALID", f"Invalid {name}: {exc}") from exc
            delay = config["reset"]["release_delay_ms"]
            timeout = flash.get("timeout_s", 60)
            if (isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or
                delay < 0 or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or
                timeout <= 0 or delay / 1000 >= min(timeout, float(ctx.remaining))):
                raise ToolError("FLASH_CONFIG_INVALID", "Release delay must fit within the process timeout")
            command = self.programmer.command(config, None if connect_only else Path(image))
            data["command"] = command
            attempt = {"attempt": 1, "started_at_ns": time.time_ns(), "command": command,
                       "address": flash.get("address"), "sha256": data.get("sha256"),
                       "release_delay_ms": delay}
            attempts.append(attempt)
            result = self.strategy.run(command, Path(ctx.path), timeout, ctx, log_path,
                                       session, reset, delay)
            # CLI output is archived verbatim; normalize platform line endings
            # only for configured multiline expressions such as ^CONNECTED$.
            output = result.output.replace("\r\n", "\n").replace("\r", "\n")
            matched = {name: bool(pattern.search(output)) for name, pattern in patterns.items()}
            attempt.update(returncode=result.returncode, duration_ms=result.duration_ms,
                           finished_at_ns=time.time_ns(), matched=matched)
            data.update(returncode=result.returncode, matched=matched)
            if not matched["connect_success_pattern"]:
                raise ToolError("CONNECT_FAILED", "CLI did not report a successful connection")
            if connect_only:
                if result.returncode != 0:
                    raise ToolError("CONNECT_FAILED", "Connect-only command returned a nonzero exit code")
            else:
                if not matched["write_success_pattern"]:
                    raise ToolError("FLASH_FAILED", "CLI did not report a successful write")
                if not matched["verify_success_pattern"]:
                    raise ToolError("FLASH_VERIFY_FAILED", "CLI did not report successful verification")
                if result.returncode != 0:
                    raise ToolError("FLASH_FAILED", "Flash command returned a nonzero exit code")
            attempt["success"] = True
            self._record(log_path, data)
            return self._archive(ctx, OperationResult(True, operation, duration_ms=int((time.monotonic() - started) * 1000),
                                                     log_path=log_path, data=data))
        except (ToolError, OSError, KeyError, TypeError, ValueError) as exc:
            code = exc.code if isinstance(exc, ToolError) else "FLASH_FAILED"
            code = {"PROCESS_TOOL_NOT_FOUND": "FLASH_TOOL_NOT_FOUND",
                    "PROCESS_TIMEOUT": "CONNECT_TIMEOUT", "PROCESS_FAILED": "FLASH_FAILED"}.get(code, code)
            data.update(getattr(exc, "data", None) or {})
            if attempt is not None:
                attempt.update(success=False, error_code=code, finished_at_ns=time.time_ns())
            data.update(error_code=code, message=str(exc))
            self._record(log_path, data)
            return self._archive(ctx, OperationResult(False, operation, error_code=code, message=str(exc),
                                                     duration_ms=int((time.monotonic() - started) * 1000),
                                                     log_path=log_path, data=data))

    @staticmethod
    def _archive(ctx, result):
        result.run_id = ctx.run_id
        with (Path(ctx.path) / "flash_result.json").open("w", encoding="utf-8") as stream:
            json.dump(result.to_dict(), stream, ensure_ascii=False, indent=2)
        return result

    @staticmethod
    def _record(log_path, data):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write("\n" + json.dumps(data, ensure_ascii=False, default=str) + "\n")
