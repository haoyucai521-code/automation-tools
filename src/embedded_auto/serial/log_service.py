"""Synchronous evidence writer; no unbounded queues and no decoder byte loss."""

import codecs
import json
import threading
import time

from embedded_auto.core.errors import ToolError


class LogService:
    def __init__(self, session, ctx, encoding='utf-8'):
        self.session = session
        self.ctx = ctx
        self.encoding = encoding
        self._lock = threading.RLock()
        self._unsubscribe = None
        self._files = {}
        self._decoder = codecs.getincrementaldecoder(encoding)(errors='replace')
        self._line_start = True
        self._finished = False
        self._error = None
        self._offset = 0

    def start(self):
        with self._lock:
            if self._unsubscribe:
                return
            if self._finished:
                raise ToolError('LOG_WRITE_FAILED', 'Log service cannot be restarted')
            self.ctx.path.mkdir(parents=True, exist_ok=True)
            try:
                for name in ('serial.bin', 'serial.log', 'serial.jsonl'):
                    self._files[name] = (self.ctx.path / name).open('xb' if name.endswith('.bin') else 'x',
                        **({} if name.endswith('.bin') else {'encoding': 'utf-8', 'newline': ''}))
                # Subscribe only after all evidence sinks are ready. This starts the reader.
                self._unsubscribe = self.session.subscribe(self._record)
            except Exception as exc:
                for file in self._files.values():
                    file.close()
                self._finished = True
                raise ToolError('LOG_WRITE_FAILED', str(exc)) from exc

    def _text(self, text, timestamp):
        file = self._files['serial.log']
        for part in text.splitlines(keepends=True):
            if self._line_start:
                file.write(f'[{timestamp:.6f}] ')
            file.write(part)
            self._line_start = part.endswith(('\n', '\r'))
        file.flush()

    def _record(self, direction: str, data: bytes, timestamp: float):
        with self._lock:
            failures = []
            offset = self._offset if direction == 'rx' else None
            if direction == 'rx':
                try:
                    self._files['serial.bin'].write(data)
                    self._files['serial.bin'].flush()
                except Exception as exc:
                    failures.append(str(exc))
                self._offset += len(data)
            # Always attempt each evidence sink, even when another sink failed.
            try:
                self._files['serial.jsonl'].write(json.dumps({
                    'direction': direction, 'timestamp': timestamp, 'hex': data.hex(),
                    'offset': offset, 'length': len(data),
                }) + '\n')
                self._files['serial.jsonl'].flush()
            except Exception as exc:
                failures.append(str(exc))
            if direction == 'rx':
                try:
                    self._text(self._decoder.decode(data, final=False), timestamp)
                except Exception as exc:
                    failures.append(str(exc))
            if failures:
                self._error = ToolError('LOG_WRITE_FAILED', '; '.join(failures))
                raise self._error

    def close(self):
        # Never hold the file lock while waiting for an in-flight subscription.
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        with self._lock:
            failures = []
            if self._files and not self._finished:
                try:
                    self._text(self._decoder.decode(b'', final=True), time.time())
                except Exception as exc:
                    failures.append(str(exc))
                self._finished = True
            for file in self._files.values():
                if not file.closed:
                    try:
                        file.close()
                    except Exception as exc:
                        failures.append(str(exc))
            if failures:
                raise ToolError('LOG_WRITE_FAILED', '; '.join(failures))
            if self._error:
                raise self._error
