"""BLE L0/L1 领域回归；独立临时目录与 Fake，不访问硬件或应用装配。"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from embedded_auto.adapters.ble.fake import FakeBackend
from embedded_auto.ble.config import (
    authorize, has_writes, prepare_config, validate_expect, validate_profile, validate_suite,
)
from embedded_auto.ble.evidence import BleEvidence, NotificationBuffer, validate_event
from embedded_auto.ble.models import Candidate, Characteristic
from embedded_auto.ble.session import BleSession
from embedded_auto.ble.test_runner import TestRunner as BleTestRunner, assertions
from embedded_auto.core.errors import ToolError
from embedded_auto.core.run_context import RunContext


SERVICE = "12345678-1234-5678-1234-56789abc0000"
CHAR = "12345678-1234-5678-1234-56789abc0001"
OTHER_SERVICE = "12345678-1234-5678-1234-56789abc0010"
OTHER_CHAR = "12345678-1234-5678-1234-56789abc0002"


def profile():
    return {
        "version": 1, "device": {"id": "fake:demo", "name": "演示"},
        "services": [{
            "uuid": SERVICE, "characteristics": [{
                "uuid": CHAR, "properties": ["read", "write", "write-without-response", "notify"],
                "initial_hex": "01 00",
                "on_write": {"store": True, "emit_notification": True},
            }],
        }],
    }


def raw_config(root="."):
    return {
        "version": 1,
        "app": {"workspace": str(root), "runs_dir": "运行 输出", "default_timeout_s": 5},
        "ble": {
            "backend": "fake", "device": "fake:demo", "fake_profile": "外设 配置.yaml",
            "scan_timeout_s": .05, "connect_timeout_s": .2,
            "operation_timeout_s": .2, "cleanup_timeout_s": .3,
        },
    }


def read_step(expect=None, alias="echo"):
    return {
        "action": "read", "characteristic": alias,
        "expect": {"equals_hex": "01 00", "length": 2} if expect is None else expect,
    }


def write_step(data="A5 5A", response=True, alias="echo"):
    return {"action": "write", "characteristic": alias, "hex": data, "response": response}


def subscribe_step(alias="echo"):
    return {"action": "subscribe", "characteristic": alias}


def checkpoint_step(label="before", alias="echo"):
    return {"action": "checkpoint", "characteristic": alias, "id": label}


def wait_step(expect=None, label="before", alias="echo", timeout=.05):
    return {
        "action": "wait_notification", "characteristic": alias, "after": label,
        "timeout_s": timeout, "expect": {"equals_hex": "a55a"} if expect is None else expect,
    }


def suite(steps=None):
    return {
        "version": 1,
        "characteristics": {
            "echo": {"service_uuid": SERVICE, "uuid": CHAR, "required_properties": ["read"]},
        },
        "tests": [{"name": "回读", "steps": [read_step()] if steps is None else steps}],
    }


def changed(source, path, value):
    result = deepcopy(source)
    item = result
    for key in path[:-1]:
        item = item[key]
    item[path[-1]] = value
    return result


class ErrorAssertions:
    def assert_code(self, code, action, *args, **kwargs):
        with self.assertRaises(ToolError) as caught:
            action(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    async def assert_async_code(self, code, action, *args, **kwargs):
        with self.assertRaises(ToolError) as caught:
            await action(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class ConfigTests(ErrorAssertions, unittest.TestCase):
    def test_valid_inputs_are_normalized_without_mutating_sources(self):
        with tempfile.TemporaryDirectory(prefix="ble 域 ") as directory:
            root = Path(directory)
            path = root / "配置" / "ble.yaml"
            raw = raw_config(root)
            original = deepcopy(raw)
            config = prepare_config(raw, path, {"scan_timeout_s": .7, "name": "演示"})
            self.assertEqual(raw, original)
            self.assertEqual(config["ble"]["scan_timeout_s"], .7)
            self.assertEqual(Path(config["ble"]["fake_profile"]), path.parent / "外设 配置.yaml")
            self.assertEqual(Path(config["app"]["runs_dir"]), root / "运行 输出")
            self.assertEqual(config["ble"]["operation_timeout_s"], original["ble"]["operation_timeout_s"])
        source = changed(profile(), ("services", 0, "uuid"), SERVICE.upper())
        source["services"][0]["characteristics"][0]["initial_hex"] = "AA 00 FF"
        normalized = validate_profile(source)
        self.assertEqual(normalized["services"][0]["uuid"], SERVICE)
        self.assertEqual(normalized["services"][0]["characteristics"][0]["initial_hex"], "aa00ff")
        self.assertEqual(source["services"][0]["uuid"], SERVICE.upper())
        source_suite = suite([read_step({"equals_hex": "AA FF", "length": 2})])
        parsed = validate_suite(source_suite)
        self.assertEqual(parsed["tests"][0]["steps"][0]["expect"]["equals_hex"], "aaff")
        self.assertEqual(source_suite["tests"][0]["steps"][0]["expect"]["equals_hex"], "AA FF")

    def test_unknown_fields_are_rejected_at_every_config_and_profile_level(self):
        matrices = [
            (raw_config(), prepare_config, [(), ("app",), ("ble",),
                                           ("ble", "ingress_limits"), ("ble", "window_limits")]),
            (profile(), validate_profile, [(), ("device",), ("services", 0),
                                          ("services", 0, "characteristics", 0),
                                          ("services", 0, "characteristics", 0, "on_write")]),
        ]
        for source, validator, paths in matrices:
            for path in paths:
                with self.subTest(validator=validator.__name__, path=path):
                    value = deepcopy(source)
                    item = value
                    for key in path:
                        if isinstance(item, dict):
                            item = item.setdefault(key, {})
                        else:
                            item = item[key]
                    item["unknown"] = True
                    args = (Path("config.yaml"),) if validator is prepare_config else ()
                    self.assert_code("CONFIG_INVALID", validator, value, *args)
        self.assert_code("CONFIG_INVALID", prepare_config, raw_config(), Path("a.yaml"), {"timeout": 2})

    def test_missing_sections_versions_and_wrong_container_types(self):
        for source, validator, required in [
            (raw_config(), prepare_config, ("version", "app", "ble")),
            (profile(), validate_profile, ("version", "device", "services")),
            (suite(), validate_suite, ("version", "characteristics", "tests")),
        ]:
            extra = (Path("a.yaml"),) if validator is prepare_config else ()
            for key in required:
                with self.subTest(validator=validator.__name__, missing=key):
                    value = deepcopy(source)
                    del value[key]
                    self.assert_code("CONFIG_INVALID", validator, value, *extra)
            for version in (True, False, 0, 2, "1"):
                with self.subTest(validator=validator.__name__, version=version):
                    self.assert_code("CONFIG_INVALID", validator, changed(source, ("version",), version), *extra)
            self.assert_code("CONFIG_INVALID", validator, [], *extra)
        for path, invalid in [
            (("services",), {}), (("services", 0, "characteristics"), {}),
            (("services", 0, "characteristics", 0, "properties"), "read"),
            (("device", "id"), ""), (("device", "name"), " "),
        ]:
            with self.subTest(path=path):
                self.assert_code("CONFIG_INVALID", validate_profile, changed(profile(), path, invalid))

    def test_uuid_requires_full_128_bit_format_in_all_inputs(self):
        for invalid in ("180D", "12345678", "z" + SERVICE[1:], SERVICE.replace("-", ""), True, None):
            for validator, source, path, extra in [
                (prepare_config, raw_config(), ("ble", "service_uuids"), (Path("a.yaml"),)),
                (validate_profile, profile(), ("services", 0, "uuid"), ()),
                (validate_profile, profile(), ("services", 0, "characteristics", 0, "uuid"), ()),
                (validate_suite, suite(), ("characteristics", "echo", "service_uuid"), ()),
                (validate_suite, suite(), ("characteristics", "echo", "uuid"), ()),
            ]:
                with self.subTest(validator=validator.__name__, path=path, value=invalid):
                    value = [invalid] if path[-1] == "service_uuids" else invalid
                    self.assert_code("CONFIG_INVALID", validator, changed(source, path, value), *extra)

    def test_timeouts_and_capacities_reject_nonfinite_bool_and_nonpositive_values(self):
        for value in (float("nan"), float("inf"), -float("inf"), True, False, 0, -1, "2"):
            for field in ("scan_timeout_s", "connect_timeout_s", "operation_timeout_s", "cleanup_timeout_s"):
                with self.subTest(field=field, value=value):
                    raw = changed(raw_config(), ("ble", field), value)
                    self.assert_code("CONFIG_INVALID", prepare_config, raw, Path("a.yaml"))
            self.assert_code(
                "CONFIG_INVALID", prepare_config,
                changed(raw_config(), ("app", "default_timeout_s"), value), Path("a.yaml"),
            )
        for container in ("ingress_limits", "window_limits"):
            for field in ("max_events", "max_payload_bytes"):
                for value in (True, False, 0, -1, 1.5, float("nan"), "2"):
                    with self.subTest(container=container, field=field, value=value):
                        raw = raw_config()
                        raw["ble"][container] = {field: value}
                        self.assert_code("CONFIG_INVALID", prepare_config, raw, Path("a.yaml"))

    def test_hex_properties_and_profile_behavior_are_strict(self):
        char_path = ("services", 0, "characteristics", 0)
        for value in ("0", "GG", "0x00", b"\x00", True, None):
            self.assert_code(
                "CONFIG_INVALID", validate_profile, changed(profile(), (*char_path, "initial_hex"), value),
            )
        for value in (["unknown"], ["read", "read"], [True], "notify"):
            self.assert_code(
                "CONFIG_INVALID", validate_profile, changed(profile(), (*char_path, "properties"), value),
            )
        for field in ("store", "emit_notification"):
            for value in (1, "true", None):
                self.assert_code(
                    "CONFIG_INVALID", validate_profile,
                    changed(profile(), (*char_path, "on_write", field), value),
                )
        for field in ("max_write_with_response", "max_write_without_response"):
            for value in (True, 0, -1, 2.5, "20"):
                self.assert_code(
                    "CONFIG_INVALID", validate_profile, changed(profile(), (*char_path, field), value),
                )

    def test_backend_target_and_per_invocation_write_authorization(self):
        for value in ("unknown", "", True):
            self.assert_code(
                "CONFIG_INVALID", prepare_config, changed(raw_config(), ("ble", "backend"), value),
                Path("a.yaml"),
            )
        for device in ("演示", "*", "AA:BB", "fake:demo"):
            raw = raw_config()
            raw["ble"].update(backend="bleak", device=device)
            self.assert_code("CONFIG_INVALID", prepare_config, raw, Path("a.yaml"))
        raw = raw_config()
        raw["ble"].update(backend="bleak", device="aa:bb:cc:dd:ee:ff")
        config = prepare_config(raw, Path("a.yaml"))
        self.assertEqual(config["ble"]["device"], "AA:BB:CC:DD:EE:FF")
        for allowed, confirmation in [(False, None), (True, None), (True, "00:BB:CC:DD:EE:FF")]:
            self.assert_code("CONFIG_INVALID", authorize, config, True, allowed, confirmation)
        authorize(config, True, True, "aa:bb:cc:dd:ee:ff")
        authorize(config, False, False)
        self.assert_code("CONFIG_INVALID", authorize, {"ble": {"backend": "fake"}}, True, False)
        raw = raw_config()
        del raw["ble"]["fake_profile"]
        self.assert_code("CONFIG_INVALID", prepare_config, raw, Path("a.yaml"))

    def test_suite_unknown_fields_actions_aliases_rules_and_empty_cases(self):
        for path in [(), ("characteristics", "echo"), ("tests", 0), ("tests", 0, "steps", 0),
                     ("tests", 0, "steps", 0, "expect")]:
            self.assert_code("CONFIG_INVALID", validate_suite, changed(suite(), (*path, "unknown"), True))
        for value in ({}, [], None):
            self.assert_code("CONFIG_INVALID", validate_suite, changed(suite(), ("characteristics",), value))
            self.assert_code("CONFIG_INVALID", validate_suite, changed(suite(), ("tests",), value))
        for value in ([], "read", None):
            self.assert_code("CONFIG_INVALID", validate_suite, changed(suite(), ("tests", 0, "steps"), value))
        self.assert_code("CONFIG_INVALID", validate_suite, suite([{"action": "erase", "characteristic": "echo"}]))
        self.assert_code("CONFIG_INVALID", validate_suite, suite([read_step(alias="missing")]))
        self.assert_code("CONFIG_INVALID", validate_suite, suite([subscribe_step()]))
        self.assert_code("CONFIG_INVALID", validate_suite, suite([read_step({})]))
        duplicate = suite()
        duplicate["tests"].append(deepcopy(duplicate["tests"][0]))
        self.assert_code("CONFIG_INVALID", validate_suite, duplicate)

    def test_entire_suite_is_prechecked_before_any_backend_call(self):
        backend = FakeBackend(validate_profile(profile()))
        source = suite([write_step(), read_step()])
        source["tests"].append({"name": "后面的错误", "steps": [read_step({"typo_hex": "00"})]})
        self.assert_code("CONFIG_INVALID", validate_suite, source)
        self.assertTrue(has_writes(source))
        self.assertEqual(backend.calls, [])

    def test_checkpoint_order_uniqueness_and_characteristic_scope(self):
        cases = [
            [checkpoint_step(), read_step()],
            [wait_step(), read_step()],
            [subscribe_step(), wait_step(), checkpoint_step()],
            [subscribe_step(), checkpoint_step(), checkpoint_step(), wait_step()],
            [subscribe_step(), checkpoint_step(), subscribe_step("other"), wait_step(alias="other")],
            [subscribe_step(), checkpoint_step(label=""), read_step()],
        ]
        for steps in cases:
            with self.subTest(steps=steps):
                source = suite(steps)
                source["characteristics"]["other"] = {"service_uuid": SERVICE, "uuid": OTHER_CHAR}
                self.assert_code("CONFIG_INVALID", validate_suite, source)
        source = suite([subscribe_step(), checkpoint_step(), read_step()])
        source["tests"].append({"name": "不可跨用例引用", "steps": [subscribe_step(), wait_step()]})
        self.assert_code("CONFIG_INVALID", validate_suite, source)

    def test_wait_write_and_expect_types_and_empty_byte_rules(self):
        for value in (True, False, 0, -1, float("nan"), float("inf"), "1", 3):
            self.assert_code(
                "CONFIG_INVALID", validate_suite,
                suite([subscribe_step(), checkpoint_step(), wait_step(timeout=value)]),
            )
        source = suite([subscribe_step(), checkpoint_step(), wait_step()])
        del source["tests"][0]["steps"][-1]["timeout_s"]
        parsed = validate_suite(source, operation_timeout_s=.3)
        self.assertEqual(parsed["tests"][0]["steps"][-1]["timeout_s"], .3)
        for value in ("", " ", "0", "GG", None, True):
            self.assert_code("CONFIG_INVALID", validate_suite, suite([write_step(value), read_step()]))
        for value in (1, "true", None):
            self.assert_code("CONFIG_INVALID", validate_suite, suite([write_step(response=value), read_step()]))
        missing = suite([write_step(), read_step()])
        del missing["tests"][0]["steps"][0]["response"]
        self.assert_code("CONFIG_INVALID", validate_suite, missing)
        for invalid in ({"contains_hex": ""}, {"contains_hex": " "}, {"length": True},
                        {"length": -1}, {"length": 1.5}, {"equals_hex": "GG"}, {}):
            self.assert_code("CONFIG_INVALID", validate_expect, invalid)
        self.assertEqual(validate_expect({"equals_hex": "", "length": 0}), {"equals_hex": "", "length": 0})


class ByteRuleTests(unittest.TestCase):
    def test_three_rules_use_original_bytes_and_require_all_to_pass(self):
        rules = validate_expect({"equals_hex": "00 FF 80", "contains_hex": "FF 80", "length": 3})
        checked = assertions(b"\x00\xff\x80", rules)
        self.assertEqual(len(checked), 3)
        self.assertTrue(all(item["success"] for item in checked))
        mismatch = assertions(b"\x00\xff\x80", {**rules, "length": 2})
        self.assertFalse(all(item["success"] for item in mismatch))
        self.assertEqual([item["success"] for item in mismatch], [True, True, False])
        self.assertEqual(mismatch[0]["actual"], "00ff80")

    def test_empty_equals_and_zero_length_are_valid_but_nonempty_contains_does_not_match(self):
        result = assertions(b"", validate_expect({"equals_hex": "", "length": 0}))
        self.assertTrue(all(item["success"] for item in result))
        result = assertions(b"", validate_expect({"contains_hex": "00"}))
        self.assertFalse(result[0]["success"])
        self.assertFalse(assertions(b"\x00", {"equals_hex": ""})[0]["success"])


class BufferTests(ErrorAssertions, unittest.TestCase):
    def setUp(self):
        self.char = Characteristic("ref", SERVICE, CHAR, 2, ("notify",))
        self.now = 100

    def buffer(self, ingress_events=2, ingress_bytes=4, window_events=2, window_bytes=4):
        return NotificationBuffer(
            {"max_events": ingress_events, "max_payload_bytes": ingress_bytes},
            {"max_events": window_events, "max_payload_bytes": window_bytes},
            clock_ns=lambda: self.now,
        )

    def push(self, buffer, payload, *, epoch="e", generation="g", char=None):
        return buffer.push(epoch, generation, char or self.char, payload)

    def retain_all(self, buffer):
        while (event := buffer.take()) is not None:
            buffer.retain(event)

    def test_current_queue_count_and_bytes_recover_on_each_take_not_historical_total(self):
        buffer = self.buffer()
        for _ in range(6):
            self.assertTrue(self.push(buffer, b"ab"))
            self.assertTrue(self.push(buffer, b"cd"))
            self.assertEqual(buffer.stats()["current_events"], 2)
            self.assertEqual(buffer.stats()["current_payload_bytes"], 4)
            self.assertEqual(buffer.take()["payload_hex"], "6162")
            self.assertEqual(buffer.stats()["current_events"], 1)
            self.assertEqual(buffer.stats()["current_payload_bytes"], 2)
            self.assertEqual(buffer.take()["payload_hex"], "6364")
            self.assertIsNone(buffer.take())
            self.assertEqual(buffer.stats()["current_events"], 0)
            self.assertEqual(buffer.stats()["current_payload_bytes"], 0)
        self.assertEqual(buffer.stats()["accepted_total"], 12)
        self.assertEqual(buffer.stats()["rejected_total"], 0)
        self.assertEqual(buffer.stats()["discarded_total"], 0)
        buffer.check()

    def test_event_limit_and_aggregate_byte_limit_are_independent_and_sticky(self):
        for limits, data in [((1, 20), b"a"), ((10, 3), b"aa")]:
            with self.subTest(limits=limits):
                buffer = self.buffer(*limits)
                self.assertTrue(self.push(buffer, data))
                before = buffer.stats()
                self.assertFalse(self.push(buffer, data))
                after = buffer.stats()
                self.assertEqual(after["current_events"], before["current_events"])
                self.assertEqual(after["current_payload_bytes"], before["current_payload_bytes"])
                self.assertEqual(after["accepted_total"], 1)
                self.assertEqual(after["rejected_total"], 1)
                error = self.assert_code("BLE_EVENT_OVERFLOW", buffer.check)
                buffer.take()
                self.assertTrue(self.push(buffer, data))
                self.assertIs(self.assert_code("BLE_EVENT_OVERFLOW", buffer.check), error)
                buffer.close()
                self.assertIs(self.assert_code("BLE_EVENT_OVERFLOW", buffer.check), error)

    def test_single_oversize_rejection_is_atomic_for_both_container_byte_limits(self):
        for ingress_bytes, window_bytes in [(3, 10), (10, 3)]:
            with self.subTest(ingress_bytes=ingress_bytes, window_bytes=window_bytes):
                buffer = self.buffer(4, ingress_bytes, 4, window_bytes)
                mark = buffer.mark("e", "g", self.char.ref)
                self.push(buffer, b"ok")
                self.retain_all(buffer)
                before = buffer.stats()
                self.assertFalse(self.push(buffer, b"huge"))
                after = buffer.stats()
                for key in ("current_events", "current_payload_bytes", "window_events",
                            "window_payload_bytes", "accepted_total"):
                    self.assertEqual(after[key], before[key], key)
                self.assertEqual(after["rejected_total"], before["rejected_total"] + 1)
                self.assertIsNone(buffer.take())
                self.assertEqual([e["payload_hex"] for e in buffer.events_after(mark, 100)], ["6f6b"])
                self.assert_code("BLE_EVENT_OVERFLOW", buffer.check)

    def test_window_eviction_restores_count_bytes_and_expires_only_affected_checkpoints(self):
        for event_limit, byte_limit in [(2, 20), (10, 4)]:
            with self.subTest(event_limit=event_limit, byte_limit=byte_limit):
                buffer = self.buffer(2, 20, event_limit, byte_limit)
                old = buffer.mark("e", "g", self.char.ref)
                for payload in (b"aa", b"bb", b"cc"):
                    self.push(buffer, payload)
                    self.retain_all(buffer)
                self.assertEqual(buffer.stats()["window_events"], 2)
                self.assertEqual(buffer.stats()["window_payload_bytes"], 4)
                self.assertEqual(buffer.stats()["current_events"], 0)
                self.assert_code("BLE_WINDOW_EXPIRED", buffer.events_after, old, 100)
                current = buffer.mark("e", "g", self.char.ref)
                self.push(buffer, b"dd")
                self.retain_all(buffer)
                self.assertEqual([e["payload_hex"] for e in buffer.events_after(current, 100)], ["6464"])
                self.assert_code("BLE_WINDOW_EXPIRED", buffer.events_after, old, 100)
                buffer.check()

    def test_checkpoint_excludes_already_queued_event_and_preserves_callback_boundary(self):
        buffer = self.buffer(8, 100, 8, 100)
        data = bytearray(b"old")
        self.push(buffer, data)
        mark = buffer.mark("e", "g", self.char.ref)
        data[:] = b"xxx"
        self.push(buffer, b"A")
        self.push(buffer, b"B")
        self.retain_all(buffer)
        events = buffer.events_after(mark, 100)
        self.assertEqual([e["payload_hex"] for e in events], ["41", "42"])
        self.assertFalse(any(assertions(bytes.fromhex(e["payload_hex"]), {"equals_hex": "4142"})[0]["success"]
                             for e in events))
        self.assertEqual(buffer.stats()["window_payload_bytes"], 5)

    def test_epoch_generation_characteristic_and_deadline_filters_are_independent(self):
        buffer = self.buffer(10, 100, 10, 100)
        mark = buffer.mark("e", "g", self.char.ref)
        self.push(buffer, b"wrong epoch", epoch="old")
        self.push(buffer, b"wrong generation", generation="old")
        self.push(buffer, b"wrong characteristic", char=replace(self.char, ref="other", uuid=OTHER_CHAR))
        self.now = 101
        self.push(buffer, b"valid")
        self.now = 103
        self.push(buffer, b"late")
        self.retain_all(buffer)
        self.assertEqual([e["payload_hex"] for e in buffer.events_after(mark, 102)], [b"valid".hex()])

    def test_close_is_idempotent_and_late_push_does_not_revive_occupancy(self):
        buffer = self.buffer()
        mark = buffer.mark("e", "g", self.char.ref)
        self.push(buffer, b"a")
        self.retain_all(buffer)
        self.push(buffer, b"bb")
        for _ in range(3):
            buffer.close()
            self.assertIsNone(buffer.take())
            stats = buffer.stats()
            for key in ("current_events", "current_payload_bytes", "window_events", "window_payload_bytes"):
                self.assertEqual(stats[key], 0)
            self.assertEqual(stats["discarded_total"], 1)
        self.assertFalse(self.push(buffer, b"late"))
        self.assertEqual(buffer.stats()["late_total"], 1)
        self.assertEqual(buffer.stats()["accepted_total"], 2)
        self.assert_code("BLE_WINDOW_EXPIRED", buffer.events_after, mark, 100)


class ContextFixture:
    def setup_context(self):
        directory = tempfile.TemporaryDirectory(prefix="ble domain ")
        self.addCleanup(directory.cleanup)
        self.config = prepare_config(raw_config(Path(directory.name)), Path(directory.name) / "ble.yaml")
        self.ctx = RunContext(self.config, "ble-domain-test", run_label="ble-domain-test")
        self.evidence = BleEvidence(self.ctx, "fake")
        self.addCleanup(self.evidence.close)

    def events(self):
        return [json.loads(line) for line in (self.ctx.path / "ble_events.jsonl").read_text(
            encoding="utf-8",
        ).splitlines()]


class EvidenceTests(ErrorAssertions, ContextFixture, unittest.TestCase):
    def setUp(self):
        self.setup_context()

    def base_event(self, kind):
        return {
            "run_id": self.ctx.run_id, "backend": "fake", "event_id": 1,
            "event_type": kind, "timestamp": "2026-10-26T00:00:00+00:00", "monotonic_ns": 1,
        }

    def test_common_and_conditional_required_fields(self):
        char = {"epoch": "e", "service_uuid": SERVICE, "characteristic_uuid": CHAR}
        payload = {"direction": "rx", "payload_hex": "", "length": 0}
        examples = {
            "scan_start": {"scan_id": "s"},
            "scan_candidate": {"scan_id": "s", "candidate_id": "c"},
            "scan_end": {"scan_id": "s"},
            "connect_start": {"epoch": "e"},
            "connect_complete": {"epoch": "e"},
            "discover": {"epoch": "e"},
            "read_request": char,
            "read_result": {**char, **payload},
            "write_request": {**char, **payload},
            "notification": {**char, **payload, "generation": "g", "ingress_seq": 1},
            "subscribe": {**char, "generation": "g"},
            "unsubscribe": {**char, "generation": "g"},
            "checkpoint": {**char, "generation": "g", "ingress_seq": 0},
            "write_complete": {
                **char, "request_event_id": 1, "write_mode": "with-response",
                "submitted": True, "ack_scope": "ATT_response_not_business_confirmation",
            },
            "error": {"error_code": "BLE_CONNECT_FAILED", "stage": "connect", "message": "failure"},
            "cleanup": {"stage": "cleanup", "action": "disconnect", "status": "ERROR",
                        "error_code": "BLE_GATT_FAILED", "message": "failure"},
        }
        for kind, fields in examples.items():
            event = {**self.base_event(kind), **fields}
            self.assertIs(validate_event(event), event)
            for key in fields:
                with self.subTest(kind=kind, missing=key):
                    broken = dict(event)
                    del broken[key]
                    self.assert_code("LOG_WRITE_FAILED", validate_event, broken)
        # 公共字段只验证一遍；event_type 缺失也必须归入稳定日志错误，而非 AttributeError。
        complete = {**self.base_event("error"), **examples["error"]}
        for key in self.base_event("error"):
            with self.subTest(common_field=key):
                broken = dict(complete)
                del broken[key]
                self.assert_code("LOG_WRITE_FAILED", validate_event, broken)

    def test_empty_payload_is_data_while_early_control_and_error_events_omit_unknown_context(self):
        start = self.evidence.emit("scan_start", scan_id="s")
        error = self.evidence.emit("error", error_code="BLE_CONNECT_FAILED", stage="connect", message="early")
        data = self.evidence.emit(
            "read_result", epoch="e", service_uuid=SERVICE, characteristic_uuid=CHAR,
            direction="rx", payload_hex="", length=0,
        )
        for event in (start, error):
            for key in ("payload_hex", "length", "epoch", "service_uuid", "characteristic_uuid"):
                self.assertNotIn(key, event)
        self.assertEqual(data["payload_hex"], "")
        self.assertEqual(data["length"], 0)
        self.assertEqual(self.events(), [start, error, data])
        self.assertEqual([e["event_id"] for e in self.events()], [1, 2, 3])

    def test_hex_length_type_and_consistency_are_checked(self):
        base = {
            **self.base_event("notification"), "epoch": "e", "service_uuid": SERVICE,
            "characteristic_uuid": CHAR, "generation": "g", "ingress_seq": 1, "direction": "rx",
        }
        for payload, length in [("GG", 1), ("00", 0), ("", 1), ("00", True), ("00", 1.0),
                                (None, 0), (b"00", 1)]:
            with self.subTest(payload=payload, length=length):
                self.assert_code(
                    "LOG_WRITE_FAILED", validate_event, {**base, "payload_hex": payload, "length": length},
                )

    def test_invalid_or_failed_write_is_sticky_and_never_silently_dropped(self):
        error = self.assert_code("LOG_WRITE_FAILED", self.evidence.emit, "read_result")
        self.assertIs(self.assert_code("LOG_WRITE_FAILED", self.evidence.emit, "scan_end"), error)
        self.assertEqual(self.events(), [])

    def test_disk_flush_failure_remains_a_diagnostic_error(self):
        with patch.object(self.evidence, "_file", SimpleNamespace(
            write=lambda text: None, flush=lambda: (_ for _ in ()).throw(OSError("disk failure")),
        )):
            error = self.assert_code("LOG_WRITE_FAILED", self.evidence.emit, "scan_start", scan_id="s")
        self.assertIn("disk failure", str(error))
        self.assertIs(self.assert_code("LOG_WRITE_FAILED", self.evidence.emit, "scan_end"), error)


async def block_forever():
    await asyncio.Event().wait()


class SessionTests(ErrorAssertions, ContextFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.make_session()

    def make_session(self, source=None, faults=None):
        self.setup_context()
        # Fake 的操作预算刻意大于领域预算，领域超时测试不与 Fake 内部计时赛跑。
        self.backend = FakeBackend(validate_profile(source or profile()), 5, faults=faults)
        self.session = BleSession(self.backend, self.config, self.ctx, self.evidence)
        self.addAsyncCleanup(self.session.close)
        return self.session

    async def ready(self):
        await self.session.connect(await self.session.scan())
        return self.session.resolve({"service_uuid": SERVICE, "uuid": CHAR})

    async def run_suite(self, source):
        parsed = validate_suite(source, self.config["ble"]["operation_timeout_s"])
        await self.ready()
        runner = BleTestRunner(self.session, parsed)
        return await runner.run()

    def calls(self, stage):
        return [call for call in self.backend.calls if call[0] == stage]

    async def test_fake_read_write_and_early_notification_complete_a_real_suite(self):
        source = suite([
            read_step({"equals_hex": "0100", "contains_hex": "00", "length": 2}),
            subscribe_step(), checkpoint_step(), write_step(), wait_step(),
            read_step({"equals_hex": "a55a", "length": 2}),
        ])
        runner = await self.run_suite(source)
        self.assertIsNone(runner.error)
        self.assertEqual(runner.verdict, "PASS")
        self.assertEqual(runner.cases[0]["status"], "COMPLETED")
        self.assertEqual(runner.cases[0]["verdict"], "PASS")
        self.assertEqual(len(self.calls("write")), 1)
        self.assertIs(self.calls("write")[0][-1], True)
        raw = self.events()
        notification = next(e for e in raw if e["event_type"] == "notification")
        complete = next(e for e in raw if e["event_type"] == "write_complete")
        self.assertLess(notification["event_id"], complete["event_id"])
        self.assertEqual(notification["payload_hex"], "a55a")
        self.assertEqual(runner.cases[0]["steps"][4]["matched_event_id"], notification["event_id"])
        cleanup = await self.session.close()
        self.assertTrue(all(item["success"] for item in cleanup))
        self.assertEqual(len(self.calls("unsubscribe")), 1)
        self.assertEqual(len(self.calls("disconnect")), 1)
        self.assertEqual(self.session.buffer.stats()["current_payload_bytes"], 0)
        self.assertEqual(self.session.buffer.stats()["window_payload_bytes"], 0)
        self.assertEqual(self.session.buffer.stats()["discarded_total"], 0)
        self.assertFalse(self.session._tasks)

    async def test_wrong_expect_stops_before_next_write_and_reports_original_bytes(self):
        runner = await self.run_suite(suite([read_step({"equals_hex": "DE AD"}), write_step()]))
        self.assertEqual(runner.error.code, "BLE_ASSERTION_FAILED")
        self.assertEqual(runner.verdict, "FAIL")
        first, second = runner.cases[0]["steps"]
        self.assertEqual(first["actual_hex"], "0100")
        self.assertEqual((first["status"], first["verdict"]), ("COMPLETED", "FAIL"))
        self.assertEqual((second["status"], second["verdict"]), ("SKIP", "NOT_EVALUATED"))
        self.assertEqual(self.calls("write"), [])

    async def test_empty_read_is_an_actual_event_not_a_missing_value(self):
        source = changed(profile(), ("services", 0, "characteristics", 0, "initial_hex"), "")
        self.make_session(source)
        runner = await self.run_suite(suite([read_step({"equals_hex": "", "length": 0})]))
        self.assertEqual(runner.verdict, "PASS")
        event = next(e for e in self.events() if e["event_type"] == "read_result")
        self.assertEqual((event["payload_hex"], event["length"]), ("", 0))
        self.assertEqual(runner.cases[0]["steps"][0]["event_ids"], [event["event_id"]])

    async def test_no_notification_does_not_satisfy_empty_expect_but_an_empty_callback_does(self):
        runner = await self.run_suite(suite([
            subscribe_step(), checkpoint_step(),
            wait_step({"equals_hex": "", "length": 0}, timeout=.02),
        ]))
        self.assertEqual((runner.verdict, runner.error.code), ("FAIL", "BLE_ASSERTION_FAILED"))
        step = runner.cases[0]["steps"][-1]
        self.assertEqual(step["assertions"][0]["observed_hex"], [])
        self.assertEqual(step["event_ids"], [])
        self.make_session()
        char = await self.ready()
        await self.session.subscribe(char)
        mark = self.session.checkpoint(char)
        self.backend.emit(char.ref, b"")
        found = await self.session.wait(
            mark, .02, lambda data: all(a["success"] for a in assertions(data, {"equals_hex": "", "length": 0})),
        )
        self.assertIsNotNone(found)
        self.assertEqual((found["payload_hex"], found["length"]), ("", 0))

    async def test_queued_precheckpoint_event_is_excluded_and_callbacks_are_not_concatenated(self):
        char = await self.ready()
        await self.session.subscribe(char)
        self.backend.emit(char.ref, b"old")
        self.assertEqual(self.session.buffer.stats()["current_events"], 1)
        mark = self.session.checkpoint(char)
        self.backend.emit(char.ref, b"A")
        self.backend.emit(char.ref, b"B")
        found = await self.session.wait(mark, .02, lambda data: data in (b"old", b"AB"))
        self.assertIsNone(found)
        self.assertEqual([e["payload_hex"] for e in self.session.last_wait_events], ["41", "42"])
        self.assertEqual(
            [e["payload_hex"] for e in self.events() if e["event_type"] == "notification"],
            [b"old".hex(), "41", "42"],
        )

    async def test_old_epoch_generation_and_other_characteristic_are_archived_but_not_matched(self):
        char = await self.ready()
        entry = await self.session.subscribe(char)
        mark = self.session.checkpoint(char)
        self.session.buffer.push("old", entry["generation"], char, b"same")
        self.session.buffer.push(self.session.epoch, "old", char, b"same")
        self.session.buffer.push(
            self.session.epoch, entry["generation"], replace(char, ref="other", uuid=OTHER_CHAR), b"same",
        )
        self.backend.emit(char.ref, b"same")
        found = await self.session.wait(mark, .02, lambda data: data == b"same")
        self.assertIsNotNone(found)
        notifications = [e for e in self.events() if e["event_type"] == "notification"]
        self.assertEqual(len(notifications), 4)
        self.assertEqual(len(self.session.last_wait_events), 1)
        self.assertTrue(all(e["excluded_reason"] == "old_connection_or_subscription" for e in notifications[:3]))
        self.assertNotIn("excluded_reason", notifications[-1])
        self.assertEqual(notifications[0]["epoch"], "old")
        self.assertEqual(notifications[1]["generation"], "old")

    async def test_window_eviction_does_not_truncate_raw_evidence(self):
        char = await self.ready()
        await self.session.subscribe(char)
        self.session.buffer.window_limits.update(max_events=1, max_payload_bytes=2)
        mark = self.session.checkpoint(char)
        for payload in (b"a", b"b", b"c"):
            self.backend.emit(char.ref, payload)
            self.session.drain()
        self.assertEqual(self.session.buffer.stats()["window_events"], 1)
        self.assert_code(
            "BLE_WINDOW_EXPIRED", self.session.buffer.events_after, mark, time.monotonic_ns(),
        )
        self.assertEqual(
            [e["payload_hex"] for e in self.events() if e["event_type"] == "notification"], ["61", "62", "63"],
        )

    async def test_unsupported_operation_modes_and_unknown_or_excessive_limits_block_writes(self):
        char = await self.ready()
        for invalid, response, data in [
            (replace(char, properties=("read",)), True, b"x"),
            (replace(char, properties=("write",)), False, b"x"),
            (replace(char, max_write_with_response=None), True, b"x"),
            (replace(char, max_write_without_response=None), False, b"x"),
            (replace(char, max_write_without_response=2), False, b"123"),
            (replace(char, max_write_with_response=999), True, b"x" * 513),
        ]:
            with self.subTest(char=invalid, response=response, length=len(data)):
                await self.assert_async_code("BLE_CAPABILITY_UNSUPPORTED", self.session.write, invalid, data, response)
        self.assertEqual(self.calls("write"), [])
        await self.assert_async_code(
            "BLE_CAPABILITY_UNSUPPORTED", self.session.subscribe, replace(char, properties=("indicate",)),
        )
        self.assertEqual(self.calls("subscribe"), [])
        await self.session.write(replace(char, max_write_without_response=2), b"12", False)
        self.assertEqual(len(self.calls("write")), 1)
        self.assertIs(self.calls("write")[0][-1], False)
        event = next(e for e in self.events() if e["event_type"] == "write_complete")
        self.assertEqual(event["ack_scope"], "local_submission_only")

    async def test_missing_declared_specification_anywhere_prevents_all_suite_writes(self):
        for failure in ("missing_property", "missing_characteristic"):
            with self.subTest(failure=failure):
                self.make_session()
                source = suite([write_step(), read_step()])
                source["characteristics"]["later"] = {
                    "service_uuid": SERVICE, "uuid": CHAR if failure == "missing_property" else OTHER_CHAR,
                    "required_properties": ["indicate"] if failure == "missing_property" else ["read"],
                }
                runner = await self.run_suite(source)
                self.assertEqual(runner.error.code, "BLE_ASSERTION_FAILED")
                self.assertEqual(runner.error.data["stage"], "gatt_preflight")
                self.assertEqual(runner.verdict, "FAIL")
                self.assertEqual(runner.cases[0]["status"], "SKIP")
                self.assertEqual(self.calls("write"), [])
                self.assertTrue(any(not item["success"] for item in runner.preflight))

    async def test_duplicate_instances_are_ambiguous_but_same_char_uuid_in_other_service_is_resolved(self):
        for duplicate in ("service", "characteristic"):
            with self.subTest(duplicate=duplicate):
                source = profile()
                if duplicate == "service":
                    source["services"].append(deepcopy(source["services"][0]))
                else:
                    source["services"][0]["characteristics"].append(
                        deepcopy(source["services"][0]["characteristics"][0]),
                    )
                self.make_session(source)
                await self.session.connect(await self.session.scan())
                runner = await BleTestRunner(self.session, validate_suite(suite([write_step(), read_step()]))).run()
                self.assertEqual(runner.error.code, "BLE_CHARACTERISTIC_AMBIGUOUS")
                self.assertEqual(runner.verdict, "NOT_EVALUATED")
                self.assertEqual(self.calls("write"), [])
        source = profile()
        second = deepcopy(source["services"][0])
        second["uuid"] = OTHER_SERVICE
        source["services"].append(second)
        self.make_session(source)
        first = await self.ready()
        second_char = self.session.resolve({"service_uuid": OTHER_SERVICE, "uuid": CHAR})
        self.assertNotEqual(first.ref, second_char.ref)
        await self.session.write(first, b"\x02", True)
        self.assertEqual((await self.session.read(second_char))[0], b"\x01\x00")

    async def test_selection_requires_exact_unique_address_not_only_a_name(self):
        candidates = await self.session.scan()
        for selected, config_device, code in [
            ([], "fake:demo", "BLE_DEVICE_NOT_FOUND"),
            (candidates * 2, "fake:demo", "BLE_DEVICE_AMBIGUOUS"),
            (candidates, None, "BLE_DEVICE_NOT_FOUND"),
            ([Candidate("changed", "fake:other", "演示")], "fake:demo", "BLE_DEVICE_NOT_FOUND"),
        ]:
            with self.subTest(code=code, device=config_device):
                self.config["ble"]["device"] = config_device
                self.config["ble"]["name"] = "演示"
                await self.assert_async_code(code, self.session.connect, selected)
        self.assertEqual(self.calls("connect"), [])

    async def test_multi_case_pass_fail_skip_preserves_completed_assertions(self):
        source = suite()
        source["tests"] += [
            {"name": "错误预期", "steps": [read_step({"equals_hex": "FF"}), write_step()]},
            {"name": "不再执行", "steps": [write_step(), read_step()]},
        ]
        runner = await self.run_suite(source)
        self.assertEqual(runner.error.code, "BLE_ASSERTION_FAILED")
        self.assertEqual(runner.verdict, "FAIL")
        self.assertEqual(
            [(c["status"], c["verdict"]) for c in runner.cases],
            [("COMPLETED", "PASS"), ("COMPLETED", "FAIL"), ("SKIP", "NOT_EVALUATED")],
        )
        self.assertEqual(runner.cases[0]["steps"][0]["verdict"], "PASS")
        self.assertEqual(runner.cases[1]["steps"][1]["status"], "SKIP")
        self.assertEqual(self.calls("write"), [])

    async def test_multi_case_pass_error_skip_retains_actual_primary_error(self):
        source = suite()
        source["tests"] += [
            {"name": "写操作错误", "steps": [write_step(), read_step()]},
            {"name": "不再执行", "steps": [read_step()]},
        ]
        primary = ToolError("BLE_REMOTE_REJECTED", "fixture ATT rejection", {"stage": "write", "att_code": 3})
        self.backend.faults["write"] = primary
        runner = await self.run_suite(source)
        self.assertIs(runner.error, primary)
        self.assertEqual(runner.verdict, "NOT_EVALUATED")
        self.assertEqual(
            [(c["status"], c["verdict"]) for c in runner.cases],
            [("COMPLETED", "PASS"), ("ERROR", "NOT_EVALUATED"), ("SKIP", "NOT_EVALUATED")],
        )
        self.assertEqual(runner.cases[1]["steps"][0]["error_data"]["write_effect"], "unknown")
        self.assertEqual(len(self.calls("write")), 1)
        self.assertEqual(len(self.calls("read")), 1)

    async def test_unexpected_disconnect_stops_runner_without_reconnect_or_write_replay(self):
        def disconnected():
            self.backend.simulate_disconnect("fixture lost link")

        self.backend.faults["write"] = disconnected
        runner = await self.run_suite(suite([read_step(), write_step(), read_step()]))
        self.assertEqual(runner.error.code, "BLE_DISCONNECTED")
        self.assertEqual(runner.error.data["write_effect"], "unknown")
        self.assertEqual(runner.cases[0]["steps"][-1]["status"], "SKIP")
        self.assertEqual(len(self.calls("write")), 1)
        self.assertEqual(len(self.calls("connect")), 1)
        self.assertEqual(len(self.calls("read")), 1)

    async def test_local_timeouts_are_stage_specific_and_internal_cancellation_is_not_user_cancel(self):
        for stage in ("scan", "connect", "discover", "read", "write", "subscribe"):
            with self.subTest(stage=stage):
                self.make_session()
                self.config["ble"].update(operation_timeout_s=.04, connect_timeout_s=.04, scan_timeout_s=.01)
                if stage == "scan":
                    self.backend.faults["scan"] = block_forever
                    action = self.session.scan
                elif stage in ("connect", "discover"):
                    candidates = await self.session.scan()
                    if stage == "connect":
                        # connect 的 timeout_s 也会传给 Fake 内层，故用不带计时器的阻塞 Stub
                        # 隔离领域预算；保留真正的 observed_at >= local_deadline 断言。
                        async def connect_stub(candidate_id, timeout_s):
                            self.backend.calls.append(("connect", candidate_id, timeout_s))
                            await block_forever()

                        self.backend.connect = connect_stub
                    else:
                        self.backend.faults[stage] = block_forever
                    action = lambda: self.session.connect(candidates)
                else:
                    char = await self.ready()
                    self.backend.faults[stage] = block_forever
                    action = {
                        "read": lambda: self.session.read(char),
                        "write": lambda: self.session.write(char, b"x", True),
                        "subscribe": lambda: self.session.subscribe(char),
                    }[stage]
                error = await self.assert_async_code("BLE_OPERATION_TIMEOUT", action)
                self.assertEqual(error.data["stage"], stage)
                self.assertEqual(error.data["timeout_scope"], "local")
                self.assertGreater(error.data["local_budget_s"], 0)
                self.assertGreaterEqual(error.data["observed_at"], error.data["local_deadline"])
                self.assertFalse(self.ctx.cancelled.is_set())
                if stage in ("read", "write", "subscribe"):
                    self.assertEqual(error.data["epoch"], self.session.epoch)
                    self.assertEqual(error.data["service_uuid"], char.service_uuid)
                    self.assertEqual(error.data["characteristic_uuid"], char.uuid)
                if stage == "write":
                    self.assertEqual(error.data["write_effect"], "unknown")
                    self.assertEqual(len(self.calls("write")), 1)
                await self.session.close()
                self.assertEqual(len(self.calls("disconnect")), 1)
                self.assertFalse(self.session._tasks)

    async def test_scan_start_and_stop_timeout_evidence_is_preserved_from_backend(self):
        # scanner 控制阶段由适配器计时；这里验证领域不改成 scan 采样超时或吞掉 stage。
        for stage in ("scan_start", "scan_stop"):
            with self.subTest(stage=stage):
                self.make_session()
                error = ToolError("BLE_OPERATION_TIMEOUT", "fixture scanner timeout", {
                    "stage": stage, "timeout_scope": "local", "local_budget_s": .2,
                    "local_deadline": 10, "observed_at": 11,
                })
                self.backend.faults["scan"] = error
                observed = await self.assert_async_code("BLE_OPERATION_TIMEOUT", self.session.scan)
                self.assertIs(observed, error)
                self.assertEqual(observed.data["stage"], stage)

    async def test_normal_scan_empty_observation_and_wait_expiry_are_not_operation_timeouts(self):
        async def empty_scan(timeout_s):
            return []

        with patch.object(self.backend, "scan", empty_scan):
            candidates = await self.session.scan()
        self.assertEqual(candidates, [])
        await self.assert_async_code("BLE_DEVICE_NOT_FOUND", self.session.connect, candidates)
        runner = await self.run_suite(suite([
            subscribe_step(), checkpoint_step(), wait_step(timeout=.02),
        ]))
        self.assertEqual(runner.error.code, "BLE_ASSERTION_FAILED")
        self.assertEqual(runner.cases[0]["steps"][-1]["verdict"], "FAIL")

    async def test_total_deadline_and_explicit_cancel_win_when_local_deadline_is_also_due(self):
        for cancelled, expected in [(False, "WORKFLOW_TIMEOUT"), (True, "CANCELLED")]:
            with self.subTest(cancelled=cancelled):
                self.make_session()
                char = await self.ready()
                clock = [time.monotonic()]

                def deadlines_reached():
                    clock[0] += 10
                    self.ctx.deadline = time.monotonic() - 1
                    if cancelled:
                        self.ctx.cancelled.set()

                self.backend.faults["read"] = deadlines_reached
                with patch("embedded_auto.ble.session.time", SimpleNamespace(monotonic=lambda: clock[0])):
                    error = await self.assert_async_code(expected, self.session.read, char)
                if not cancelled:
                    self.assertEqual(error.data["timeout_scope"], "run")
                    self.assertEqual(error.data["stage"], "read")
                    self.assertGreaterEqual(error.data["observed_at"], error.data["local_deadline"])
                self.assertEqual(len(self.calls("read")), 1)
                await self.session.close()
                self.assertEqual(len(self.calls("disconnect")), 1)

    async def test_preexisting_cancel_wins_over_total_deadline_without_starting_backend(self):
        char = await self.ready()
        self.ctx.deadline = time.monotonic() - 1
        self.ctx.cancelled.set()
        await self.assert_async_code("CANCELLED", self.session.read, char)
        self.assertEqual(self.calls("read"), [])
        await self.session.close()
        self.assertEqual(len(self.calls("disconnect")), 1)

    async def test_task_cancel_during_read_is_user_cancel_and_cleanup_still_runs(self):
        char = await self.ready()
        started = asyncio.Event()

        async def reading():
            started.set()
            await block_forever()

        self.backend.faults["read"] = reading
        task = asyncio.create_task(self.session.read(char))
        await started.wait()
        task.cancel()
        error = await self.assert_async_code("CANCELLED", lambda: task)
        self.assertEqual(error.data["stage"], "read")
        self.assertEqual(error.data.get("epoch"), self.session.epoch)
        self.assertEqual(error.data.get("service_uuid"), char.service_uuid)
        self.assertEqual(error.data.get("characteristic_uuid"), char.uuid)
        self.assertTrue(self.ctx.cancelled.is_set())
        await self.session.close()
        self.assertEqual(len(self.calls("disconnect")), 1)
        self.assertFalse(self.session._tasks)

    async def test_task_cancel_during_notification_wait_is_not_an_empty_window_failure(self):
        char = await self.ready()
        await self.session.subscribe(char)
        mark = self.session.checkpoint(char)
        started = asyncio.Event()

        def predicate(data):
            started.set()
            return False

        self.backend.emit(char.ref, b"observed")
        task = asyncio.create_task(self.session.wait(mark, 5, predicate))
        await started.wait()
        task.cancel()
        await self.assert_async_code("CANCELLED", lambda: task)
        self.assertTrue(self.ctx.cancelled.is_set())
        await self.session.close()
        self.assertEqual(len(self.calls("disconnect")), 1)

    async def test_partial_subscribe_failure_and_early_callback_are_cleaned_by_disconnect(self):
        char = await self.ready()

        async def partially_subscribed():
            self.backend.emit(char.ref, b"early")
            raise RuntimeError("partial subscribe")

        self.backend.faults["subscribe"] = partially_subscribed
        await self.assert_async_code("BLE_GATT_FAILED", self.session.subscribe, char)
        self.assertIn(char.ref, self.session.subscriptions)
        cleanup = await self.session.close()
        self.assertTrue(any(item["action"] == "disconnect" and item["success"] for item in cleanup))
        self.assertEqual(len(self.calls("disconnect")), 1)
        self.assertEqual(
            [e["payload_hex"] for e in self.events() if e["event_type"] == "notification"], [b"early".hex()],
        )
        self.assertFalse(self.session._tasks)

    async def test_cleanup_failures_do_not_replace_runner_primary_error_or_skip_disconnect(self):
        self.backend.faults.update({
            "unsubscribe": RuntimeError("unsubscribe fixture"),
            "disconnect": RuntimeError("disconnect fixture"),
        })
        runner = await self.run_suite(suite([subscribe_step(), read_step({"equals_hex": "FF"})]))
        primary = runner.error
        self.assertEqual(primary.code, "BLE_ASSERTION_FAILED")
        # 主错已确定后再到达总截止，不能跳过独立清理或改写 runner 错误。
        self.ctx.deadline = time.monotonic() - 1
        cleanup = await self.session.close()
        self.assertIs(runner.error, primary)
        self.assertEqual(runner.verdict, "FAIL")
        self.assertEqual([item["action"] for item in cleanup], ["unsubscribe", "disconnect"])
        self.assertTrue(all(not item["success"] for item in cleanup))
        self.assertEqual(len(self.calls("unsubscribe")), 1)
        self.assertEqual(len(self.calls("disconnect")), 1)
        again = await self.session.close()
        self.assertIs(again, cleanup)
        self.assertEqual(len(self.calls("disconnect")), 1)
        self.assertFalse(self.session._tasks)

    async def test_all_passed_cases_remain_pass_when_cleanup_fails(self):
        self.backend.faults["unsubscribe"] = RuntimeError("unsubscribe fixture")
        runner = await self.run_suite(suite([subscribe_step(), read_step()]))
        cleanup = await self.session.close()
        self.assertIsNone(runner.error)
        self.assertEqual((runner.verdict, runner.cases[0]["verdict"]), ("PASS", "PASS"))
        self.assertFalse(cleanup[0]["success"])
        self.assertEqual(cleanup[-1]["action"], "disconnect")
        self.assertTrue(cleanup[-1]["success"])

    async def test_cleanup_uses_one_shared_budget_but_attempts_disconnect_after_unsubscribe_timeout(self):
        char = await self.ready()
        await self.session.subscribe(char)
        self.config["ble"]["cleanup_timeout_s"] = .04
        self.backend.faults["unsubscribe"] = block_forever
        self.backend.faults["disconnect"] = block_forever
        cleanup = await self.session.close()
        attempts = [item for item in cleanup if item["action"] in ("unsubscribe", "disconnect")]
        self.assertEqual([item["action"] for item in attempts], ["unsubscribe", "disconnect"])
        self.assertTrue(all(not item["success"] for item in attempts))
        for item in attempts:
            self.assertEqual(item["error_code"], "BLE_OPERATION_TIMEOUT")
            self.assertEqual(item["data"]["stage"], "cleanup")
            self.assertEqual(item["data"]["timeout_scope"], "cleanup")
        self.assertEqual(attempts[0]["data"]["local_deadline"], attempts[1]["data"]["local_deadline"])
        self.assertEqual(len(self.calls("disconnect")), 1)
        await asyncio.sleep(0)  # 让已取消任务完成其 done 回调，不重启任何清理动作。
        self.assertFalse(self.session._tasks)


if __name__ == "__main__":
    unittest.main()
