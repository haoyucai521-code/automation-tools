"""Shell-free child process execution with durable, merged output and cleanup."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time

from .errors import ToolError


@dataclass
class ProcessResult:
    returncode: int
    output: str
    duration_ms: int
    command: list[str]
    log_path: Path
    pid: int
    started_at_ns: int


class ProcessRunner:
    """Output goes to disk, never an undrained subprocess.PIPE.

    on_started receives the Popen instance, after the OS process has started.
    A failing callback, cancellation or interrupt terminates and waits for the
    child tree before the exception is reported. Hard OS termination cannot be
    intercepted by Python.
    """

    @staticmethod
    def _stop_tree(process: subprocess.Popen) -> None:
        failures = []
        if os.name == "nt":
            try:
                killed = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    timeout=10, check=False,
                )
                if killed.returncode and process.poll() is None:
                    failures.append(killed.stdout.decode(errors="replace"))
            except Exception as exc:
                failures.append(f"taskkill: {exc}")
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception as exc:
                failures.append(f"killpg: {exc}")
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        except Exception as exc:
            failures.append(f"wait: {exc}")
        if failures:
            raise ToolError("PROCESS_CLEANUP_FAILED", "; ".join(failures),
                            {"pid": process.pid, "cleanup_errors": failures})

    def run(self, command, cwd, timeout_s, ctx, log_path, on_started=None) -> ProcessResult:
        command = list(command)
        log_path = Path(log_path)
        started = time.monotonic()
        started_at_ns = time.time_ns()
        process = None
        log_path.parent.mkdir(parents=True, exist_ok=True)
        base = {"command": command, "cwd": str(cwd), "log_path": str(log_path),
                "started_at_ns": started_at_ns}
        try:
            if not command or not all(isinstance(arg, str) and arg for arg in command):
                raise ToolError("PROCESS_COMMAND_INVALID", "Command must be a nonempty list of strings")
            if not math.isfinite(float(timeout_s)) or timeout_s <= 0:
                raise ToolError("PROCESS_TIMEOUT", "Process has no time remaining")
            ctx.check()
            budget = min(float(timeout_s), float(ctx.remaining))
            if budget <= 0:
                raise ToolError("PROCESS_TIMEOUT", "Process has no time remaining")
            deadline = time.monotonic() + budget
            with log_path.open("ab", buffering=0) as log:
                log.write(("\n" + json.dumps(base, ensure_ascii=False) + "\n").encode("utf-8"))
                output_offset = log.tell()
                options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                           if os.name == "nt" else {"start_new_session": True})
                process = subprocess.Popen(
                    command, cwd=str(cwd), shell=False, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, **options,
                )
                if on_started is not None:
                    on_started(process)
                while True:
                    ctx.check()
                    if time.monotonic() >= deadline:
                        raise ToolError("PROCESS_TIMEOUT", "Process timeout expired")
                    if process.poll() is not None:
                        break
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                returncode = process.wait()
            with log_path.open("rb") as log:
                log.seek(output_offset)
                output = log.read().decode("utf-8", errors="replace")
            duration_ms = int((time.monotonic() - started) * 1000)
            with log_path.open("ab") as log:
                log.write(("\n" + json.dumps({"returncode": returncode,
                           "duration_ms": duration_ms}) + "\n").encode())
            return ProcessResult(returncode, output, duration_ms, command, log_path,
                                 process.pid, started_at_ns)
        except BaseException as exc:
            cleanup_error = None
            if process is not None:
                try:
                    self._stop_tree(process)
                except BaseException as cleanup:
                    cleanup_error = cleanup
            data = dict(base, duration_ms=int((time.monotonic() - started) * 1000))
            if process is not None:
                data.update(pid=process.pid, returncode=process.returncode)
            if cleanup_error is not None:
                data.update(original_error=str(exc),
                            original_error_code=getattr(exc, "code", type(exc).__name__),
                            original_error_data=getattr(exc, "data", None),
                            cleanup_error=str(cleanup_error),
                            cleanup_error_data=getattr(cleanup_error, "data", None))
                error = ToolError("PROCESS_CLEANUP_FAILED", "Child process cleanup failed", data)
            elif isinstance(exc, ToolError):
                exc.data = {**(exc.data or {}), **data}
                error = exc
            elif isinstance(exc, FileNotFoundError):
                error = ToolError("PROCESS_TOOL_NOT_FOUND", str(exc), data)
            elif isinstance(exc, (KeyboardInterrupt, SystemExit)):
                error = ToolError("CANCELLED", "Process interrupted", data)
            else:
                error = ToolError("PROCESS_FAILED", str(exc), data)
            with log_path.open("ab") as log:
                log.write(("\n" + json.dumps({"error_code": error.code, "message": str(error),
                                             "data": error.data}, ensure_ascii=False, default=str)
                           + "\n").encode("utf-8"))
            raise error from (None if error is exc else exc)
