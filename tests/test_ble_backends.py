"""BLE 适配器契约测试：全部使用 Mock/Fake，不加载平台蓝牙栈。"""
import asyncio
from copy import deepcopy
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from embedded_auto.adapters.ble.bleak_backend import BleakBackend
from embedded_auto.adapters.ble.fake import FakeBackend
from embedded_auto.core.errors import ToolError


SERVICE_UUID = "12345678-1234-5678-1234-56789abc0000"
CHAR_UUID = "12345678-1234-5678-1234-56789abc0001"


def profile():
    return {
        "version": 1,
        "device": {"id": "fake:demo", "name": "Demo"},
        "services": [{
            "uuid": SERVICE_UUID,
            "characteristics": [{
                "uuid": CHAR_UUID, "properties": ["read", "write", "notify"],
                "initial_hex": "01 00",
                "on_write": {"store": True, "emit_notification": True},
            }],
        }],
    }


class MockATTError(Exception):
    def __init__(self, code=3):
        super().__init__("ATT_ERROR_RSP")
        self.code = code


class BleakHarness:
    def __init__(self):
        self.device = SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="OS name")
        self.adv = SimpleNamespace(local_name="Advertised", rssi=-42, service_uuids=[SERVICE_UUID])
        self.scanner = SimpleNamespace(
            start=AsyncMock(), stop=AsyncMock(),
            discovered_devices_and_advertisement_data={
                self.device.address: (self.device, self.adv),
            },
        )
        self.char = SimpleNamespace(
            uuid=CHAR_UUID, handle=2, properties=["read", "write", "notify"],
            max_write_without_response_size=64,
        )
        self.client = SimpleNamespace(
            connect=AsyncMock(return_value=None), disconnect=AsyncMock(return_value=None),
            read_gatt_char=AsyncMock(return_value=bytearray(b"\x01\x02")),
            write_gatt_char=AsyncMock(return_value=None), start_notify=AsyncMock(),
            stop_notify=AsyncMock(),
            services=[SimpleNamespace(uuid=SERVICE_UUID, handle=1, characteristics=[self.char])],
        )
        self.module = SimpleNamespace(
            BleakScanner=Mock(return_value=self.scanner), BleakClient=Mock(return_value=self.client),
            exc=SimpleNamespace(BleakGATTProtocolError=MockATTError),
        )
        self.backend = BleakBackend(0.05, bleak_module=self.module)

    async def ready(self):
        candidates = await self.backend.scan(0)
        await self.backend.connect(candidates[0].candidate_id, 0.05)
        return (await self.backend.discover())[0].characteristics[0].ref

    def disconnected(self, client=None):
        callback = self.module.BleakClient.call_args.kwargs["disconnected_callback"]
        callback(self.client if client is None else client)


async def blocked(*_args, **_kwargs):
    await asyncio.Event().wait()


class BleakBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_uses_public_start_stop_and_preserves_device_object(self):
        h = BleakHarness()
        candidate = (await h.backend.scan(0))[0]
        self.assertEqual(candidate.address, h.device.address)
        self.assertEqual(candidate.name, "Advertised")
        self.assertEqual(candidate.rssi, -42)
        self.assertEqual(candidate.service_uuids, (SERVICE_UUID,))
        h.scanner.start.assert_awaited_once_with()
        h.scanner.stop.assert_awaited_once_with()
        await h.backend.connect(candidate.candidate_id, 0.1)
        self.assertIs(h.module.BleakClient.call_args.args[0], h.device)
        self.assertEqual(set(h.module.BleakClient.call_args.kwargs), {"disconnected_callback"})
        h.client.connect.assert_awaited_once_with()
        await h.backend.disconnect()

    async def test_sampling_window_is_not_an_operation_timeout(self):
        h = BleakHarness()
        h.backend.operation_timeout_s = 0.01
        self.assertEqual(len(await h.backend.scan(0.03)), 1)
        h.scanner.discovered_devices_and_advertisement_data = {}
        self.assertEqual(await h.backend.scan(0), [])

    async def test_new_scan_invalidates_old_candidate_even_for_same_address(self):
        h = BleakHarness()
        old = (await h.backend.scan(0))[0].candidate_id
        new = (await h.backend.scan(0))[0].candidate_id
        self.assertNotEqual(old, new)
        with self.assertRaises(ToolError) as caught:
            await h.backend.connect(old, 0.1)
        self.assertEqual(caught.exception.code, "BLE_DEVICE_NOT_FOUND")
        h.module.BleakClient.assert_not_called()

    async def test_scan_failure_also_invalidates_old_candidate(self):
        h = BleakHarness()
        old = (await h.backend.scan(0))[0].candidate_id
        h.scanner.start.side_effect = RuntimeError("start failed")
        with self.assertRaises(ToolError) as caught:
            await h.backend.scan(0)
        self.assertEqual(caught.exception.data["stage"], "scan_start")
        with self.assertRaises(ToolError):
            await h.backend.connect(old, 0.1)

    async def test_read_copies_bytes_and_explicitly_disables_cache(self):
        h = BleakHarness()
        ref = await h.ready()
        for data in (bytearray(b"\x01\x02"), b"\x01\x02"):
            h.client.read_gatt_char.return_value = data
            result = await h.backend.read(ref)
            self.assertIs(type(result), bytes)
            self.assertEqual(result, b"\x01\x02")
            h.client.read_gatt_char.assert_awaited_with(h.char, use_cached=False)

    async def test_write_always_passes_explicit_response(self):
        h = BleakHarness()
        ref = await h.ready()
        for response in (True, False):
            await h.backend.write(ref, b"\x05", response)
            h.client.write_gatt_char.assert_awaited_with(h.char, b"\x05", response=response)
        self.assertEqual(h.client.write_gatt_char.await_count, 2)

    async def test_duplicate_service_and_characteristic_uuids_are_not_collapsed(self):
        h = BleakHarness()
        char2 = SimpleNamespace(**{**vars(h.char), "handle": 3})
        char3 = SimpleNamespace(**{**vars(h.char), "handle": 5})
        h.client.services[0].characteristics.append(char2)
        h.client.services.append(SimpleNamespace(
            uuid=SERVICE_UUID, handle=4, characteristics=[char3],
        ))
        await h.ready()
        services = await h.backend.discover()
        self.assertEqual(len(services), 2)
        chars = [char for service in services for char in service.characteristics]
        self.assertEqual(len(chars), 3)
        self.assertEqual(len({char.ref for char in chars}), 3)
        self.assertEqual({char.uuid for char in chars}, {CHAR_UUID})
        self.assertEqual(chars[0].max_write_with_response, 512)
        self.assertEqual(chars[0].max_write_without_response, 64)
        for model, raw in zip(chars, [h.char, char2, char3]):
            await h.backend.read(model.ref)
            self.assertIs(h.client.read_gatt_char.call_args.args[0], raw)

    async def test_early_notification_is_copied_and_late_callbacks_are_ignored(self):
        h = BleakHarness()
        ref = await h.ready()
        events = []
        callbacks = []

        async def start(char, callback):
            callbacks.append(callback)
            data = bytearray(b"early")
            callback(char, data)
            data[:] = b"xxxxx"
            events.append("start-return")

        h.client.start_notify.side_effect = start
        token = await h.backend.subscribe(ref, events.append)
        self.assertEqual(events, [b"early", "start-return"])
        await h.backend.unsubscribe(token)
        h.client.stop_notify.assert_awaited_once_with(h.char)
        callbacks[0](h.char, bytearray(b"late"))
        self.assertEqual(events, [b"early", "start-return"])
        await h.backend.disconnect()
        callbacks[0](h.char, bytearray(b"later"))
        self.assertEqual(events, [b"early", "start-return"])

    async def test_disconnect_is_expected_but_unexpected_disconnect_invalidates_state(self):
        h = BleakHarness()
        ref = await h.ready()
        unexpected = Mock()
        h.backend.on_disconnect = unexpected
        token = await h.backend.subscribe(ref, Mock())
        h.disconnected()
        h.disconnected()
        unexpected.assert_called_once_with("BLE_DISCONNECTED")
        with self.assertRaises(ToolError) as caught:
            await h.backend.read(ref)
        self.assertEqual(caught.exception.code, "BLE_DISCONNECTED")
        with self.assertRaises(ToolError):
            await h.backend.unsubscribe(token)
        h.client.disconnect.side_effect = h.disconnected
        await h.backend.disconnect()
        self.assertEqual(unexpected.call_count, 1)
        h.client.disconnect.assert_awaited_once()

    async def test_old_refs_tokens_and_client_callbacks_do_not_cross_connections(self):
        h = BleakHarness()
        old_ref = await h.ready()
        old_token = await h.backend.subscribe(old_ref, Mock())
        old_client = h.client
        old_callback = h.client.start_notify.call_args.args[1]
        await h.backend.disconnect()
        h.client = SimpleNamespace(**vars(old_client))
        h.module.BleakClient.return_value = h.client
        ref = await h.ready()
        self.assertNotEqual(old_ref, ref)
        unexpected = Mock()
        h.backend.on_disconnect = unexpected
        h.disconnected(old_client)
        old_callback(h.char, bytearray(b"stale"))
        unexpected.assert_not_called()
        with self.assertRaises(ToolError) as caught:
            await h.backend.read(old_ref)
        self.assertEqual(caught.exception.code, "BLE_CHARACTERISTIC_NOT_FOUND")
        with self.assertRaises(ToolError):
            await h.backend.unsubscribe(old_token)
        self.assertEqual(await h.backend.read(ref), b"\x01\x02")

    async def test_partial_connect_failure_still_disconnects(self):
        h = BleakHarness()
        candidate = (await h.backend.scan(0))[0]
        h.client.connect.side_effect = RuntimeError("partial connect")
        with self.assertRaises(ToolError) as caught:
            await h.backend.connect(candidate.candidate_id, 0.05)
        self.assertEqual(caught.exception.code, "BLE_CONNECT_FAILED")
        await h.backend.disconnect()
        h.client.disconnect.assert_awaited_once_with()

    async def test_disconnect_during_connect_is_not_overwritten_by_normal_none_return(self):
        h = BleakHarness()
        candidate = (await h.backend.scan(0))[0]
        h.client.connect.side_effect = h.disconnected
        with self.assertRaises(ToolError) as caught:
            await h.backend.connect(candidate.candidate_id, 0.05)
        self.assertEqual(caught.exception.code, "BLE_DISCONNECTED")
        await h.backend.disconnect()
        h.client.disconnect.assert_awaited_once()

    async def test_partial_subscribe_failure_still_disconnects_and_drops_callbacks(self):
        h = BleakHarness()
        ref = await h.ready()
        received = []
        callbacks = []

        async def start(char, callback):
            callbacks.append(callback)
            callback(char, bytearray(b"partial"))
            raise RuntimeError("partial registration")

        h.client.start_notify.side_effect = start
        with self.assertRaises(ToolError):
            await h.backend.subscribe(ref, received.append)
        await h.backend.disconnect()
        callbacks[0](h.char, bytearray(b"late"))
        self.assertEqual(received, [b"partial"])
        h.client.disconnect.assert_awaited_once()

    async def test_indicate_only_is_not_silently_subscribed(self):
        h = BleakHarness()
        ref = await h.ready()
        h.char.properties = ["indicate"]
        with self.assertRaises(ToolError) as caught:
            await h.backend.subscribe(ref, Mock())
        self.assertEqual(caught.exception.code, "BLE_CAPABILITY_UNSUPPORTED")
        h.client.start_notify.assert_not_awaited()

    async def test_start_timeout_stops_partial_scanner(self):
        h = BleakHarness()
        h.backend.operation_timeout_s = 0.01
        h.scanner.start.side_effect = blocked
        with self.assertRaises(ToolError) as caught:
            await h.backend.scan(0)
        self.assertEqual(caught.exception.code, "BLE_OPERATION_TIMEOUT")
        self.assertEqual(caught.exception.data["stage"], "scan_start")
        self.assertEqual(caught.exception.data["timeout_scope"], "local")
        h.scanner.stop.assert_awaited_once()

    async def test_stop_timeout_is_primary_and_finally_attempts_stop_again(self):
        h = BleakHarness()
        h.backend.operation_timeout_s = 0.01

        async def stop():
            if h.scanner.stop.await_count == 1:
                await blocked()

        h.scanner.stop.side_effect = stop
        with self.assertRaises(ToolError) as caught:
            await h.backend.scan(0)
        self.assertEqual(caught.exception.code, "BLE_OPERATION_TIMEOUT")
        self.assertEqual(caught.exception.data["stage"], "scan_stop")
        self.assertEqual(h.scanner.stop.await_count, 2)

    async def test_failed_scan_cleanup_preserves_primary_and_disconnect_can_retry(self):
        h = BleakHarness()
        h.scanner.start.side_effect = RuntimeError("primary")
        h.scanner.stop.side_effect = RuntimeError("cleanup")
        with self.assertRaises(ToolError) as caught:
            await h.backend.scan(0)
        self.assertEqual(str(caught.exception), "primary")
        self.assertEqual(caught.exception.data["cleanup"][0]["message"], "cleanup")
        h.scanner.stop.side_effect = None
        await h.backend.disconnect()
        self.assertEqual(h.scanner.stop.await_count, 2)

    async def test_scan_cancellation_still_stops_scanner(self):
        h = BleakHarness()
        started = asyncio.Event()
        h.scanner.start.side_effect = started.set
        task = asyncio.create_task(h.backend.scan(60))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        h.scanner.stop.assert_awaited_once()

    async def test_cancelled_scan_and_disconnect_share_one_serialized_scanner_stop(self):
        h = BleakHarness()
        h.backend.operation_timeout_s = 2
        started = asyncio.Event()
        stop_entered = asyncio.Event()
        disconnect_entered = asyncio.Event()
        release_stop = asyncio.Event()
        active = max_active = 0
        h.scanner.start.side_effect = started.set

        async def stop():
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            stop_entered.set()
            try:
                await release_stop.wait()
            finally:
                active -= 1

        async def disconnect():
            disconnect_entered.set()
            await h.backend.disconnect()

        h.scanner.stop.side_effect = stop
        scan_task = asyncio.create_task(h.backend.scan(60))
        disconnect_task = None
        try:
            await asyncio.wait_for(started.wait(), 2)
            scan_task.cancel()
            await asyncio.wait_for(stop_entered.wait(), 2)
            disconnect_task = asyncio.create_task(disconnect())
            # disconnect 连续运行至首个阻塞点后，本协程才恢复；不用 sleep 猜测竞争时序。
            await asyncio.wait_for(disconnect_entered.wait(), 2)
            self.assertEqual(max_active, 1)
            self.assertFalse(scan_task.done())
            self.assertFalse(disconnect_task.done())
            release_stop.set()
            results = await asyncio.wait_for(
                asyncio.gather(scan_task, disconnect_task, return_exceptions=True), 2,
            )
            self.assertIsInstance(results[0], asyncio.CancelledError)
            self.assertIsNone(results[1])
            self.assertTrue(scan_task.cancelled())
            self.assertTrue(disconnect_task.done())
            self.assertEqual((active, max_active), (0, 1))
            h.scanner.stop.assert_awaited_once_with()
        finally:
            release_stop.set()
            tasks = [task for task in (scan_task, disconnect_task) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_each_local_operation_timeout_has_stage_and_no_write_retry(self):
        for stage, method in [
            ("read", "read_gatt_char"), ("write", "write_gatt_char"),
            ("subscribe", "start_notify"), ("unsubscribe", "stop_notify"),
            ("disconnect", "disconnect"),
        ]:
            with self.subTest(stage=stage):
                h = BleakHarness()
                ref = await h.ready()
                token = await h.backend.subscribe(ref, Mock())
                h.backend.operation_timeout_s = 0.01
                getattr(h.client, method).side_effect = blocked
                actions = {
                    "read": lambda: h.backend.read(ref),
                    "write": lambda: h.backend.write(ref, b"one", True),
                    "subscribe": lambda: h.backend.subscribe(ref, Mock()),
                    "unsubscribe": lambda: h.backend.unsubscribe(token),
                    "disconnect": h.backend.disconnect,
                }
                with self.assertRaises(ToolError) as caught:
                    await actions[stage]()
                self.assertEqual(caught.exception.code, "BLE_OPERATION_TIMEOUT")
                self.assertEqual(caught.exception.data["stage"], stage)
                if stage == "write":
                    self.assertEqual(caught.exception.data["write_effect"], "unknown")
                    h.client.write_gatt_char.assert_awaited_once()

    async def test_connect_timeout_retains_client_for_cleanup(self):
        h = BleakHarness()
        candidate = (await h.backend.scan(0))[0]
        h.client.connect.side_effect = blocked
        with self.assertRaises(ToolError) as caught:
            await h.backend.connect(candidate.candidate_id, 0.01)
        self.assertEqual(caught.exception.code, "BLE_OPERATION_TIMEOUT")
        self.assertEqual(caught.exception.data["stage"], "connect")
        await h.backend.disconnect()
        h.client.disconnect.assert_awaited_once()

    async def test_remote_rejected_requires_typed_att_evidence(self):
        h = BleakHarness()
        ref = await h.ready()
        for exc, expected in [
            (RuntimeError("remote rejected: not permitted"), "BLE_GATT_FAILED"),
            (MockATTError(3), "BLE_REMOTE_REJECTED"),
        ]:
            with self.subTest(error=type(exc).__name__):
                h.client.read_gatt_char.side_effect = exc
                with self.assertRaises(ToolError) as caught:
                    await h.backend.read(ref)
                self.assertEqual(caught.exception.code, expected)
                self.assertIn(type(exc).__name__, caught.exception.data["exception_type"])
                self.assertEqual(caught.exception.data["platform"], sys.platform)
                if expected == "BLE_REMOTE_REJECTED":
                    self.assertEqual(caught.exception.data["att_code"], 3)

    async def test_platform_details_and_disconnect_failure_are_preserved(self):
        h = BleakHarness()
        ref = await h.ready()
        exc = OSError("platform failure")
        exc.winerror = 5
        exc.hresult = -1
        h.client.read_gatt_char.side_effect = exc
        with self.assertRaises(ToolError) as caught:
            await h.backend.read(ref)
        self.assertEqual(caught.exception.data["winerror"], 5)
        self.assertEqual(caught.exception.data["hresult"], -1)
        h.client.disconnect.side_effect = RuntimeError("disconnect failed")
        with self.assertRaises(ToolError):
            await h.backend.disconnect()
        h.client.disconnect.side_effect = None
        await h.backend.disconnect()
        self.assertEqual(h.client.disconnect.await_count, 2)


class DependencyTests(unittest.TestCase):
    def test_module_import_does_not_import_bleak(self):
        source = (
            "import builtins\n"
            "original = builtins.__import__\n"
            "def guarded(name, *args, **kwargs):\n"
            "    if name == 'bleak' or name.startswith('bleak.'):\n"
            "        raise AssertionError('optional dependency imported')\n"
            "    return original(name, *args, **kwargs)\n"
            "builtins.__import__ = guarded\n"
            "from embedded_auto.adapters.ble.bleak_backend import BleakBackend\n"
            "from embedded_auto.adapters.ble.fake import FakeBackend\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", source],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_dependency_is_stable_tool_error(self):
        with patch(
            "embedded_auto.adapters.ble.bleak_backend.import_module",
            side_effect=ModuleNotFoundError("No module named 'bleak'"),
        ) as imported:
            with self.assertRaises(ToolError) as caught:
                BleakBackend()
        imported.assert_called_once_with("bleak")
        self.assertEqual(caught.exception.code, "BLE_DEPENDENCY_MISSING")

    def test_injected_module_does_not_load_optional_dependency(self):
        with patch("embedded_auto.adapters.ble.bleak_backend.import_module") as imported:
            BleakHarness()
        imported.assert_not_called()


class FakeBackendTests(unittest.IsolatedAsyncioTestCase):
    async def ready(self, backend):
        candidate = (await backend.scan(0.1))[0]
        await backend.connect(candidate.candidate_id, 0.1)
        return (await backend.discover())[0].characteristics[0].ref

    async def test_write_stores_and_notifies_before_return_independent_of_expect(self):
        original = profile()
        original["suite"] = {"expect": "DE AD"}  # 即使无关断言存在，Fake 也不能读取。
        backend = FakeBackend(original)
        ref = await self.ready(backend)
        observed = []
        token = await backend.subscribe(ref, observed.append)
        await backend.write(ref, b"\x02\x03", True)
        self.assertEqual(observed, [b"\x02\x03"])
        observed.append("write-return")
        self.assertEqual(await backend.read(ref), b"\x02\x03")
        backend.emit(ref, bytearray(b"\x04"))
        self.assertEqual(observed, [b"\x02\x03", "write-return", b"\x04"])
        self.assertIn(("write", ref, b"\x02\x03", True), backend.calls)
        await backend.unsubscribe(token)
        backend.emit(ref, b"\x05")
        self.assertEqual(len(observed), 3)
        self.assertEqual(original["services"][0]["characteristics"][0]["initial_hex"], "01 00")

    async def test_profile_is_deepcopied_and_new_instance_resets_state(self):
        original = profile()
        first = FakeBackend(original)
        original["services"][0]["characteristics"][0]["initial_hex"] = "FF"
        ref = await self.ready(first)
        self.assertEqual(await first.read(ref), b"\x01\x00")
        await first.write(ref, b"\x99", False)
        second = FakeBackend(profile())
        ref2 = await self.ready(second)
        self.assertEqual(await second.read(ref2), b"\x01\x00")
        self.assertEqual(await first.read(ref), b"\x99")

    async def test_fixed_behaviors_can_disable_store_and_emit(self):
        source = profile()
        source["services"][0]["characteristics"][0]["on_write"] = {}
        backend = FakeBackend(source)
        ref = await self.ready(backend)
        events = []
        await backend.subscribe(ref, events.append)
        await backend.write(ref, b"\x07", True)
        self.assertEqual(await backend.read(ref), b"\x01\x00")
        self.assertEqual(events, [])

    async def test_new_scan_invalidates_fake_candidate(self):
        backend = FakeBackend(profile())
        old = (await backend.scan(0))[0]
        new = (await backend.scan(0))[0]
        self.assertEqual(new.address, "fake:demo")
        self.assertNotEqual(new.candidate_id, old.candidate_id)
        with self.assertRaises(ToolError) as caught:
            await backend.connect(old.candidate_id, 0.1)
        self.assertEqual(caught.exception.code, "BLE_DEVICE_NOT_FOUND")

    async def test_duplicate_uuids_and_optional_write_limits(self):
        source = profile()
        char = source["services"][0]["characteristics"][0]
        char["max_write_with_response"] = 100
        char["max_write_without_response"] = 40
        source["services"].append(deepcopy(source["services"][0]))
        backend = FakeBackend(source)
        await self.ready(backend)
        services = await backend.discover()
        self.assertEqual(len(services), 2)
        first, second = [s.characteristics[0] for s in services]
        self.assertNotEqual(first.ref, second.ref)
        self.assertEqual((first.max_write_with_response, first.max_write_without_response), (100, 40))
        await backend.write(first.ref, b"\x09", True)
        self.assertEqual(await backend.read(second.ref), b"\x01\x00")

    async def test_default_write_limits_are_fake_values_only(self):
        backend = FakeBackend(profile())
        await self.ready(backend)
        char = (await backend.discover())[0].characteristics[0]
        self.assertEqual((char.max_write_with_response, char.max_write_without_response), (512, 20))

    async def test_disconnect_and_reconnect_invalidate_refs_and_tokens(self):
        backend = FakeBackend(profile())
        ref = await self.ready(backend)
        token = await backend.subscribe(ref, Mock())
        unexpected = Mock()
        backend.on_disconnect = unexpected
        await backend.disconnect()
        unexpected.assert_not_called()
        new_ref = await self.ready(backend)
        self.assertNotEqual(ref, new_ref)
        with self.assertRaises(ToolError):
            await backend.read(ref)
        with self.assertRaises(ToolError):
            await backend.unsubscribe(token)
        backend.simulate_disconnect()
        backend.simulate_disconnect()
        unexpected.assert_called_once_with("BLE_DISCONNECTED")
        with self.assertRaises(ToolError):
            backend.emit(new_ref, b"\x01")

    async def test_faults_are_controlled_fixture_not_profile_code(self):
        source = profile()
        source["faults"] = {"read": "raise error"}
        backend = FakeBackend(source)
        ref = await self.ready(backend)
        self.assertEqual(await backend.read(ref), b"\x01\x00")
        backend.faults["read"] = RuntimeError("read failed")
        with self.assertRaises(ToolError) as caught:
            await backend.read(ref)
        self.assertEqual(caught.exception.code, "BLE_GATT_FAILED")
        self.assertEqual(caught.exception.data["stage"], "read")

    async def test_async_fault_can_emit_before_subscribe_return_then_cleanup_partial_failure(self):
        backend = FakeBackend(profile())
        ref = await self.ready(backend)
        received = []

        async def hook():
            backend.emit(ref, b"early")
            raise RuntimeError("partial subscribe")

        backend.faults["subscribe"] = hook
        with self.assertRaises(ToolError):
            await backend.subscribe(ref, received.append)
        self.assertEqual(received, [b"early"])
        await backend.disconnect()
        self.assertIn(("disconnect",), backend.calls)
        with self.assertRaises(ToolError):
            backend.emit(ref, b"late")

    async def test_fake_fault_timeout_and_disconnect_failure(self):
        backend = FakeBackend(profile(), 0.01, faults={"write": blocked})
        ref = await self.ready(backend)
        with self.assertRaises(ToolError) as caught:
            await backend.write(ref, b"\x88", True)
        self.assertEqual(caught.exception.code, "BLE_OPERATION_TIMEOUT")
        self.assertEqual(caught.exception.data["write_effect"], "unknown")
        self.assertEqual(await backend.read(ref), b"\x01\x00")
        backend.faults["disconnect"] = RuntimeError("cleanup failed")
        with self.assertRaises(ToolError):
            await backend.disconnect()
        with self.assertRaises(ToolError) as caught:
            await backend.read(ref)
        self.assertEqual(caught.exception.code, "BLE_DISCONNECTED")


if __name__ == "__main__":
    unittest.main()
