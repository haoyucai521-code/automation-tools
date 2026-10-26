"""One port owner, one reader, lossless subscribers and a bounded response window."""

import math
import threading
import time

from embedded_auto.core.errors import ToolError


class SerialSession:
    _owners = {}
    _owners_lock = threading.Lock()

    def __init__(self, config: dict, factory):
        self.config = config
        self._factory = factory
        self._port = None
        self._thread = None
        self._stop = threading.Event()
        self._lifecycle = threading.RLock()
        self._io = threading.RLock()
        self._dispatch = threading.RLock()
        self._condition = threading.Condition(threading.RLock())
        self._subscribers = []
        self._buffer = bytearray()
        self._base = 0
        self._error = None
        self._capacity = config['serial'].get('max_buffer_bytes', 1024 * 1024)
        if type(self._capacity) is not int or self._capacity <= 0:
            raise ToolError('CONFIG_INVALID', 'max_buffer_bytes must be positive')
        self._key = str(config['serial']['port']).removeprefix('\\\\.\\').upper()

    def _fail(self, error):
        with self._condition:
            if self._error is None:
                self._error = error
            self._condition.notify_all()

    def _release_owner(self):
        with self._owners_lock:
            if self._owners.get(self._key) is self:
                del self._owners[self._key]

    def open(self):
        with self._lifecycle:
            if self._stop.is_set() and (self._port is not None or self._thread is not None):
                raise ToolError('SERIAL_CLOSE_FAILED', 'Previous close is incomplete; retry close first')
            if self._port is not None:
                self.check()
                return
            with self._owners_lock:
                if self._key in self._owners:
                    raise ToolError('SERIAL_PORT_BUSY', 'Serial port already owned', {'port': self._key})
                self._owners[self._key] = self
            self._error = None
            self._stop.clear()
            cfg = self.config['serial']
            try:
                self._port = self._factory(
                    port=None, baudrate=cfg.get('baudrate', 115200),
                    bytesize=cfg.get('bytesize', 8), parity=cfg.get('parity', 'N'),
                    stopbits=cfg.get('stopbits', 1),
                    timeout=min(max(float(cfg.get('read_timeout_s', 0.1)), 0.001), 0.1),
                    write_timeout=cfg.get('write_timeout_s', 1.0),
                    rtscts=False, dsrdtr=False, xonxoff=False,
                )
                self._port.rts = not self.config['reset']['asserted_rts_value']
                self._port.port = cfg['port']
                self._port.open()
                self._start_reader()
            except Exception as exc:
                failures = []
                self._stop.set()
                if self._port is not None:
                    if getattr(self._port, 'is_open', False):
                        # 句柄已打开时 close 前尽力把 RTS 恢复为已确认释放值；
                        # 不能用 set_rts（_stop 已置位会被拒绝），也不证明关闭后的物理电平。
                        try:
                            self._port.rts = not self.config['reset']['asserted_rts_value']
                        except Exception as release_exc:
                            failures.append(f'rts release failed: {release_exc}')
                    try:
                        self._port.close()
                        self._port = None
                    except Exception as close_exc:
                        failures.append(f'close failed: {close_exc}')
                if self._port is None:
                    self._release_owner()
                raise ToolError('SERIAL_OPEN_FAILED', str(exc),
                                {'cleanup_error': '; '.join(failures) if failures else None}) from exc

    def _start_reader(self):
        # Logging files must be open and subscribed before any bytes are read.
        if self._port is not None and self._subscribers and self._thread is None and not self._stop.is_set():
            self._thread = threading.Thread(target=self._receive, name=f'serial-{self._key}', daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._thread = None
                raise

    def subscribe(self, callback):
        with self._lifecycle, self._dispatch:
            self._subscribers.append(callback)
            try:
                self._start_reader()
            except Exception:
                self._subscribers.remove(callback)
                raise

        def unsubscribe():
            # Waiting for this lock also waits for an in-flight callback to finish.
            with self._dispatch:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def _publish(self, direction, data, timestamp):
        failure = None
        with self._dispatch:
            for callback in tuple(self._subscribers):
                try:
                    callback(direction, data, timestamp)
                except Exception as exc:
                    if failure is None:
                        failure = exc if isinstance(exc, ToolError) else ToolError(
                            'SERIAL_SUBSCRIBER_FAILED', str(exc), {'direction': direction})
        return failure

    def _receive(self):
        try:
            while not self._stop.is_set():
                data = self._port.read(4096)
                if not data:
                    self._stop.wait(0.001)
                    continue
                data = bytes(data)
                failure = self._publish('rx', data, time.time())
                with self._condition:
                    self._buffer.extend(data)
                    overflow = len(self._buffer) - self._capacity
                    if overflow > 0:
                        del self._buffer[:overflow]
                        self._base += overflow
                    if failure:
                        self._fail(failure)
                    self._condition.notify_all()
        except Exception as exc:
            if not self._stop.is_set():
                self._fail(ToolError('SERIAL_READ_FAILED', str(exc)))

    def check(self):
        with self._condition:
            if self._error is not None:
                raise self._error
            if self._port is None or self._stop.is_set():
                raise ToolError('SERIAL_NOT_OPEN', 'Serial session is not open')

    def write(self, data: bytes):
        # Use dispatch -> I/O ordering: an RX subscriber may itself send a reply.
        with self._dispatch, self._io:
            self.check()
            try:
                count = self._port.write(data)
                if count:
                    failure = self._publish('tx', bytes(data[:count]), time.time())
                    if failure:
                        self._fail(failure)
                if count != len(data):
                    raise ToolError('SERIAL_WRITE_FAILED', 'Partial serial write', {'written': count, 'expected': len(data)})
            except ToolError:
                raise
            except Exception as exc:
                raise ToolError('SERIAL_WRITE_FAILED', str(exc)) from exc
            self.check()

    def set_rts(self, value: bool):
        with self._io:
            # Historical RX/log errors still block normal I/O, not physical
            # cleanup of NRST on a port whose handle is still usable.
            if self._port is None or self._stop.is_set() or not self._port.is_open:
                raise ToolError('SERIAL_NOT_OPEN', 'Serial session is not open')
            try:
                self._port.rts = bool(value)
            except Exception as exc:
                error = ToolError('SERIAL_CONTROL_FAILED', str(exc))
                self._fail(error)
                raise error from exc

    def set_dtr(self, value: bool):
        with self._io:
            if self._port is None or self._stop.is_set() or not self._port.is_open:
                raise ToolError('SERIAL_NOT_OPEN', 'Serial session is not open')
            try:
                self._port.dtr = bool(value)
            except Exception as exc:
                error = ToolError('SERIAL_CONTROL_FAILED', str(exc))
                self._fail(error)
                raise error from exc

    def mark(self) -> int:
        with self._condition:
            return self._base + len(self._buffer)

    def received_since(self, mark: int) -> bytes:
        with self._condition:
            end = self._base + len(self._buffer)
            if type(mark) is not int or not self._base <= mark <= end:
                raise ToolError('SERIAL_BUFFER_RANGE', 'Receive mark is expired or invalid',
                                {'mark': mark, 'oldest': self._base, 'end': end})
            return bytes(self._buffer[mark - self._base:])

    def wait_for(self, predicate, timeout_s, ctx, start: int = 0) -> bytes:
        if not isinstance(timeout_s, (float, int)) or not math.isfinite(timeout_s) or timeout_s < 0:
            raise ToolError('CONFIG_INVALID', 'timeout_s must be finite and nonnegative')
        deadline = time.monotonic() + timeout_s
        while True:
            ctx.check()
            self.check()
            with self._condition:
                data = self.received_since(start)
                end = start + len(data)
            # Never hold the receive lock across caller code or a regex worker.
            # New bytes must keep flowing to raw logs while a matcher is busy.
            matched = predicate(data)
            ctx.check()
            self.check()
            if matched:
                return data
            with self._condition:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self.received_since(start)
                if self.mark() != end:
                    continue  # Data arrived during matching; avoid a lost wakeup.
                self._condition.wait(min(remaining, 0.02))

    def close(self):
        with self._lifecycle:
            if self._port is None and self._thread is None:
                self._release_owner()
                return
            with self._io:
                self._stop.set()
            with self._condition:
                self._condition.notify_all()
            failures = []
            if self._thread is not None and self._thread.is_alive():
                if self._thread is threading.current_thread():
                    raise ToolError('SERIAL_CLOSE_FAILED', 'Cannot join the receive thread from its callback')
                try:
                    cancel = getattr(self._port, 'cancel_read', None)
                    if cancel:
                        cancel()
                except Exception as exc:
                    failures.append(str(exc))
                self._thread.join(0.5)
            if self._port is not None:
                try:
                    self._port.close()
                    self._port = None
                except Exception as exc:
                    failures.append(str(exc))
            if self._thread is not None:
                self._thread.join(0.5)
                if self._thread.is_alive():
                    failures.append('Receive thread is still running; close may be retried')
                else:
                    self._thread = None
            if self._thread is None and self._port is None:
                self._release_owner()
            if failures:
                raise ToolError('SERIAL_CLOSE_FAILED', '; '.join(failures))
