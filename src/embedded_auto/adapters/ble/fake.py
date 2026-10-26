"""固定 profile 驱动的离线外设；不读取 suite 或执行 profile 中的代码。"""
import asyncio
from collections.abc import Callable
from copy import deepcopy
import inspect
from uuid import uuid4

from embedded_auto.ble.models import Candidate, Characteristic, Service
from embedded_auto.core.errors import ToolError


class FakeBackend:
    def __init__(self, profile: dict, operation_timeout_s: float = 2, *, faults: dict | None = None):
        self.profile = deepcopy(profile)
        self.operation_timeout_s = operation_timeout_s
        self.faults = dict(faults or {})
        self.calls: list[tuple] = []
        self.on_disconnect: Callable[[str], None] | None = None
        self._candidate_id: str | None = None
        self._connected = False
        self._services: list[Service] = []
        self._entries: dict[str, tuple[int, int]] = {}
        self._subscriptions: dict[str, tuple[str, Callable[[bytes], None]]] = {}
        self._values = {
            (si, ci): bytes.fromhex(char.get("initial_hex", ""))
            for si, service in enumerate(self.profile["services"])
            for ci, char in enumerate(service["characteristics"])
        }

    async def _before(self, stage: str, *args, timeout_s: float | None = None):
        self.calls.append((stage, *args))
        budget = self.operation_timeout_s if timeout_s is None else timeout_s
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        try:
            async with asyncio.timeout_at(deadline):
                fault = self.faults.get(stage)
                if isinstance(fault, Exception):
                    raise fault
                if fault is not None:
                    result = fault()
                    if inspect.isawaitable(result):
                        await result
        except ToolError:
            raise
        except TimeoutError as exc:
            data = {
                "stage": stage, "timeout_scope": "local", "local_budget_s": budget,
                "local_deadline": deadline, "observed_at": loop.time(),
            }
            if stage == "write":
                data["write_effect"] = "unknown"
            raise ToolError("BLE_OPERATION_TIMEOUT", f"{stage} timed out", data) from exc
        except Exception as exc:
            data = {"stage": stage, "exception_type": type(exc).__name__}
            if stage == "write":
                data["write_effect"] = "unknown"
            code = "BLE_CONNECT_FAILED" if stage == "connect" else "BLE_GATT_FAILED"
            raise ToolError(code, str(exc), data) from exc

    async def scan(self, timeout_s: float) -> list[Candidate]:
        self._candidate_id = None
        await self._before("scan", timeout_s)
        self._candidate_id = uuid4().hex
        device = self.profile["device"]
        return [Candidate(
            self._candidate_id, device["id"], device.get("name"), device.get("rssi"),
            tuple(service["uuid"] for service in self.profile["services"]),
        )]

    async def connect(self, candidate_id: str, timeout_s: float) -> None:
        if self._candidate_id is None or candidate_id != self._candidate_id:
            raise ToolError("BLE_DEVICE_NOT_FOUND", "Candidate is not from the latest scan")
        if self._connected:
            raise ToolError("BLE_CONNECT_FAILED", "Already connected")
        await self._before("connect", candidate_id, timeout_s, timeout_s=timeout_s)
        epoch = uuid4().hex
        self._entries.clear()
        self._services.clear()
        self._subscriptions.clear()
        handle = 0
        for si, service in enumerate(self.profile["services"]):
            handle += 1
            service_handle = handle
            chars = []
            for ci, spec in enumerate(service["characteristics"]):
                handle += 1
                ref = f"{epoch}:{handle}"
                self._entries[ref] = (si, ci)
                chars.append(Characteristic(
                    ref, service["uuid"], spec["uuid"], handle, tuple(spec["properties"]),
                    spec.get("max_write_with_response", 512),
                    spec.get("max_write_without_response", 20),
                ))
            self._services.append(Service(service["uuid"], service_handle, tuple(chars)))
        self._connected = True

    def _require_connected(self):
        if not self._connected:
            raise ToolError("BLE_DISCONNECTED", "No active Fake BLE connection")

    def _resolve(self, ref: str) -> tuple[int, int]:
        self._require_connected()
        if ref not in self._entries:
            raise ToolError("BLE_CHARACTERISTIC_NOT_FOUND", "Unknown or stale characteristic ref")
        return self._entries[ref]

    async def discover(self) -> list[Service]:
        self._require_connected()
        await self._before("discover")
        return list(self._services)

    async def read(self, characteristic_ref: str) -> bytes:
        key = self._resolve(characteristic_ref)
        await self._before("read", characteristic_ref)
        return self._values[key]

    async def write(self, characteristic_ref: str, data: bytes, response: bool) -> None:
        si, ci = self._resolve(characteristic_ref)
        data = bytes(data)
        await self._before("write", characteristic_ref, data, response)
        behavior = self.profile["services"][si]["characteristics"][ci].get("on_write", {})
        if behavior.get("store", False):
            self._values[(si, ci)] = data
        if behavior.get("emit_notification", False):
            # 故意在 write 返回之前回调，验证调用方不会丢失早到通知。
            self.emit(characteristic_ref, data)

    async def subscribe(self, characteristic_ref: str, callback: Callable[[bytes], None]) -> str:
        si, ci = self._resolve(characteristic_ref)
        spec = self.profile["services"][si]["characteristics"][ci]
        if "notify" not in spec["properties"]:
            raise ToolError("BLE_CAPABILITY_UNSUPPORTED", "Characteristic does not support notify")
        token = uuid4().hex
        self._subscriptions[token] = (characteristic_ref, callback)
        await self._before("subscribe", characteristic_ref)
        return token

    async def unsubscribe(self, token: str) -> None:
        self._require_connected()
        if token not in self._subscriptions:
            raise ToolError("BLE_GATT_FAILED", "Unknown or stale subscription token",
                            {"stage": "unsubscribe"})
        await self._before("unsubscribe", token)
        self._subscriptions.pop(token, None)

    def emit(self, characteristic_ref: str, data: bytes) -> None:
        self._resolve(characteristic_ref)
        for token, (ref, callback) in list(self._subscriptions.items()):
            if ref == characteristic_ref and token in self._subscriptions and self._connected:
                callback(bytes(data))

    def _invalidate(self):
        self._connected = False
        self._entries.clear()
        self._services.clear()
        self._subscriptions.clear()

    def simulate_disconnect(self, reason: str = "BLE_DISCONNECTED") -> None:
        was_connected = self._connected
        self._invalidate()
        if was_connected and self.on_disconnect is not None:
            self.on_disconnect(reason)

    async def disconnect(self) -> None:
        self._invalidate()
        await self._before("disconnect")
