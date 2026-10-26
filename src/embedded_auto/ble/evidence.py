"""原始证据与两个独立的当前占用账本；回调入口不做文件 IO。"""
from collections import deque
import json
import threading
import time

from embedded_auto.core.errors import ToolError
from embedded_auto.core.run_context import utc_now

from .models import Checkpoint


def validate_event(event):
    required = {"run_id", "backend", "event_id", "event_type", "timestamp", "monotonic_ns"}
    kind = event.get("event_type")
    if not isinstance(kind, str) or not kind:
        raise ToolError("LOG_WRITE_FAILED", "BLE 事件缺少有效 event_type")
    char_fields = {"epoch", "service_uuid", "characteristic_uuid"}
    if kind in ("read_request", "read_result", "write_request", "write_complete", "subscribe", "unsubscribe"):
        required |= char_fields
    if kind in ("read_result", "write_request", "notification"):
        required |= char_fields | {"direction", "payload_hex", "length"}
    if kind == "notification":
        required |= {"generation", "ingress_seq"}
    if kind in ("subscribe", "unsubscribe", "checkpoint"):
        required |= char_fields | {"generation"}
    if kind == "checkpoint":
        required |= {"ingress_seq"}
    if kind.startswith("scan_"):
        required |= {"scan_id"}
    if kind == "scan_candidate":
        required |= {"candidate_id"}
    if kind in ("connect_start", "connect_complete", "discover"):
        required |= {"epoch"}
    if kind == "write_complete":
        required |= {"request_event_id", "write_mode", "submitted", "ack_scope"}
    if kind == "error":
        required |= {"error_code", "stage", "message"}
    if kind == "cleanup":
        required |= {"stage", "action", "status"}
        if event.get("status") == "ERROR":
            required |= {"error_code", "message"}
    if required - event.keys():
        raise ToolError("LOG_WRITE_FAILED", f"BLE 事件缺字段: {sorted(required - event.keys())}")
    if "payload_hex" in event:
        try:
            valid = type(event.get("length")) is int and len(bytes.fromhex(event["payload_hex"])) == event["length"]
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ToolError("LOG_WRITE_FAILED", "BLE hex/length 不一致")
    return event


class BleEvidence:
    def __init__(self, ctx, backend):
        self.ctx, self.backend = ctx, backend
        self._id = 0
        self._file = None
        self.error = None
        try:
            self._file = (ctx.path / "ble_events.jsonl").open("x", encoding="utf-8", newline="\n")
        except OSError as exc:
            raise ToolError("LOG_WRITE_FAILED", str(exc)) from exc

    def emit(self, event_type, **fields):
        if self.error:
            raise self.error
        self._id += 1
        event = {"run_id": self.ctx.run_id, "backend": self.backend,
                 "event_id": self._id, "event_type": event_type,
                 "timestamp": utc_now(), "monotonic_ns": time.monotonic_ns(),
                 **{k: v for k, v in fields.items() if v is not None}}
        try:
            validate_event(event)
            self._file.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._file.flush()
        except (OSError, ValueError, ToolError) as exc:
            self.error = exc if isinstance(exc, ToolError) else ToolError("LOG_WRITE_FAILED", str(exc))
            self.error.data.update({key: event[key] for key in
                                    ("epoch", "generation", "service_uuid", "characteristic_uuid")
                                    if key in event})
            raise self.error
        return event

    def close(self):
        if self._file is not None:
            file, self._file = self._file, None
            try:
                file.close()
            except OSError as exc:
                raise ToolError("LOG_WRITE_FAILED", str(exc)) from exc


