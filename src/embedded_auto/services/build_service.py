"""Build once, prove freshness, and publish an immutable run-local BIN snapshot."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import time

from embedded_auto.adapters.build import GCCBackend, IARBackend
from embedded_auto.core.errors import ToolError
from embedded_auto.core.models import OperationResult
from embedded_auto.core.process import ProcessRunner


def _fingerprint(path: Path) -> dict:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    stat = path.stat()
    return {"sha256": digest, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def validate_artifact(image: Path, manifest: dict, config: dict) -> dict:
    """Fail closed before hardware access; standalone callers load the sibling manifest.

    This is integrity/identity checking, not an authenticity signature. Manifests
    and firmware must come from a trusted build/run directory.
    """
    image = Path(image)
    if not isinstance(manifest, dict) or not manifest:
        raise ToolError("ARTIFACT_MANIFEST_MISSING", "A build artifact manifest is required")
    required = ("run_id", "target_id", "exact_part_number", "address", "address_source",
                "sha256", "size", "build_success", "source_path", "snapshot_path",
                "build_started_at_ns", "build_finished_at_ns", "source_mtime_ns", "command")
    missing = [key for key in required if key not in manifest or manifest[key] is None]
    if missing:
        raise ToolError("ARTIFACT_INVALID", "Incomplete artifact metadata", {"missing": missing})
    if manifest["build_success"] is not True:
        raise ToolError("ARTIFACT_INVALID", "Manifest does not attest a successful build")
    target, flash = config["target"], config["flash"]
    if (not isinstance(target.get("id"), str) or not target["id"].strip() or
        not isinstance(target.get("exact_part_number"), str) or not target["exact_part_number"].strip() or
        any("TODO" in target[key].upper() for key in ("id", "exact_part_number")) or
        manifest["target_id"] != target["id"] or
        manifest["exact_part_number"] != target["exact_part_number"]):
        raise ToolError("ARTIFACT_TARGET_MISMATCH", "Artifact target differs from the explicitly configured target")
    address = flash.get("address")
    if type(address) is not int or type(manifest["address"]) is not int or manifest["address"] != address:
        raise ToolError("ARTIFACT_ADDRESS_MISMATCH", "Artifact address differs from configured address")
    if (not isinstance(flash.get("address_source"), str) or not flash["address_source"].strip() or
        "TODO" in flash["address_source"].upper() or
        manifest["address_source"] != flash["address_source"]):
        raise ToolError("ARTIFACT_ADDRESS_MISMATCH", "Address provenance is missing or mismatched")
    if (not isinstance(manifest["run_id"], str) or not manifest["run_id"].strip() or
        not isinstance(manifest["command"], list) or not manifest["command"] or
        not all(isinstance(arg, str) and arg for arg in manifest["command"]) or
        not all(type(manifest[key]) is int and manifest[key] > 0 for key in
                ("build_started_at_ns", "build_finished_at_ns", "source_mtime_ns")) or
        manifest["build_finished_at_ns"] < manifest["build_started_at_ns"] or
        not isinstance(manifest["sha256"], str) or len(manifest["sha256"]) != 64 or
        any(char not in "0123456789abcdef" for char in manifest["sha256"])):
        raise ToolError("ARTIFACT_INVALID", "Invalid build identity/timing/hash metadata")
    for key in ("source_path", "snapshot_path"):
        if not isinstance(manifest[key], str) or not Path(manifest[key]).is_absolute():
            raise ToolError("ARTIFACT_INVALID", "Artifact paths must be absolute")
    if image.resolve() != Path(manifest["snapshot_path"]).resolve():
        raise ToolError("ARTIFACT_INVALID", "Image is not the manifest snapshot")
    if image.suffix.lower() != ".bin" or Path(manifest["source_path"]).suffix.lower() != ".bin":
        raise ToolError("ARTIFACT_INVALID", "Only BIN images are allowed")
    try:
        if not image.is_file():
            raise ToolError("ARTIFACT_INVALID", "BIN image does not exist")
        actual = _fingerprint(image)
    except OSError as exc:
        raise ToolError("ARTIFACT_INVALID", str(exc)) from exc
    if type(manifest["size"]) is not int or manifest["size"] <= 0 or actual["size"] <= 0:
        raise ToolError("ARTIFACT_INVALID", "BIN image must be nonempty")
    if actual["sha256"] != manifest["sha256"] or actual["size"] != manifest["size"]:
        raise ToolError("ARTIFACT_HASH_MISMATCH", "BIN hash or size differs from manifest", actual)
    start, size = flash.get("region_start"), flash.get("region_size")
    if (type(start) is not int or type(size) is not int or start < 0 or size <= 0 or
        address < start or address + actual["size"] > start + size or
        address < 0 or address + actual["size"] > 2**32):
        raise ToolError("ARTIFACT_OUT_OF_RANGE", "BIN is outside the configured memory region")
    return {**manifest, "image": str(image.resolve()), "sha256": actual["sha256"], "size": actual["size"]}


class BuildService:
    def __init__(self, runner=None):
        self.runner = runner if runner is not None else ProcessRunner()

    def build(self, config, ctx) -> OperationResult:
        started = time.monotonic()
        log_path = Path(ctx.path) / "build.log"
        data = {"run_id": ctx.run_id, "build_success": False}
        try:
            ctx.check()
            Path(ctx.path).mkdir(parents=True, exist_ok=True)
            build = config["build"]
            backend = {"gcc": GCCBackend, "iar": IARBackend}.get(build.get("backend"))
            if backend is None:
                raise ToolError("BUILD_CONFIG_INVALID", "Unknown build backend")
            command = backend().command(config)
            source = Path(build["artifact"])
            cwd = Path(build["cwd"])
            if not source.is_absolute() or source.suffix.lower() != ".bin" or not cwd.is_absolute():
                raise ToolError("BUILD_CONFIG_INVALID", "Build cwd and BIN artifact paths must be absolute")
            snapshot = Path(ctx.path).resolve() / "firmware.bin"
            manifest_path = Path(ctx.path).resolve() / "artifact_manifest.json"
            if snapshot.exists() or manifest_path.exists() or source.resolve() == snapshot:
                raise ToolError("ARTIFACT_RUN_CONFLICT", "Run snapshot/manifest already exists; use a new run directory")
            before = _fingerprint(source) if source.exists() else None
            build_started_at_ns = time.time_ns()
            data.update(command=command, source_path=str(source), artifact_before=before,
                        build_started_at_ns=build_started_at_ns, cwd=str(cwd),
                        configuration=build.get("configuration"))
            ctx.event("BUILD_STARTED", command=command, source_path=str(source))
            result = self.runner.run(command, cwd, build.get("timeout_s", 60), ctx, log_path)
            data.update(returncode=result.returncode, build_finished_at_ns=time.time_ns())
            if result.returncode != 0:
                raise ToolError("BUILD_FAILED", "Build command returned a nonzero exit code", data)
            if not source.is_file():
                raise ToolError("ARTIFACT_MISSING", "Build did not produce the configured BIN", data)
            after = _fingerprint(source)
            data["artifact_after"] = after
            if after["size"] <= 0:
                raise ToolError("ARTIFACT_EMPTY", "Build produced an empty BIN", data)
            if after == before or after["mtime_ns"] < build_started_at_ns:
                raise ToolError("ARTIFACT_STALE", "BIN was not updated by this build", data)
            ctx.check()
            with source.open("rb") as src, snapshot.open("xb") as dst:
                shutil.copyfileobj(src, dst)
            copied = _fingerprint(snapshot)
            if (_fingerprint(source) != after or copied["sha256"] != after["sha256"] or
                copied["size"] != after["size"]):
                raise ToolError("ARTIFACT_CHANGED", "BIN changed while creating the snapshot", data)
            manifest = {"run_id": ctx.run_id, "target_id": config["target"]["id"],
                        "exact_part_number": config["target"]["exact_part_number"],
                        "address": config["flash"]["address"],
                        "address_source": config["flash"]["address_source"],
                        "sha256": copied["sha256"], "size": copied["size"],
                        "build_success": True, "source_path": str(source.resolve()),
                        "snapshot_path": str(snapshot), "source_mtime_ns": after["mtime_ns"],
                        "build_started_at_ns": build_started_at_ns,
                        "build_finished_at_ns": data["build_finished_at_ns"],
                        "command": command, "cwd": str(cwd),
                        "configuration": build.get("configuration"), "returncode": result.returncode}
            validate_artifact(snapshot, manifest, config)
            ctx.check()
            with manifest_path.open("x", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
            data.update(build_success=True, image=str(snapshot), artifact=str(snapshot), snapshot_path=str(snapshot),
                        artifact_path=str(snapshot), manifest_path=str(manifest_path),
                        manifest=manifest, sha256=copied["sha256"], size=copied["size"])
            ctx.event("BUILD_SUCCEEDED", sha256=copied["sha256"], size=copied["size"])
            return self._archive(ctx, OperationResult(True, "build", duration_ms=int((time.monotonic() - started) * 1000),
                                                     log_path=log_path, data=data))
        except (ToolError, OSError, KeyError, TypeError, ValueError) as exc:
            code = exc.code if isinstance(exc, ToolError) else "BUILD_FAILED"
            code = {"PROCESS_TOOL_NOT_FOUND": "BUILD_TOOL_NOT_FOUND",
                    "PROCESS_TIMEOUT": "BUILD_TIMEOUT"}.get(code, code)
            data.update(getattr(exc, "data", None) or {})
            data["build_success"] = False
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                log.write("\n" + json.dumps({"error_code": code, "message": str(exc), "data": data},
                                             ensure_ascii=False, default=str) + "\n")
            return self._archive(ctx, OperationResult(False, "build", error_code=code, message=str(exc),
                                                     duration_ms=int((time.monotonic() - started) * 1000),
                                                     log_path=log_path, data=data))

    @staticmethod
    def _archive(ctx, result):
        result.run_id = ctx.run_id
        with (Path(ctx.path) / "build_result.json").open("w", encoding="utf-8") as stream:
            json.dump(result.to_dict(), stream, ensure_ascii=False, indent=2)
        return result
