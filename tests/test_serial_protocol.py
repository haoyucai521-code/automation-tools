"""No real hardware: serial lifecycle, archival evidence and protocol assertions."""

import copy
import json
import multiprocessing
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from embedded_auto.core.errors import ToolError
from embedded_auto.services.log_analyzer import LogAnalyzer
from embedded_auto.services.log_service import LogService
from embedded_auto.services.reset_controller import ResetController
from embedded_auto.services.serial_session import SerialSession
from embedded_auto.services.test_runner import TestRunner, _RegexMatcher, validate_suite


def config():
    # Verified markers are synthetic test fixtures, never real hardware claims.
    return {
        'app': {'default_timeout_s': 5, 'runs_dir': 'runs', 'workspace': '.'},
        'target': {'id': 'fake', 'family': 'STM32', 'exact_part_number': 'STM32F030F4P6', 'status': 'verified'},
        'serial': {'port': 'COM_FAKE', 'baudrate': 115200, 'bytesize': 8, 'parity': 'N',
                   'stopbits': 1, 'rtscts': False, 'dsrdtr': False, 'read_timeout_s': 0.01},
        'reset': {'asserted_rts_value': False, 'pulse_ms': 1, 'release_delay_ms': 0,
                  'polarity_verified': True, 'voltage_verified': True, 'wiring_verified': True,
                  'open_transient_verified': True, 'timing_verified': True},
        'build': {}, 'flash': {'cli_verified': True, 'compatibility_verified': True},
        'startup': {},
    }


class FakeContext:
    def __init__(self, path):
        self.path = Path(path)
        self.config = config()
        self.run_id = 'fake-run'
        self.events = []
        self.cancelled = threading.Event()
        self.deadline = time.monotonic() + 5
        self.sleep_error = None

    @property
    def remaining(self):
        return max(0, self.deadline - time.monotonic())

    def check(self):
        if self.cancelled.is_set():
            raise ToolError('CANCELLED', 'cancelled')
        if not self.remaining:
            raise ToolError('WORKFLOW_TIMEOUT', 'deadline')

    def event(self, state, **data):
        self.events.append((state, data))

    def sleep(self, seconds):
        if self.sleep_error:
            raise self.sleep_error
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check()
            self.cancelled.wait(min(0.005, end - time.monotonic()))
        self.check()


class FakeSerial:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.port = kwargs.get('port')
        self.timeout = kwargs.get('timeout', 0.01)
        self.is_open = False
        self.history = []
        self.writes = []
        self.on_write = None
        self.on_rts = None
        self.open_error = None
        self.read_error = None
        self.close_failures = 0
        self.control_error = False
        self.partial = None
        self.close_calls = 0
        self.read_threads = set()
        self._rts = True
        self._incoming = bytearray()
        self._condition = threading.Condition()
        self._cancel = False

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, value):
        if self.control_error:
            raise OSError('RTS disconnected')
        self.history.append(('rts', value, self.is_open))
        self._rts = value
        if self.on_rts:
            self.on_rts(value)

    def open(self):
        self.history.append(('open', self._rts))
        if self.open_error:
            raise self.open_error
        self.is_open = True

    def feed(self, data):
        with self._condition:
            self._incoming.extend(data)
            self._condition.notify_all()

    def read(self, size):
        with self._condition:
            self.read_threads.add(threading.get_ident())
            if not self._incoming and not self._cancel:
                self._condition.wait(self.timeout)
            if self.read_error:
                raise self.read_error
            if self._cancel:
                self._cancel = False
                return b''
            data = bytes(self._incoming[:size])
            del self._incoming[:size]
            return data

    def write(self, data):
        self.writes.append(data)
        if self.on_write:
            self.on_write(data)
        return len(data) if self.partial is None else self.partial

    def cancel_read(self):
        with self._condition:
            self._cancel = True
            self._condition.notify_all()

    def close(self):
        self.close_calls += 1
        if self.close_failures:
            self.close_failures -= 1
            raise OSError('close failed once')
        self.is_open = False
        self.cancel_read()


class SerialProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = FakeContext(self.temp.name)
        self.cfg = config()
        self.fake = None
        self.session = self.new_session()

    def new_session(self, cfg=None):
        def factory(**kwargs):
            self.fake = FakeSerial(**kwargs)
            return self.fake
        session = SerialSession(cfg or self.cfg, factory=factory)
        self.addCleanup(session.close)
        return session

    def error_code(self, code, fn, *args, **kwargs):
        with self.assertRaises(ToolError) as raised:
            fn(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def start(self):
        log = LogService(self.session, self.ctx)
        log.start()
        self.addCleanup(log.close)
        self.session.open()
        # unittest cleanups are LIFO: stop reader before closing evidence files.
        self.addCleanup(self.session.close)
        return log

    def wait_rx(self, expected, start=0):
        data = self.session.wait_for(lambda data: expected in data, 0.5, self.ctx, start=start)
        self.assertIn(expected, data)
        return data

    def suite(self, expect, **changes):
        case = {'name': 'case', 'send_hex': '50', 'expect': expect, 'timeout_s': 0.08}
        case.update(changes)
        return {'version': 1, 'tests': [case]}

    def run_response(self, expect, response, **changes):
        self.start()
        self.fake.on_write = lambda data: self.fake.feed(response)
        return TestRunner(self.session, self.ctx).run(self.suite(expect, **changes))

    def test_open_presets_released_rts_and_disables_flow_control(self):
        self.session.open()
        self.assertEqual(self.fake.history[:2], [('rts', True, False), ('open', True)])
        self.assertIsNone(self.fake.kwargs['port'])
        for key in ('rtscts', 'dsrdtr', 'xonxoff'):
            self.assertIs(self.fake.kwargs[key], False)

    def test_inverse_reset_polarity(self):
        self.cfg['reset']['asserted_rts_value'] = True
        self.session.open()
        self.assertFalse(self.fake.rts)
        controller = ResetController(self.session, self.cfg, self.ctx)
        controller.pulse()
        self.assertEqual(controller.state, 'RELEASED')
        self.assertFalse(self.fake.rts)

    def test_log_before_open_starts_one_receiver_without_extra_call(self):
        log = self.start()
        self.fake.feed(b'boot ready\n')
        self.wait_rx(b'boot ready')
        self.session.open()
        self.assertEqual(len(self.fake.read_threads), 1)
        self.assertEqual((self.ctx.path / 'serial.bin').read_bytes(), b'boot ready\n')
        self.assertTrue(log._unsubscribe)

    def test_open_before_subscription_does_not_consume_early_data(self):
        self.session.open()
        self.fake.feed(b'early')
        time.sleep(0.02)
        self.assertEqual(self.fake.read_threads, set())
        log = LogService(self.session, self.ctx)
        log.start()
        self.addCleanup(log.close)
        self.addCleanup(self.session.close)
        self.wait_rx(b'early')
        self.assertEqual((self.ctx.path / 'serial.bin').read_bytes(), b'early')

    def test_unique_owner_and_case_normalization(self):
        self.session.open()
        cfg = copy.deepcopy(self.cfg)
        cfg['serial']['port'] = '\\\\.\\com_fake'
        other = self.new_session(cfg)
        self.error_code('SERIAL_PORT_BUSY', other.open)
        self.session.close()
        other.open()

    def test_failed_open_closes_and_releases_owner(self):
        bad = FakeSerial()
        bad.open_error = OSError('missing')
        self.session._factory = lambda **kwargs: bad
        self.error_code('SERIAL_OPEN_FAILED', self.session.open)
        self.assertEqual(bad.close_calls, 1)
        other = self.new_session()
        other.open()

    def test_factory_failure_releases_owner(self):
        def fail(**kwargs):
            raise ImportError('no pyserial')
        self.session._factory = fail
        self.error_code('SERIAL_OPEN_FAILED', self.session.open)
        self.new_session().open()

    def test_open_failure_with_failed_cleanup_retains_owner_until_close(self):
        bad = FakeSerial()
        bad.open_error = OSError('open partially failed')
        bad.close_failures = 1
        self.session._factory = lambda **kwargs: bad
        error = self.error_code('SERIAL_OPEN_FAILED', self.session.open)
        self.assertIn('close failed', error.data['cleanup_error'])
        other = self.new_session()
        self.error_code('SERIAL_PORT_BUSY', other.open)
        self.error_code('SERIAL_CLOSE_FAILED', self.session.open)
        self.session.close()
        other.open()

    def test_open_reader_start_failure_releases_rts_before_close(self):
        log = LogService(self.session, self.ctx)
        log.start()
        self.addCleanup(log.close)
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('cannot start reader')):
            error = self.error_code('SERIAL_OPEN_FAILED', self.session.open)
        # 释放发生在 close 之前：释放时端口仍处于打开状态；句柄未打开时不强行释放。
        self.assertEqual(self.fake.history[-2:], [('open', True), ('rts', True, True)])
        self.assertEqual(self.fake.close_calls, 1)
        self.assertIsNone(error.data['cleanup_error'])

    def test_open_failure_after_handle_open_reports_release_and_close_errors(self):
        class HalfOpenSerial(FakeSerial):
            def open(self):
                self.is_open = True
                raise OSError('open partially failed')
        bad = HalfOpenSerial()
        bad.on_rts = lambda value: setattr(bad, 'control_error', True)  # 预置成功，释放尝试失败
        bad.close_failures = 1
        self.session._factory = lambda **kwargs: bad
        error = self.error_code('SERIAL_OPEN_FAILED', self.session.open)
        self.assertIn('open partially failed', str(error))  # 原始 open 错误保留
        self.assertIn('RTS disconnected', error.data['cleanup_error'])
        self.assertIn('close failed', error.data['cleanup_error'])
        other = self.new_session()  # 关闭失败，所有权保留
        self.error_code('SERIAL_PORT_BUSY', other.open)

    def test_failed_close_can_be_retried_and_retains_ownership(self):
        self.start()
        self.fake.close_failures = 1
        self.error_code('SERIAL_CLOSE_FAILED', self.session.close)
        other = self.new_session()
        self.error_code('SERIAL_PORT_BUSY', other.open)
        self.error_code('SERIAL_CLOSE_FAILED', self.session.open)
        self.session.close()
        other.open()
        self.assertIsNone(self.session._thread)

    def test_blocked_reader_is_reported_then_close_is_retryable(self):
        gate, entered = threading.Event(), threading.Event()
        self.session.open()
        def stuck_read(size):
            entered.set()
            gate.wait(3)
            return b''
        self.fake.read = stuck_read
        self.session.subscribe(lambda *args: None)
        self.assertTrue(entered.wait(0.5))
        try:
            self.error_code('SERIAL_CLOSE_FAILED', self.session.close)
            self.assertTrue(self.session._thread.is_alive())
            self.error_code('SERIAL_CLOSE_FAILED', self.session.open)
        finally:
            gate.set()
            self.session.close()
        self.assertIsNone(self.session._thread)

    def test_read_error_propagates_through_check_and_wait(self):
        self.start()
        self.fake.read_error = OSError('disconnect')
        self.fake.feed(b'x')
        self.error_code('SERIAL_READ_FAILED', self.session.wait_for, lambda data: False, 0.3, self.ctx)
        self.error_code('SERIAL_READ_FAILED', self.session.check)

    def test_unsubscribe_and_subscriber_error_do_not_drop_other_evidence(self):
        self.session.open()
        calls = []
        unsubscribe = self.session.subscribe(lambda *args: calls.append(args))
        unsubscribe()
        unsubscribe()
        def broken(*args):
            raise ValueError('subscriber broke')
        self.session.subscribe(broken)
        self.session.subscribe(lambda *args: calls.append(args))
        self.fake.feed(b'x')
        self.error_code('SERIAL_SUBSCRIBER_FAILED', self.session.wait_for, lambda data: False, 0.2, self.ctx)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ('rx', b'x'))

    def test_absolute_offsets_and_bounded_memory(self):
        self.cfg['serial']['max_buffer_bytes'] = 4
        self.session = self.new_session()
        self.start()
        self.fake.feed(b'1234')
        self.wait_rx(b'1234')
        mark = self.session.mark()
        self.fake.feed(b'5678')
        self.wait_rx(b'5678', mark)
        self.assertEqual(self.session.mark(), 8)
        self.assertEqual(len(self.session._buffer), 4)
        self.error_code('SERIAL_BUFFER_RANGE', self.session.received_since, 0)
        self.error_code('SERIAL_BUFFER_RANGE', self.session.wait_for, lambda data: False, 0.1, self.ctx)
        self.assertEqual((self.ctx.path / 'serial.bin').read_bytes(), b'12345678')

    def test_wait_timeout_returns_partial_and_honors_cancel(self):
        self.start()
        self.fake.feed(b'part')
        self.assertEqual(self.session.wait_for(lambda data: False, 0.03, self.ctx), b'part')
        self.ctx.cancelled.set()
        self.error_code('CANCELLED', self.session.wait_for, lambda data: True, 1, self.ctx)

    def test_wait_honors_context_deadline_and_rejects_nonfinite_timeout(self):
        self.start()
        self.ctx.deadline = time.monotonic() + 0.02
        self.error_code('WORKFLOW_TIMEOUT', self.session.wait_for, lambda data: False, 10, self.ctx)
        self.error_code('CONFIG_INVALID', self.session.wait_for, lambda data: False, float('nan'), self.ctx)

    def test_busy_receive_stream_does_not_extend_wait_deadline(self):
        self.start()
        stop = threading.Event()
        def produce():
            while not stop.wait(0.001):
                self.fake.feed(b'x')
        producer = threading.Thread(target=produce)
        producer.start()
        def slow_false(data):
            time.sleep(0.004)
            return False
        # 首字节屏障：确认忙碌串流已进入接收缓冲后再计时，
        # 避免生产者线程启动延迟让 deadline 内没有任何字节而返回 b''。
        self.assertTrue(self.session.wait_for(lambda data: bool(data), 5, self.ctx))
        began = time.monotonic()
        try:
            data = self.session.wait_for(slow_false, 0.03, self.ctx)
        finally:
            stop.set()
            producer.join()
        self.assertTrue(data)
        self.assertLess(time.monotonic() - began, 0.3)

    def test_incremental_utf8_binary_evidence_and_tx_json(self):
        log = self.start()
        self.fake.feed(b'\xe4\xbd')
        self.wait_rx(b'\xe4\xbd')
        self.fake.feed(b'\xa0\xff\n\xe4')
        self.wait_rx(b'\xa0\xff')
        self.session.write(b'\x00\xff')
        self.session.close()
        log.close()
        self.assertEqual((self.ctx.path / 'serial.bin').read_bytes(), b'\xe4\xbd\xa0\xff\n\xe4')
        text = (self.ctx.path / 'serial.log').read_text(encoding='utf-8')
        self.assertIn('你\ufffd', text)
        self.assertTrue(text.rstrip().endswith('\ufffd'))
        records = [json.loads(line) for line in (self.ctx.path / 'serial.jsonl').read_text().splitlines()]
        self.assertEqual(b''.join(bytes.fromhex(item['hex']) for item in records if item['direction'] == 'rx'),
                         (self.ctx.path / 'serial.bin').read_bytes())
        self.assertEqual([item['hex'] for item in records if item['direction'] == 'tx'], ['00ff'])
        self.assertTrue(all(isinstance(item['timestamp'], float) for item in records))

    def test_partial_write_is_failed_but_written_prefix_is_logged(self):
        self.start()
        self.fake.partial = 1
        self.error_code('SERIAL_WRITE_FAILED', self.session.write, b'abc')
        records = [json.loads(line) for line in (self.ctx.path / 'serial.jsonl').read_text().splitlines()]
        self.assertEqual(records[-1]['hex'], '61')

    def test_text_sink_failure_still_preserves_raw_bytes(self):
        log = self.start()
        with patch.object(log, '_text', side_effect=OSError('text disk failure')):
            self.fake.feed(b'raw\xff')
            self.error_code('LOG_WRITE_FAILED', self.session.wait_for, lambda data: False, 0.2, self.ctx)
        self.assertEqual((self.ctx.path / 'serial.bin').read_bytes(), b'raw\xff')
        self.session.close()
        self.error_code('LOG_WRITE_FAILED', log.close)
        log._error = None  # The cleanup has already verified and reported this injected failure.

    def test_logs_never_overwrite_existing_evidence(self):
        (self.ctx.path / 'serial.bin').write_bytes(b'old evidence')
        self.error_code('LOG_WRITE_FAILED', LogService(self.session, self.ctx).start)
        self.assertEqual((self.ctx.path / 'serial.bin').read_bytes(), b'old evidence')

    def test_reset_release_after_cancelled_sleep_and_capture_boot(self):
        self.start()
        self.fake.on_rts = lambda value: self.fake.feed(b'boot\n') if value else None
        controller = ResetController(self.session, self.cfg, self.ctx)
        self.ctx.sleep_error = ToolError('CANCELLED', 'during pulse')
        self.error_code('CANCELLED', controller.pulse, 20)
        self.assertEqual(controller.state, 'RELEASED')
        self.assertTrue(self.fake.rts)
        self.wait_rx(b'boot')

    def test_reset_failure_and_failed_cleanup_preserve_primary_error(self):
        self.start()
        controller = ResetController(self.session, self.cfg, self.ctx)
        self.fake.control_error = True
        self.error_code('RESET_CONTROL_FAILED', controller.assert_reset)
        self.assertEqual(controller.state, 'UNKNOWN')
        def fail_sleep(seconds):
            self.fake.control_error = True
            raise ToolError('CANCELLED', 'original')
        self.fake.control_error = False
        self.ctx.sleep = fail_sleep
        exc = self.error_code('CANCELLED', controller.pulse)
        self.assertIn('reset_release_error', exc.data)

    def test_reset_release_still_writes_rts_after_historical_receive_error(self):
        self.start()
        controller = ResetController(self.session, self.cfg, self.ctx)
        controller.assert_reset()
        self.fake.read_error = OSError('read disconnected')
        self.fake.feed(b'x')
        self.error_code('SERIAL_READ_FAILED', self.session.wait_for, lambda data: False, 0.2, self.ctx)
        controller.release_reset()
        self.assertEqual(controller.state, 'RELEASED')
        self.assertTrue(self.fake.rts)
        self.error_code('SERIAL_READ_FAILED', self.session.check)

    def test_reset_release_still_writes_rts_after_subscriber_error(self):
        self.start()
        controller = ResetController(self.session, self.cfg, self.ctx)
        controller.assert_reset()
        self.session.subscribe(lambda *args: (_ for _ in ()).throw(ValueError('log failed')))
        self.fake.feed(b'x')
        self.error_code('SERIAL_SUBSCRIBER_FAILED', self.session.wait_for, lambda data: False, 0.2, self.ctx)
        controller.release_reset()
        self.assertTrue(self.fake.rts)
        self.error_code('SERIAL_SUBSCRIBER_FAILED', self.session.check)

    def test_pulse_does_not_use_connection_release_delay(self):
        self.start()
        self.cfg['reset']['release_delay_ms'] = 5000
        sleeps = []
        self.ctx.sleep = sleeps.append
        controller = ResetController(self.session, self.cfg, self.ctx)
        controller.pulse(10)
        self.assertEqual(sleeps, [0.01])
        self.cfg['reset']['post_pulse_delay_ms'] = 7
        controller.pulse(10)
        self.assertEqual(sleeps, [0.01, 0.01, 0.007])

    def test_protocol_all_binary_rules_pass_and_report_offsets(self):
        response = bytes.fromhex('aa0102ad')
        result = self.run_response({'contains_hex': '0102', 'header_hex': 'aa', 'equals_hex': response.hex(),
            'length': 4, 'fields': [{'offset': 1, 'size': 2, 'byteorder': 'big', 'value': 258}],
            'checksum': {'algorithm': 'sum8', 'start': 0, 'end': 3, 'index': 3}}, response)
        self.assertTrue(result.success, result.message)
        report = json.loads((self.ctx.path / 'test_report.json').read_text(encoding='utf-8'))
        case = report['data']['tests'][0]
        self.assertEqual(case['status'], 'PASS')
        self.assertEqual(case['tx_hex'], '50')
        self.assertEqual(case['rx_hex'], 'aa0102ad')
        self.assertEqual(case['rx_end'] - case['rx_start'], 4)
        self.assertLessEqual(case['started_at'], case['finished_at'])
        self.assertEqual(case['assertions'][0]['offsets'], [{'start': 1, 'end': 3}])

    def test_text_regex_order_and_little_endian_xor(self):
        result = self.run_response({'contains_text': 'OK', 'regex': r'OK\s+DONE', 'ordered_text': ['OK', 'DONE'],
                                   'fields': [{'offset': 0, 'size': 2, 'byteorder': 'little', 'value': 19279}],
                                   'checksum': {'algorithm': 'xor8', 'start': 0, 'end': 2, 'index': 7}}, b'OK DONE\x04', send_hex='41')
        self.assertTrue(result.success, result.to_dict())

    def test_multirule_does_not_pass_on_partial_match(self):
        result = self.run_response({'contains_text': 'OK', 'length': 5}, b'OK')
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, 'PROTOCOL_TEST_FAILED')
        assertions = result.data['tests'][0]['assertions']
        self.assertEqual([item['success'] for item in assertions], [True, False])

    def test_pathological_regex_is_bounded_by_test_timeout(self):
        began = time.monotonic()
        result = self.run_response({'regex': '(a+)+$'}, b'a' * 1000 + b'!')
        self.assertEqual(result.error_code, 'PROTOCOL_TEST_FAILED')
        self.assertTrue(result.data['tests'][0]['assertions'][0]['timed_out'])
        self.assertLess(time.monotonic() - began, 3)
        self.assertFalse(any(p.name == 'protocol-regex' for p in multiprocessing.active_children()))

    def test_cancel_interrupts_pathological_regex_and_archives(self):
        self.start()
        timer = None
        def respond(data):
            nonlocal timer
            self.fake.feed(b'a' * 1000 + b'!')
            timer = threading.Timer(0.03, self.ctx.cancelled.set)
            timer.start()
        self.fake.on_write = respond
        result = TestRunner(self.session, self.ctx).run(self.suite({'regex': '(a+)+$'}, timeout_s=2))
        if timer:
            timer.join()
        self.assertEqual(result.error_code, 'CANCELLED')
        self.assertEqual(len(result.data['tests'][0]['rx_hex']), 2002)
        self.assertTrue((self.ctx.path / 'test_report.json').exists())
        self.assertFalse(any(p.name == 'protocol-regex' for p in multiprocessing.active_children()))

    def test_regex_matcher_error_code_parameterization_keeps_default(self):
        from multiprocessing.context import SpawnProcess
        matcher = _RegexMatcher(self.ctx)
        self.assertEqual(matcher.error_code, 'PROTOCOL_TEST_FAILED')  # 默认保持协议契约
        with patch.object(SpawnProcess, 'start', side_effect=OSError('spawn failed')):
            startup = _RegexMatcher(self.ctx, error_code='BOOT_MATCHER_FAILED')
            error = self.error_code('BOOT_MATCHER_FAILED', startup.start)
        self.assertIn('Could not start regex worker', str(error))

    def test_no_response_fails_even_vacuous_length_and_regex(self):
        result = self.run_response({'length': 0, 'regex': '.*'}, b'')
        self.assertEqual(result.error_code, 'PROTOCOL_TEST_FAILED')
        self.assertIn('No response', result.message)
        self.assertEqual(result.data['tests'][0]['rx_hex'], '')

    def test_wrong_header_field_checksum_and_order_are_failures(self):
        result = self.run_response({'header_hex': 'ff', 'equals_hex': '00', 'length': 1,
            'ordered_text': ['B', 'A'], 'fields': [{'offset': 10, 'size': 1, 'byteorder': 'big', 'value': 1}],
            'checksum': {'algorithm': 'sum8', 'start': 0, 'end': 2, 'index': 2}}, b'AB\x00')
        self.assertEqual(result.error_code, 'PROTOCOL_TEST_FAILED')
        self.assertTrue(all(not item['success'] for item in result.data['tests'][0]['assertions']))

    def test_split_response_waits_until_all_rules_pass(self):
        self.start()
        sent = threading.Event()
        def feed_later():
            if sent.wait(0.5):
                time.sleep(0.02)
                self.fake.feed(b'DONE')
        producer = threading.Thread(target=feed_later)
        producer.start()
        self.addCleanup(producer.join)
        def respond(data):
            self.fake.feed(b'OK ')
            sent.set()
        self.fake.on_write = respond
        result = TestRunner(self.session, self.ctx).run(self.suite({'ordered_text': ['OK', 'DONE'], 'length': 7}, timeout_s=0.5))
        self.assertTrue(result.success, result.message)

    def test_cases_use_absolute_mark_not_stale_previous_response(self):
        self.start()
        self.fake.feed(b'stale OK')
        self.wait_rx(b'stale OK')
        self.fake.on_write = lambda data: self.fake.feed(b'OK' if data == b'1' else b'NO')
        suite = {'version': 1, 'tests': [
            {'name': str(i), 'send_text': str(i), 'expect': {'contains_text': 'OK'}, 'timeout_s': 0.04}
            for i in (1, 2)]}
        result = TestRunner(self.session, self.ctx).run(suite)
        self.assertEqual([item['status'] for item in result.data['tests']], ['PASS', 'FAIL'])
        self.assertEqual([item['rx_start'] for item in result.data['tests']], [8, 10])
        self.assertEqual(result.data['tests'][1]['rx_hex'], b'NO'.hex())

    def test_invalid_suite_never_sends_and_writes_failure_report(self):
        self.start()
        suite = self.suite({'contains_text': 'OK'})
        suite['tests'].append({'name': 'bad', 'send_hex': '00', 'expect': {}, 'timeout_s': 0.1})
        result = TestRunner(self.session, self.ctx).run(suite)
        self.assertEqual(result.error_code, 'CONFIG_INVALID')
        self.assertEqual(self.fake.writes, [])
        self.assertTrue((self.ctx.path / 'test_report.json').exists())

    def test_validate_suite_is_hardware_free_and_does_not_mutate(self):
        suite = self.suite({'contains_text': 'OK'})
        before = copy.deepcopy(suite)
        self.assertIs(validate_suite(suite), suite)
        self.assertEqual(suite, before)
        self.assertIsNone(self.fake)

    def test_invalid_suite_shapes_and_rules_fail_preflight(self):
        invalid = [None, {}, {'version': 1, 'tests': []}, {'version': True, 'tests': []}]
        for expect in ({}, {'unknown': True}, {'contains_text': ''}, {'contains_hex': 'z'}, {'header_hex': ''},
                       {'regex': '['}, {'length': True}, {'length': -1}, {'fields': []},
                       {'fields': [{'offset': -1, 'size': 1, 'byteorder': 'big', 'value': 1}]},
                       {'fields': [{'offset': 0, 'size': 0, 'byteorder': 'big', 'value': 0}]},
                       {'fields': [{'offset': 0, 'size': 1, 'byteorder': 'middle', 'value': 1}]},
                       {'fields': [{'offset': 0, 'size': 1, 'byteorder': 'big', 'value': 256}]},
                       {'checksum': {'algorithm': 'crc', 'start': 0, 'end': 1, 'index': 1}},
                       {'checksum': {'algorithm': 'sum8', 'start': 2, 'end': 1, 'index': 3}},
                       {'ordered_text': []}, {'ordered_text': ['']}):
            invalid.append(self.suite(expect))
        for timeout in (0, -1, float('inf'), float('nan'), True, '1'):
            invalid.append(self.suite({'length': 1}, timeout_s=timeout))
        invalid.append(self.suite({'length': 1}, send_text='also hex'))
        missing_send = self.suite({'length': 1})
        del missing_send['tests'][0]['send_hex']
        invalid.append(missing_send)
        for suite in invalid:
            with self.subTest(suite=suite):
                self.error_code('CONFIG_INVALID', validate_suite, suite)

    def test_runner_archives_cancelled_test_evidence(self):
        self.start()
        def cancel(data):
            self.ctx.cancelled.set()
        self.fake.on_write = cancel
        result = TestRunner(self.session, self.ctx).run(self.suite({'length': 1}))
        self.assertEqual(result.error_code, 'CANCELLED')
        self.assertEqual(result.data['tests'][0]['tx_hex'], '50')
        self.assertTrue((self.ctx.path / 'test_report.json').exists())

    def test_analyzer_default_errors_custom_regex_and_original_path(self):
        path = self.ctx.path / 'serial.log'
        path.write_text('INFO boot ready\nERROR E42 bad\nassert failed\nWARNING voltage\n', encoding='utf-8')
        summary = LogAnalyzer.analyze(path)
        self.assertEqual(summary['log_path'], str(path))
        self.assertGreaterEqual(summary['match_count'], 2)
        self.assertEqual(summary['levels']['error'], 2)
        self.assertEqual(summary['boot_markers'][0]['line'], 1)
        custom = LogAnalyzer.analyze(path, [r'E\d+'])
        self.assertEqual(custom['matches'][0]['match'], 'E42')
        self.error_code('CONFIG_INVALID', LogAnalyzer.analyze, path, ['['])


if __name__ == '__main__':
    unittest.main()
