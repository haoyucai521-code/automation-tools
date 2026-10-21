"""Validated, deterministic protocol tests with byte-offset evidence.

Hex offsets and checksum ranges are byte-based; checksum end is exclusive.
Text is UTF-8; regex operates on UTF-8 bytes so evidence offsets remain exact.
"""

import json
import math
import multiprocessing
import re
import time

from embedded_auto.core.errors import ToolError
from embedded_auto.core.models import OperationResult


_RULES = {'contains_text', 'contains_hex', 'regex', 'equals_hex', 'length',
          'header_hex', 'fields', 'checksum', 'ordered_text'}


def _require(condition, message):
    if not condition:
        raise ToolError('CONFIG_INVALID', message)


def _integer(value, name, minimum=0):
    _require(type(value) is int and value >= minimum, f'{name} must be an integer >= {minimum}')
    return value


def _text(value, name):
    _require(isinstance(value, str) and bool(value), f'{name} must be nonempty text')
    return value.encode('utf-8')


def _hex(value, name, nonempty=False):
    _require(isinstance(value, str), f'{name} must be hex text')
    data = bytes.fromhex(value)
    _require(not nonempty or bool(data), f'{name} must not be empty')
    return data


def _prepare_suite(suite):
    try:
        _require(isinstance(suite, dict) and type(suite.get('version')) is int and suite['version'] == 1,
                 'suite.version must be 1')
        tests = suite.get('tests')
        _require(isinstance(tests, list) and bool(tests), 'suite.tests must be a nonempty list')
        prepared = []
        for index, case in enumerate(tests):
            _require(isinstance(case, dict), f'tests[{index}] must be an object')
            _text(case.get('name'), 'test.name')
            sends = [key for key in ('send_text', 'send_hex') if key in case]
            _require(len(sends) == 1, 'Each test needs exactly one send_text or send_hex')
            if sends[0] == 'send_text':
                _require(isinstance(case['send_text'], str), 'send_text must be text')
                tx = case['send_text'].encode('utf-8')
            else:
                tx = _hex(case['send_hex'], 'send_hex')
            timeout = case.get('timeout_s')
            _require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0,
                     'timeout_s must be finite and positive')
            expect = case.get('expect')
            _require(isinstance(expect, dict) and bool(expect), 'Each test needs at least one assertion')
            _require(not (set(expect) - _RULES), 'Unknown expectation rule')
            rules = []
            for name, value in expect.items():
                if name == 'contains_text':
                    value = _text(value, name)
                elif name in ('contains_hex', 'header_hex', 'equals_hex'):
                    value = _hex(value, name, nonempty=name != 'equals_hex')
                elif name == 'regex':
                    value = re.compile(_text(value, name))
                elif name == 'length':
                    value = _integer(value, name)
                elif name == 'ordered_text':
                    _require(isinstance(value, list) and bool(value), 'ordered_text must be a nonempty list')
                    value = [_text(item, name) for item in value]
                elif name == 'fields':
                    _require(isinstance(value, list) and bool(value), 'fields must be a nonempty list')
                    fields = []
                    for field in value:
                        _require(isinstance(field, dict), 'field must be an object')
                        offset = _integer(field.get('offset'), 'field.offset')
                        size = _integer(field.get('size'), 'field.size', 1)
                        byteorder = field.get('byteorder')
                        _require(byteorder in ('little', 'big'), 'field.byteorder must be little or big')
                        expected = _integer(field.get('value'), 'field.value')
                        _require(expected.bit_length() <= size * 8, 'field.value does not fit size')
                        fields.append({'offset': offset, 'size': size, 'byteorder': byteorder, 'value': expected})
                    value = fields
                elif name == 'checksum':
                    _require(isinstance(value, dict), 'checksum must be an object')
                    algorithm = value.get('algorithm')
                    _require(algorithm in ('sum8', 'xor8'), 'Unsupported checksum algorithm')
                    start = _integer(value.get('start'), 'checksum.start')
                    end = _integer(value.get('end'), 'checksum.end', start + 1)
                    check_index = _integer(value.get('index'), 'checksum.index')
                    value = {'algorithm': algorithm, 'start': start, 'end': end, 'index': check_index}
                rules.append((name, value))
            prepared.append({'name': case['name'], 'tx': tx, 'timeout_s': float(timeout), 'rules': rules})
        return prepared
    except ToolError:
        raise
    except (ValueError, TypeError, OverflowError, re.error) as exc:
        raise ToolError('CONFIG_INVALID', 'Invalid protocol suite: ' + str(exc)) from exc


