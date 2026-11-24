"""Single-attempt reset-assisted mass erase, write and verified programming."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
import math
import os
from pathlib import Path
import re
import shutil
import time

from embedded_auto.core.errors import ToolError
from embedded_auto.core.models import OperationResult
from embedded_auto.core.process import ProcessRunner
from .build_service import _fingerprint, validate_artifact


@contextmanager
def _protect_flash_input(path: Path):
    if os.name != "nt":
        # 非 Windows 只用于离线验证；写入前重新校验副本，不宣称提供相同的锁。
        yield
        return
    import ctypes
    from ctypes import wintypes

    GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING = 0x80000000, 1, 3
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create_file.restype = wintypes.HANDLE
    close_handle = kernel.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    # 允许烧录工具读取，但在本句柄释放前拒绝写入、删除和替换文件。
    handle = create_file(str(path), GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        if not close_handle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def _check_flash_input(path: Path, artifact: dict):
    actual = _fingerprint(path)
    if any(actual[key] != artifact[key] for key in ("sha256", "size")):
        raise ToolError("ARTIFACT_HASH_MISMATCH", "本次烧录副本与获授权的 BIN 不一致", {"flash_input_actual": actual})


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
                    try:
                        original.data = data
                    except (AttributeError, TypeError):
                        original.add_note(f"reset release cleanup failed: {exc}")
                else:
                    raise ToolError("RESET_CONTROL_FAILED", "Failed to release reset during cleanup", data) from exc


class FlashService:
    def __init__(self, programmer, runner=None):
        self.programmer = programmer
        self.strategy = ResetAssistedConnectStrategy(runner)

    def flash(self, config, ctx, session, reset, image: Path, manifest: dict,
              connect_only=False, authorization=None) -> OperationResult:
        started = time.monotonic()
        log_path = Path(ctx.path) / "flash.log"
        operation = "connect" if connect_only else "flash"
        attempts = []
        data = {"run_id": ctx.run_id, "connect_only": connect_only, "attempts": attempts,
                "phase": "preflight", "destructive_effect": "NOT_STARTED", "confirmed_actions": [],
                "physical_reset_state": "UNKNOWN"}
        attempt = None
        outcome = None
        resources = ExitStack()
        try:
            ctx.check()
            flash = config["flash"]
            from .config import validate_cli_version
            validate_cli_version(flash)
            data.update(cli_verified=flash.get("cli_verified") is True,
                        compatibility_verified=flash.get("compatibility_verified") is True,
                        address=flash.get("address"), tool_version_status="DECLARED_NOT_VERIFIED_THIS_RUN")
            if not connect_only:
                artifact = validate_artifact(image, manifest, config)
                data.update(image=artifact["image"], sha256=artifact["sha256"], size=artifact["size"],
                            artifact_run_id=artifact["run_id"])
                if (config["target"].get("status") != "verified" or
                    flash.get("cli_verified") is not True or flash.get("compatibility_verified") is not True):
                    raise ToolError("FLASH_UNVERIFIED", "Installed CLI and target compatibility must be verified before writing")
                if flash.get("verify") is not True:
                    raise ToolError("FLASH_CONFIG_INVALID", "Flash verification is mandatory")
                from .erase_policy import validate_erase_policy
                data["erase_authorization"] = validate_erase_policy(config, artifact, authorization)
            pattern_names = ["connect_success_pattern"]
            if not connect_only:
                pattern_names.extend(["erase_success_pattern", "write_success_pattern", "verify_success_pattern"])
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
            flash_input = None
            if not connect_only:
                flash_input = Path(ctx.path).resolve() / "flash_input.bin"
                data["flash_input"] = str(flash_input)
                ctx.check()
                try:
                    with Path(artifact["image"]).open("rb") as source, flash_input.open("xb") as destination:
                        shutil.copyfileobj(source, destination)
                except FileExistsError as exc:
                    raise ToolError("ARTIFACT_RUN_CONFLICT", "运行目录内已有烧录输入副本，不覆盖") from exc
                resources.enter_context(_protect_flash_input(flash_input))
                _check_flash_input(flash_input, artifact)
                ctx.check()
            command = self.programmer.command(config, flash_input)
            data["command"] = command
            erase_command = None if connect_only else self.programmer.erase_command(config)
            attempt = {"attempt": 1, "started_at_ns": time.time_ns(), "command": command,
                       "address": flash.get("address"), "sha256": data.get("sha256"),
                       "release_delay_ms": delay}
            attempts.append(attempt)
            if erase_command is not None:
                erase_log_path = Path(ctx.path) / "erase.log"
                data["erase"] = {"scope": "all", "start": data["erase_authorization"]["erase_start"],
                                 "size": data["erase_authorization"]["erase_size"], "command": erase_command,
                                 "log_path": str(erase_log_path), "success": False,
                                 "warning": "Entire internal Flash, including KV, parameters and bootloader, is erased"}
                attempt["erase"] = data["erase"]
                ctx.event("ERASE_FLASH", scope="all", command=erase_command)
                data.update(phase="erase", destructive_effect="UNKNOWN")
                erased = self.strategy.run(erase_command, Path(ctx.path), timeout, ctx,
                                           erase_log_path, session, reset, delay)
                erase_output = erased.output.replace("\r\n", "\n").replace("\r", "\n")
                erase_matched = {name: bool(patterns[name].search(erase_output)) for name in
                                 ("connect_success_pattern", "erase_success_pattern")}
                data["erase"].update(returncode=erased.returncode, duration_ms=erased.duration_ms,
                                     matched=erase_matched)
                if not erase_matched["connect_success_pattern"]:
                    raise ToolError("CONNECT_FAILED", "Erase CLI did not report a successful connection")
                if erased.returncode != 0 or not erase_matched["erase_success_pattern"]:
                    raise ToolError("FLASH_ERASE_FAILED", "Mass erase did not succeed; write was not started")
                data["erase"]["success"] = True
                data["confirmed_actions"].append("mass_erase_succeeded")
                data["destructive_effect"] = "ERASE_CONFIRMED"
                ctx.event("FLASH_ERASE_SUCCEEDED", scope="all")
                ctx.check()
                session.check()
                if os.name != "nt":
                    _check_flash_input(flash_input, artifact)
                ctx.event("WRITE_FLASH", address=flash["address"])
                data["phase"] = "write_verify"
            result = self.strategy.run(command, Path(ctx.path), timeout, ctx, log_path,
                                       session, reset, delay)
            # CLI output is archived verbatim; normalize platform line endings
            # only for configured multiline expressions such as ^CONNECTED$.
            output = result.output.replace("\r\n", "\n").replace("\r", "\n")
            matched = {name: bool(pattern.search(output)) for name, pattern in patterns.items()
                       if name != "erase_success_pattern"}
            attempt.update(returncode=result.returncode, duration_ms=result.duration_ms,
                           finished_at_ns=time.time_ns(), matched=matched)
            data.update(returncode=result.returncode, matched=matched)
            if not matched["connect_success_pattern"]:
                raise ToolError("CONNECT_FAILED", "CLI did not report a successful connection")
            if connect_only:
                if result.returncode != 0:
                    raise ToolError("CONNECT_FAILED", "Connect-only command returned a nonzero exit code")
            else:
                if matched["write_success_pattern"]:
                    data["confirmed_actions"].append("write_success_pattern_observed")
                if not matched["write_success_pattern"]:
                    raise ToolError("FLASH_FAILED", "CLI did not report a successful write")
                if not matched["verify_success_pattern"]:
                    raise ToolError("FLASH_VERIFY_FAILED", "CLI did not report successful verification")
                if result.returncode != 0:
                    raise ToolError("FLASH_FAILED", "Flash command returned a nonzero exit code")
                data["confirmed_actions"].append("write_and_verify_succeeded")
            attempt["success"] = True
            self._record(log_path, data)
            outcome = OperationResult(True, operation, duration_ms=int((time.monotonic() - started) * 1000),
                                      log_path=log_path, data=data)
        except (ToolError, OSError, KeyError, TypeError, ValueError, KeyboardInterrupt) as exc:
            if isinstance(exc, KeyboardInterrupt):
                ctx.cancelled.set()
                code, message = "CANCELLED", "用户中断"
            else:
                code = exc.code if isinstance(exc, ToolError) else "FLASH_FAILED"
                message = str(exc)
            code = {"PROCESS_TOOL_NOT_FOUND": "FLASH_TOOL_NOT_FOUND",
                    "PROCESS_TIMEOUT": "CONNECT_TIMEOUT" if connect_only else "PROCESS_TIMEOUT",
                    "PROCESS_FAILED": "FLASH_FAILED"}.get(code, code)
            data.update(getattr(exc, "data", None) or {})
            if ("mass_erase_succeeded" in data["confirmed_actions"] and
                    "write_and_verify_succeeded" not in data["confirmed_actions"]):
                data["device_may_be_unbootable"] = True
                write_status = "写入阶段尚未进入" if data["phase"] == "erase" else "写入或校验未完成"
                data["manual_review"] = f"已确认全片擦除；{write_status}，请人工检查并评估恢复方案"
            if attempt is not None:
                attempt.update(success=False, error_code=code, finished_at_ns=time.time_ns())
            data.update(error_code=code, message=message)
            try:
                self._record(log_path, data)
            except OSError as log_error:
                data["error_log_write_error"] = str(log_error)
            outcome = OperationResult(False, operation, error_code=code, message=message,
                                      duration_ms=int((time.monotonic() - started) * 1000),
                                      log_path=log_path, data=data)
        finally:
            try:
                resources.close()
            except OSError as exc:
                data["flash_input_release_error"] = str(exc)
                if outcome is not None and outcome.success:
                    outcome.success, outcome.error_code = False, "CLEANUP_FAILED"
                    outcome.message = "烧录副本保护句柄关闭失败"
        return self._archive(ctx, outcome)

    @staticmethod
    def _archive(ctx, result):
        result.run_id = ctx.run_id
        try:
            with (Path(ctx.path) / "flash_result.json").open("w", encoding="utf-8") as stream:
                json.dump(result.to_dict(), stream, ensure_ascii=False, indent=2)
        except (OSError, ValueError, TypeError) as exc:
            result.data["archive_error"] = str(exc)
            if result.success:
                result.success, result.error_code, result.message = False, "ARCHIVE_FAILED", "烧录结果归档失败"
        return result

    @staticmethod
    def _record(log_path, data):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write("\n" + json.dumps(data, ensure_ascii=False, default=str) + "\n")
