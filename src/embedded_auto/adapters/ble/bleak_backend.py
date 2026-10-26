"""仅使用 Bleak 3.0.2 公开 API 的薄适配器，不改变系统蓝牙配置。"""
import asyncio
from collections.abc import Callable
from importlib import import_module
import sys
from uuid import uuid4

from embedded_auto.ble.models import Candidate, Characteristic, Service
from embedded_auto.core.errors import ToolError


class BleakBackend:
    def __init__(self, operation_timeout_s: float = 2, *, bleak_module=None):
        if bleak_module is None:
            try:
                bleak_module = import_module("bleak")
            except ImportError as exc:
                raise ToolError(
                    "BLE_DEPENDENCY_MISSING", str(exc),
                    {"exception_type": type(exc).__name__, "platform": sys.platform},
                ) from exc
        self._bleak = bleak_module
        self.operation_timeout_s = operation_timeout_s
        self.on_disconnect: Callable[[str], None] | None = None
        self._candidates: dict[str, object] = {}
        self._client = None
        self._scanner = None
        self._scan_stop_lock = asyncio.Lock()
        self._connected = False
        self._disconnect_seen = False
        self._closing = False
        self._epoch = ""
        self._characteristics: dict[str, object] = {}
        self._subscriptions: dict[str, object] = {}

    def _error(self, exc: Exception, stage: str) -> ToolError:
        if isinstance(exc, ToolError):
            return exc
        data = {
            "stage": stage,
            "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "platform": sys.platform,
        }
        for attr in ("winerror", "hresult", "errno", "dbus_error", "dbus_error_details"):
            value = getattr(exc, attr, None)
            if value is not None:
                data[attr] = value
        # ATT_ERROR_RSP 的公开异常类型是远端拒绝证据，不检查错误英文文本。
        remote_type = getattr(
            getattr(self._bleak, "exc", None), "BleakGATTProtocolError", (),
        )
        if isinstance(exc, remote_type):
            code = "BLE_REMOTE_REJECTED"
            data["att_code"] = int(exc.code)
        else:
            code = "BLE_CONNECT_FAILED" if stage == "connect" else "BLE_GATT_FAILED"
        if stage == "write":
            data["write_effect"] = "unknown"
        return ToolError(code, str(exc), data)

    async def _call(self, stage: str, action, timeout_s: float | None = None):
        budget = self.operation_timeout_s if timeout_s is None else timeout_s
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        try:
            async with asyncio.timeout_at(deadline):
                return await action()
        except ToolError:
            raise
        except TimeoutError as exc:
            data = {
                "stage": stage, "timeout_scope": "local",
                "local_budget_s": budget, "local_deadline": deadline,
                "observed_at": loop.time(),
            }
            if stage == "write":
                data["write_effect"] = "unknown"
            raise ToolError("BLE_OPERATION_TIMEOUT", f"{stage} timed out", data) from exc
        except Exception as exc:
            raise self._error(exc, stage) from exc

    async def scan(self, timeout_s: float) -> list[Candidate]:
        self._candidates.clear()
        if self._scanner is not None:
            raise ToolError("BLE_GATT_FAILED", "Previous scanner needs cleanup",
                            {"stage": "scan_start"})
        try:
            scanner = self._bleak.BleakScanner()
        except Exception as exc:
            raise self._error(exc, "scan_start") from exc
        self._scanner = scanner
        stopped = False
        primary = None
        try:
            await self._call("scan_start", scanner.start)
            # timeout_s 只是采样窗口，届满本身不是 BLE_OPERATION_TIMEOUT。
            await asyncio.sleep(timeout_s)
            await self._stop_scanner(scanner)
            stopped = True
            self._scanner = None
            candidates = []
            for device, adv in scanner.discovered_devices_and_advertisement_data.values():
                candidate_id = uuid4().hex
                self._candidates[candidate_id] = device
                candidates.append(Candidate(
                    candidate_id, device.address, adv.local_name or device.name,
                    adv.rssi, tuple(adv.service_uuids),
                ))
            return candidates
        except BaseException as exc:
            self._candidates.clear()
            primary = self._error(exc, "scan") if isinstance(exc, Exception) else exc
            if primary is exc:
                raise
            raise primary from exc
        finally:
            if not stopped:
                try:
                    await self._stop_scanner(scanner)
                    self._scanner = None
                except Exception as cleanup:
                    if isinstance(primary, ToolError):
                        primary.data.setdefault("cleanup", []).append({
                            "code": cleanup.code, "message": str(cleanup),
                            "data": cleanup.data,
                        })
                    elif primary is not None:
                        primary.add_note(f"scan cleanup failed: {cleanup}")
                    else:
                        raise

    async def _stop_scanner(self, scanner):
        async def stop_owned():
            # 扫描 finally 与 disconnect 共用所有权，不能并发调用 native stop。
            async with self._scan_stop_lock:
                if self._scanner is scanner:
                    await scanner.stop()
                    self._scanner = None
        # 等待锁也消耗本次控制调用预算，不另造不受登记的后台清理任务。
        await self._call("scan_stop", stop_owned)

    def _invalidate(self):
        self._connected = False
        self._characteristics.clear()
        self._subscriptions.clear()

    def _disconnected(self, client):
        # 旧客户端排队的回调不能影响新连接；预期退出也不能伪报意外断连。
        if client is not self._client or self._closing:
            return
        self._disconnect_seen = True
        was_connected = self._connected
        self._invalidate()
        if was_connected and self.on_disconnect is not None:
            self.on_disconnect("BLE_DISCONNECTED")

    async def connect(self, candidate_id: str, timeout_s: float) -> None:
        if candidate_id not in self._candidates:
            raise ToolError("BLE_DEVICE_NOT_FOUND", "Candidate is not from the latest scan")
        if self._client is not None:
            raise ToolError("BLE_CONNECT_FAILED", "Previous client needs disconnect")
        self._closing = False
        self._disconnect_seen = False
        self._epoch = uuid4().hex
        try:
            self._client = self._bleak.BleakClient(
                self._candidates[candidate_id], disconnected_callback=self._disconnected,
            )
        except Exception as exc:
            raise self._error(exc, "connect") from exc
        # 保留 client，即使连接部分失败也由 disconnect 尝试释放。
        await self._call("connect", self._client.connect, timeout_s)
        if self._disconnect_seen:
            raise ToolError("BLE_DISCONNECTED", "Disconnected while connecting",
                            {"stage": "connect"})
        self._connected = True

    def _require_connected(self):
        if not self._connected:
            raise ToolError("BLE_DISCONNECTED", "No active BLE connection")

    def _resolve(self, ref: str):
        self._require_connected()
        if ref not in self._characteristics:
            raise ToolError("BLE_CHARACTERISTIC_NOT_FOUND", "Unknown or stale characteristic ref")
        return self._characteristics[ref]

    async def discover(self) -> list[Service]:
        self._require_connected()

        async def collect():
            services = []
            for service in self._client.services:
                characteristics = []
                for char in service.characteristics:
                    ref = f"{self._epoch}:{char.handle}"
                    self._characteristics[ref] = char
                    characteristics.append(Characteristic(
                        ref, service.uuid, char.uuid, char.handle, tuple(char.properties),
                        512, getattr(char, "max_write_without_response_size", None),
                    ))
                services.append(Service(service.uuid, service.handle, tuple(characteristics)))
            return services

        return await self._call("discover", collect)

    async def read(self, characteristic_ref: str) -> bytes:
        char = self._resolve(characteristic_ref)
        data = await self._call(
            "read", lambda: self._client.read_gatt_char(char, use_cached=False),
        )
        return bytes(data)

    async def write(self, characteristic_ref: str, data: bytes, response: bool) -> None:
        char = self._resolve(characteristic_ref)
        await self._call(
            "write", lambda: self._client.write_gatt_char(char, data, response=response),
        )

    async def subscribe(self, characteristic_ref: str, callback: Callable[[bytes], None]) -> str:
        char = self._resolve(characteristic_ref)
        if "notify" not in char.properties:
            raise ToolError("BLE_CAPABILITY_UNSUPPORTED", "Characteristic does not support notify")
        token = uuid4().hex
        client = self._client
        # 先登记再 await：支持早到回调；部分失败时保留注册，disconnect 会统一释放。
        self._subscriptions[token] = char

        def notified(_sender, data):
            if self._connected and client is self._client and token in self._subscriptions:
                callback(bytes(data))

        await self._call("subscribe", lambda: client.start_notify(char, notified))
        return token

    async def unsubscribe(self, token: str) -> None:
        self._require_connected()
        if token not in self._subscriptions:
            raise ToolError("BLE_GATT_FAILED", "Unknown or stale subscription token",
                            {"stage": "unsubscribe"})
        char = self._subscriptions[token]
        await self._call("unsubscribe", lambda: self._client.stop_notify(char))
        self._subscriptions.pop(token, None)

    async def disconnect(self) -> None:
        self._closing = True
        self._invalidate()
        errors = []
        if self._scanner is not None:
            try:
                await self._stop_scanner(self._scanner)
                self._scanner = None
            except Exception as exc:
                errors.append(exc)
        if self._client is not None:
            try:
                # 不依赖 is_connected，部分连接失败也总尝试 disconnect。
                await self._call("disconnect", self._client.disconnect)
                self._client = None
            except Exception as exc:
                errors.append(exc)
        if errors:
            error = errors[0]
            error.data["cleanup_errors"] = [
                {"code": item.code, "message": str(item), "data": dict(item.data)}
                for item in errors[1:]
            ]
            raise error