def validate_suite(suite: dict) -> dict:
    """Preflight the entire suite without opening hardware, sending, or mutating it.

    Returns the original suite; invalid input raises ToolError(CONFIG_INVALID).
    """
    _prepare_suite(suite)
    return suite


def _regex_worker(connection):
    """Keep pathological regex backtracking outside the cancellable caller."""
    try:
        connection.send('ready')
        while True:
            request = connection.recv()
            if request is None:
                return
            pattern, data = request
            match = re.search(pattern, data)
            connection.send(match.span() if match else None)
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


class _RegexMatcher:
    def __init__(self, ctx):
        self.ctx = ctx
        self.process = None
        self.connection = None
        self.timed_out = False

    def start(self):
        if self.process is not None:
            return
        spawn = multiprocessing.get_context('spawn')
        self.connection, child = spawn.Pipe()
        self.process = spawn.Process(target=_regex_worker, args=(child,), name='protocol-regex', daemon=True)
        try:
            self.process.start()
        except (OSError, RuntimeError) as exc:
            raise ToolError('PROTOCOL_TEST_FAILED', 'Could not start regex worker: ' + str(exc)) from exc
        finally:
            child.close()
        while not self.connection.poll(0.01):
            self.ctx.check()
            if not self.process.is_alive():
                raise ToolError('PROTOCOL_TEST_FAILED', 'Regex worker exited during startup')
        if self.connection.recv() != 'ready':
            raise ToolError('PROTOCOL_TEST_FAILED', 'Regex worker did not initialize')

    def search(self, pattern, data, deadline):
        if self.timed_out or time.monotonic() >= deadline:
            self.timed_out = True
            return None
        try:
            self.connection.send((pattern, data))
        except (EOFError, OSError) as exc:
            raise ToolError('PROTOCOL_TEST_FAILED', 'Regex worker communication failed: ' + str(exc)) from exc
        while True:
            self.ctx.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.timed_out = True
                self.close()
                return None
            if self.connection.poll(min(0.01, remaining)):
                try:
                    return self.connection.recv()
                except (EOFError, OSError) as exc:
                    raise ToolError('PROTOCOL_TEST_FAILED', 'Regex worker communication failed: ' + str(exc)) from exc
            if not self.process.is_alive():
                raise ToolError('PROTOCOL_TEST_FAILED', 'Regex worker exited during matching')

    def close(self):
        if self.process is not None:
            if self.process.pid is not None:
                if self.process.is_alive():
                    self.process.terminate()
                self.process.join(1)
                if self.process.is_alive():
                    self.process.kill()
                    self.process.join(1)
                if self.process.is_alive():
                    raise ToolError('CLEANUP_FAILED', 'Regex worker is still running', {'pid': self.process.pid})
            self.process.close()
            self.process = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None


def _assertions(data, rules, base, matcher, deadline):
    evidence = []

    def record(rule, success, spans=(), **details):
        evidence.append({'rule': rule, 'success': bool(success),
                         'message': '' if success else f'{rule} assertion not satisfied',
                         'offsets': [{'start': base + start, 'end': base + end} for start, end in spans],
                         **details})

    for name, value in rules:
        if name in ('contains_text', 'contains_hex'):
            index = data.find(value)
            record(name, index >= 0, [(index, index + len(value))] if index >= 0 else (), expected_hex=value.hex())
        elif name == 'header_hex':
            record(name, data.startswith(value), [(0, min(len(data), len(value)))], expected_hex=value.hex())
        elif name == 'equals_hex':
            record(name, data == value, [(0, len(data))], expected_hex=value.hex(), actual_hex=data.hex())
        elif name == 'length':
            record(name, len(data) == value, [(0, len(data))], expected=value, actual=len(data))
        elif name == 'regex':
            span = matcher.search(value.pattern, data, deadline) if data else None
            record(name, span is not None, [span] if span else (), pattern=value.pattern.decode('utf-8'),
                   timed_out=matcher.timed_out)
        elif name == 'fields':
            for field in value:
                start, end = field['offset'], field['offset'] + field['size']
                actual = int.from_bytes(data[start:end], field['byteorder']) if len(data) >= end else None
                record(name, actual == field['value'], [(start, min(end, len(data)))] if start < len(data) else (),
                       expected=field['value'], actual=actual, field=field)
        elif name == 'checksum':
            start, end, index = value['start'], value['end'], value['index']
            ready = len(data) >= end and len(data) > index
            calculated = None
            if ready:
                calculated = 0
                for byte in data[start:end]:
                    calculated = (calculated + byte) & 255 if value['algorithm'] == 'sum8' else calculated ^ byte
            record(name, ready and calculated == data[index],
                   [(start, end), (index, index + 1)] if ready else (),
                   algorithm=value['algorithm'], expected=calculated, actual=data[index] if ready else None)
        elif name == 'ordered_text':
            cursor, spans = 0, []
            for token in value:
                index = data.find(token, cursor)
                if index < 0:
                    break
                spans.append((index, index + len(token)))
                cursor = index + len(token)
            record(name, len(spans) == len(value), spans, matched=len(spans), expected=len(value))
    return evidence