class NotificationBuffer:
    """队列容量在出队恢复；窗口容量在淘汰恢复，历史统计不用于限流。"""
    def __init__(self, ingress_limits, window_limits, clock_ns=time.monotonic_ns):
        self.ingress_limits, self.window_limits = ingress_limits, window_limits
        self.clock_ns = clock_ns
        self._queue, self._window = deque(), deque()
        self._lock = threading.RLock()
        self._seq = 0
        self._evicted = {}
        self.queue_bytes = self.window_bytes = 0
        self.accepted_total = self.rejected_total = self.late_total = 0
        self.discarded_total = 0
        self.error = None
        self.closed = False

    @staticmethod
    def _key(event):
        return event["epoch"], event["generation"], event["ref"]

    def push(self, epoch, generation, char, data):
        with self._lock:
            if self.closed:
                self.late_total += 1
                return False
            self._seq += 1
            try:
                payload = bytes(data)
            except (ValueError, TypeError) as exc:
                self.error = self.error or ToolError("BLE_GATT_FAILED", f"无效通知数据: {exc}")
                return False
            size = len(payload)
            too_large = size > min(self.ingress_limits["max_payload_bytes"],
                                   self.window_limits["max_payload_bytes"])
            if (too_large or len(self._queue) >= self.ingress_limits["max_events"] or
                    self.queue_bytes + size > self.ingress_limits["max_payload_bytes"]):
                self.rejected_total += 1
                self.error = self.error or ToolError("BLE_EVENT_OVERFLOW", "通知入口容量不足或单条超限",
                                                    {"stage": "notification_ingress", "epoch": epoch,
                                                     "generation": generation, "service_uuid": char.service_uuid,
                                                     "characteristic_uuid": char.uuid})
                return False
            event = {"epoch": epoch, "generation": generation, "ref": char.ref,
                     "service_uuid": char.service_uuid, "characteristic_uuid": char.uuid,
                     "ingress_seq": self._seq, "monotonic_ns": self.clock_ns(),
                     "payload_hex": payload.hex(), "length": size, "direction": "rx"}
            self._queue.append(event)
            self.queue_bytes += size
            self.accepted_total += 1
            return True

    def take(self):
        with self._lock:
            if not self._queue:
                return None
            event = self._queue.popleft()
            self.queue_bytes -= event["length"]
            return event

    def retain(self, event):
        with self._lock:
            # 单条大小已在入口检查，此处不能为不可能容纳的事件清空窗口。
            if event["length"] > self.window_limits["max_payload_bytes"]:
                raise ToolError("BLE_EVENT_OVERFLOW", "单条事件超过窗口容量")
            while (len(self._window) >= self.window_limits["max_events"] or
                   self.window_bytes + event["length"] > self.window_limits["max_payload_bytes"]):
                old = self._window.popleft()
                self.window_bytes -= old["length"]
                self._evicted[self._key(old)] = old["ingress_seq"]
            self._window.append(event)
            self.window_bytes += event["length"]

    def mark(self, epoch, generation, ref):
        with self._lock:
            return Checkpoint(epoch, generation, ref, self._seq)

    def events_after(self, mark, deadline_ns):
        with self._lock:
            key = mark.epoch, mark.generation, mark.ref
            if self.closed or self._evicted.get(key, 0) > mark.seq:
                raise ToolError("BLE_WINDOW_EXPIRED", "checkpoint 对应窗口已过期")
            return [e for e in self._window if self._key(e) == key and
                    e["ingress_seq"] > mark.seq and e["monotonic_ns"] <= deadline_ns]

    def check(self):
        with self._lock:
            if self.error:
                raise self.error

    def stats(self):
        with self._lock:
            return {"current_events": len(self._queue), "current_payload_bytes": self.queue_bytes,
                    "window_events": len(self._window), "window_payload_bytes": self.window_bytes,
                    "accepted_total": self.accepted_total, "rejected_total": self.rejected_total,
                    "late_total": self.late_total, "discarded_total": self.discarded_total}

    def close(self):
        with self._lock:
            self.closed = True
            self.discarded_total += len(self._queue)
            self._queue.clear()
            self._window.clear()
            self.queue_bytes = self.window_bytes = 0
