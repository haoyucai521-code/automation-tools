"""firmware 领域的最小接口契约。"""
from typing import Protocol
from pathlib import Path
from embedded_auto.core.models import OperationResult


class BuildBackend(Protocol):
    def command(self, config: dict) -> list[str]: ...


class Builder(Protocol):
    def build(self, config: dict, ctx) -> OperationResult: ...


class Programmer(Protocol):
    def erase_command(self, config: dict) -> list[str]: ...
    def command(self, config: dict, image: Path | None = None) -> list[str]: ...


class Flasher(Protocol):
    def flash(self, config, ctx, session, reset, image, manifest,
              connect_only=False) -> OperationResult: ...