class TestRunner:
    def __init__(self, session, ctx):
        self.session = session
        self.ctx = ctx

    def run(self, suite: dict) -> OperationResult:
        started = time.monotonic()
        started_at = time.time()
        reports = []
        error = None
        matcher = _RegexMatcher(self.ctx)
        try:
            cases = _prepare_suite(suite)  # Validate all cases before even the first write.
            for case in cases:
                self.ctx.check()
                self.session.check()
                if any(name == 'regex' for name, _ in case['rules']):
                    matcher.timed_out = False
                    matcher.start()  # Worker startup is not response latency.
                begin = self.session.mark()
                case_started = time.monotonic()
                report = {'name': case['name'], 'status': 'FAIL', 'tx_hex': case['tx'].hex(),
                          'rx_hex': '', 'started_at': time.time(), 'rx_start': begin,
                          'timeout_s': case['timeout_s'], 'assertions': []}
                reports.append(report)
                failure = None
                data = b''
                try:
                    self.ctx.event('PROTOCOL_TEST_STARTED', name=case['name'], rx_start=begin)
                    self.session.write(case['tx'])
                    deadline = time.monotonic() + case['timeout_s']
                    latest_data, latest_evidence = None, []
                    def satisfied(response):
                        nonlocal latest_data, latest_evidence
                        if response != latest_data:
                            latest_evidence = _assertions(response, case['rules'], begin, matcher, deadline)
                            latest_data = response
                        return bool(response) and all(item['success'] for item in latest_evidence)
                    data = self.session.wait_for(satisfied, case['timeout_s'], self.ctx, start=begin)
                    if data != latest_data:
                        latest_evidence = _assertions(data, case['rules'], begin, matcher, deadline)
                    report['assertions'] = latest_evidence
                    if not data or not all(item['success'] for item in report['assertions']):
                        failure = ToolError('PROTOCOL_TEST_FAILED', 'No response before timeout' if not data else 'Response assertions not satisfied before timeout')
                except ToolError as exc:
                    failure = exc
                    try:
                        data = self.session.received_since(begin)
                        report['assertions'] = _assertions(data, case['rules'], begin, matcher, time.monotonic())
                    except ToolError as evidence_exc:
                        report['evidence_error'] = {'code': evidence_exc.code, 'message': str(evidence_exc)}
                report.update(rx_hex=data.hex(), rx_end=begin + len(data),
                              finished_at=time.time(), duration_ms=int((time.monotonic() - case_started) * 1000),
                              status='FAIL' if failure else 'PASS',
                              error_code=failure.code if failure else None, message=str(failure) if failure else '')
                self.ctx.event('PROTOCOL_TEST_FINISHED', name=case['name'], status=report['status'], rx_end=report['rx_end'])
                if failure and error is None:
                    error = failure
                if failure and failure.code != 'PROTOCOL_TEST_FAILED':
                    error = failure
                    break
        except ToolError as exc:
            error = exc
        finally:
            try:
                matcher.close()
            except ToolError as cleanup_exc:
                if error is None:
                    error = cleanup_exc
                else:
                    error.data = dict(error.data or {}, regex_cleanup_error=str(cleanup_exc))
        report_path = self.ctx.path / 'test_report.json'
        result = OperationResult(success=error is None, operation='test',
            error_code=error.code if error else None, message=str(error) if error else 'All protocol tests passed',
            duration_ms=int((time.monotonic() - started) * 1000), log_path=str(report_path),
            data={'version': 1, 'tests': reports, 'started_at': started_at, 'finished_at': time.time(),
                  'error_details': error.data if error else {},
                  'report_path': str(report_path), 'serial_bin': str(self.ctx.path / 'serial.bin'),
                  'serial_jsonl': str(self.ctx.path / 'serial.jsonl')})
        try:
            self.ctx.path.mkdir(parents=True, exist_ok=True)
            with report_path.open('x', encoding='utf-8') as stream:
                json.dump(result.to_dict(), stream, ensure_ascii=False, indent=2)
        except OSError as exc:
            result.success = False
            result.error_code = result.error_code or 'LOG_WRITE_FAILED'
            result.data['report_error'] = str(exc)
            result.message += '; Could not archive test report: ' + str(exc)
        return result
