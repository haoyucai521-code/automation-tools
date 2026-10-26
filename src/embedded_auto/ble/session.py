"""单连接 BLE 生命周期；局部调用、总截止和独立清理预算分别管理。"""
import asyncio
import time
import uuid

from embedded_auto.core.errors import ToolError

from .evidence import NotificationBuffer


class BleSession:
    def __init__(self, backend, config, ctx, evidence, notice=None):
        self.backend, self.config, self.ctx, self.evidence = backend, config["ble"], ctx, evidence
        self.notice = notice or (lambda message: None)
        self.buffer = NotificationBuffer(self.config["ingress_limits"], self.config["window_limits"])
        self.epoch = None
        self.device = None
        self.services = []
        self.subscriptions = {}
        self._tasks = set()
        self._fatal = None
        self._closing = False
        self._cleanup = None
        backend.on_disconnect = self._disconnected

    def _disconnected(self, reason):
        if not self._closing:
            self._fatal = ToolError("BLE_DISCONNECTED", str(reason),
                                    {"stage": "disconnected", "epoch": self.epoch})

    def _track(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)

        def done(value):
            self._tasks.discard(value)
            if not value.cancelled():
                value.exception()  # 异常仍由 await/result 返回；回收晚到任务的诊断异常。
        task.add_done_callback(done)
        return task

    def drain(self):
        # 每轮工作量有界，持续回调不能饿死总截止、取消和 finally。
        for _ in range(self.config["ingress_limits"]["max_events"]):
            event = self.buffer.take()
            if event is None:
                break
            entry = self.subscriptions.get(event["ref"])
            valid = (entry is not None and entry["generation"] == event["generation"] and
                     event["epoch"] == self.epoch and entry["active"])
            fields = dict(event)
            if not valid:
                fields["excluded_reason"] = "old_connection_or_subscription"
            recorded = self.evidence.emit("notification", **fields)
            event["event_id"] = recorded["event_id"]
            if valid:
                self.buffer.retain(event)

    def check(self):
        self.ctx.check()  # 用户取消优先于总截止，总截止优先于局部/后台错误。
        if self._fatal:
            raise self._fatal
        self.buffer.check()

    def _timeout(self, stage, budget, deadline):
        return ToolError("BLE_OPERATION_TIMEOUT", f"{stage} 局部调用超时",
                         {"stage": stage, "timeout_scope": "local", "local_budget_s": budget,
                          "local_deadline": deadline, "run_deadline": self.ctx.deadline,
                          "observed_at": time.monotonic()})

    async def call(self, stage, budget, factory, *, context=None):
        deadline = time.monotonic() + budget
        task = None
        try:
            self.check()
            task = self._track(factory())
            while True:
                self.ctx.check()
                self.drain()
                self.check()
                if time.monotonic() >= deadline:
                    raise self._timeout(stage, budget, deadline)
                if task.done():
                    try:
                        return task.result()
                    except ToolError:
                        raise
                    except asyncio.CancelledError as exc:
                        raise ToolError("BLE_GATT_FAILED", "后端任务意外取消", {"stage": stage}) from exc
                    except Exception as exc:
                        code = "BLE_CONNECT_FAILED" if stage == "connect" else "BLE_GATT_FAILED"
                        raise ToolError(code, str(exc), {"stage": stage, "exception_type": type(exc).__name__}) from exc
                await asyncio.wait({task}, timeout=min(.02, self.ctx.remaining, max(0, deadline - time.monotonic())))
        except asyncio.CancelledError as exc:
            self.ctx.cancelled.set()
            raise ToolError("CANCELLED", "用户取消 BLE 操作",
                            {"stage": stage, **(context or {})}) from exc
        except ToolError as exc:
            exc.data.setdefault("stage", stage)
            for key, value in (context or {}).items():
                exc.data.setdefault(key, value)
            if exc.code == "WORKFLOW_TIMEOUT":
                exc.data.update(stage=stage, timeout_scope="run", local_budget_s=budget,
                                local_deadline=deadline, run_deadline=self.ctx.deadline,
                                observed_at=time.monotonic(), selection_reason="run_deadline_precedes_local",
                                local_expired=time.monotonic() >= deadline, run_expired=True)
            raise
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.sleep(0)

    def context(self, char):
        return {"epoch": self.epoch, "service_uuid": char.service_uuid, "characteristic_uuid": char.uuid}

    async def scan(self):
        self.scan_id = uuid.uuid4().hex
        self.evidence.emit("scan_start", scan_id=self.scan_id)
        budget = self.config["scan_timeout_s"] + 2 * self.config["operation_timeout_s"]
        candidates = await self.call("scan", budget, lambda: self.backend.scan(self.config["scan_timeout_s"]),
                                     context={"scan_id": self.scan_id})
        for candidate in candidates:
            self.evidence.emit("scan_candidate", scan_id=self.scan_id, candidate_id=candidate.candidate_id,
                               address=candidate.address, name=candidate.name)
        name, services = self.config.get("name"), set(self.config.get("service_uuids", []))
        candidates = [c for c in candidates if (name is None or c.name == name) and
                      services <= set(c.service_uuids)]
        self.evidence.emit("scan_end", scan_id=self.scan_id, count=len(candidates))
        return candidates

    async def connect(self, candidates):
        matched = [c for c in candidates if c.address == self.config.get("device")]
        if len(matched) != 1:
            code = "BLE_DEVICE_NOT_FOUND" if not matched else "BLE_DEVICE_AMBIGUOUS"
            raise ToolError(code, "本次扫描未得到唯一明确目标", {"stage": "select_device"})
        self.device = matched[0]
        self.epoch = uuid.uuid4().hex
        self.notice(f"目标: {self.device.address} {self.device.name or ''} ({self.device.candidate_id})")
        self.evidence.emit("connect_start", epoch=self.epoch, candidate_id=self.device.candidate_id,
                           address=self.device.address)
        await self.call("connect", self.config["connect_timeout_s"],
                        lambda: self.backend.connect(self.device.candidate_id, self.config["connect_timeout_s"]),
                        context={"epoch": self.epoch, "candidate_id": self.device.candidate_id})
        self.evidence.emit("connect_complete", epoch=self.epoch, candidate_id=self.device.candidate_id)
        self.services = await self.call("discover", self.config["operation_timeout_s"], self.backend.discover,
                                        context={"epoch": self.epoch})
        self.evidence.emit("discover", epoch=self.epoch, count=len(self.services))

    def resolve(self, selector):
        services = [s for s in self.services if s.uuid == selector["service_uuid"]]
        if len(services) > 1:
            raise ToolError("BLE_CHARACTERISTIC_AMBIGUOUS", "重复的服务实例，拒绝自动选择")
        chars = [c for s in services for c in s.characteristics if c.uuid == selector["uuid"]]
        if len(chars) != 1:
            code = "BLE_CHARACTERISTIC_NOT_FOUND" if not chars else "BLE_CHARACTERISTIC_AMBIGUOUS"
            raise ToolError(code, "指定特征不存在或不唯一")
        return chars[0]

    @staticmethod
    def capable(char, prop):
        if prop not in char.properties:
            raise ToolError("BLE_CAPABILITY_UNSUPPORTED", f"特征不支持 {prop}")

    async def read(self, char):
        self.capable(char, "read")
        self.evidence.emit("read_request", **self.context(char))
        data = bytes(await self.call("read", self.config["operation_timeout_s"], lambda: self.backend.read(char.ref),
                                     context=self.context(char)))
        event = self.evidence.emit("read_result", **self.context(char), direction="rx",
                                   payload_hex=data.hex(), length=len(data))
        return data, event["event_id"]

    async def write(self, char, data, response):
        prop = "write" if response else "write-without-response"
        self.capable(char, prop)
        limit = char.max_write_with_response if response else char.max_write_without_response
        if limit is None or len(data) > limit or (response and len(data) > 512):
            raise ToolError("BLE_CAPABILITY_UNSUPPORTED", "写长度超过已确认上限；不自动拆包")
        mode = "with-response" if response else "without-response"
        self.notice(f"写入目标: {self.device.address} {char.uuid} mode={mode} bytes={len(data)}")
        request = self.evidence.emit("write_request", **self.context(char), direction="tx",
                                     payload_hex=data.hex(), length=len(data), write_mode=mode)
        try:
            await self.call("write", self.config["operation_timeout_s"],
                            lambda: self.backend.write(char.ref, data, response), context=self.context(char))
        except ToolError as exc:
            exc.data.update(write_effect="unknown", request_event_id=request["event_id"])
            raise
        return self.evidence.emit("write_complete", **self.context(char), request_event_id=request["event_id"],
                                   write_mode=mode, submitted=True,
                                   ack_scope="ATT_response_not_business_confirmation" if response else "local_submission_only")

    async def subscribe(self, char):
        self.capable(char, "notify")
        if char.ref in self.subscriptions:
            return self.subscriptions[char.ref]
        entry = {"char": char, "generation": uuid.uuid4().hex, "token": None, "active": True}
        self.subscriptions[char.ref] = entry  # 在 start_notify 前建立入口，保留部分注册供 finally 清理。
        epoch, generation = self.epoch, entry["generation"]
        entry["start_checkpoint"] = self.buffer.mark(epoch, generation, char.ref)
        entry["start_monotonic_ns"] = time.monotonic_ns()
        self.evidence.emit("checkpoint", **self.context(char), generation=generation,
                           ingress_seq=entry["start_checkpoint"].seq, scope="subscription_start")
        callback = lambda data: self.buffer.push(epoch, generation, char, data)
        entry["token"] = await self.call("subscribe", self.config["operation_timeout_s"],
                                         lambda: self.backend.subscribe(char.ref, callback),
                                         context={**self.context(char), "generation": generation})
        self.evidence.emit("subscribe", **self.context(char), generation=generation)
        return entry

    def checkpoint(self, char):
        entry = self.subscriptions.get(char.ref)
        if entry is None or not entry["active"]:
            raise ToolError("CONFIG_INVALID", "checkpoint 需要当前有效订阅")
        self.check()
        mark = self.buffer.mark(self.epoch, entry["generation"], char.ref)
        self.evidence.emit("checkpoint", **self.context(char), generation=mark.generation, ingress_seq=mark.seq)
        return mark

    async def wait(self, mark, timeout_s, predicate=None, *, deadline_ns=None):
        deadline_ns = (time.monotonic_ns() + int(timeout_s * 1_000_000_000)
                       if deadline_ns is None else deadline_ns)
        deadline = deadline_ns / 1_000_000_000
        self.last_wait_events = []
        try:
            while True:
                self.ctx.check()
                self.drain()
                self.check()
                events = self.buffer.events_after(mark, deadline_ns)
                self.last_wait_events = events
                if predicate is not None:
                    for event in events:
                        if predicate(bytes.fromhex(event["payload_hex"])):
                            self.check()
                            return event
                if time.monotonic() >= deadline:
                    return None
                await asyncio.sleep(min(.01, self.ctx.remaining, max(0, deadline - time.monotonic())))
        except asyncio.CancelledError as exc:
            self.ctx.cancelled.set()
            raise ToolError("CANCELLED", "用户取消通知等待",
                            {"stage": "wait_notification", "epoch": mark.epoch,
                             "generation": mark.generation}) from exc
        except ToolError as exc:
            exc.data.setdefault("stage", "wait_notification")
            exc.data.setdefault("epoch", mark.epoch)
            exc.data.setdefault("generation", mark.generation)
            if exc.code == "WORKFLOW_TIMEOUT":
                exc.data.update(timeout_scope="run", local_budget_s=timeout_s,
                                local_deadline=deadline, run_deadline=self.ctx.deadline,
                                observed_at=time.monotonic(), selection_reason="run_deadline_precedes_window",
                                local_expired=time.monotonic() >= deadline, run_expired=True)
            raise

    async def close(self):
        if self._cleanup is not None:
            return self._cleanup
        self._closing = True
        self._cleanup = []
        deadline = time.monotonic() + self.config["cleanup_timeout_s"]

        async def attempt(action, factory, context=None):
            task = self._track(factory())
            item = {"action": action, "success": False}
            try:
                done, _ = await asyncio.wait({task}, timeout=max(0, deadline - time.monotonic()))
                if not done:
                    raise ToolError("BLE_OPERATION_TIMEOUT", "清理预算耗尽",
                                    {"stage": "cleanup", "timeout_scope": "cleanup",
                                     "local_budget_s": self.config["cleanup_timeout_s"],
                                     "local_deadline": deadline, "observed_at": time.monotonic()})
                task.result()
                item["success"] = True
            except (Exception, asyncio.CancelledError) as exc:
                item.update(error_code=getattr(exc, "code", "CLEANUP_FAILED"), message=str(exc),
                            data=getattr(exc, "data", {}))
            finally:
                if not task.done():
                    task.cancel()
                self._cleanup.append(item)
                try:
                    fields = {} if item["success"] else {
                        **item.get("data", {}), "error_code": item["error_code"], "message": item["message"]}
                    fields.update(context or {})
                    fields.update(stage="cleanup", action=action,
                                  status="COMPLETED" if item["success"] else "ERROR")
                    self.evidence.emit("cleanup", **fields)
                    if action == "unsubscribe" and item["success"]:
                        self.evidence.emit("unsubscribe", **context)
                except ToolError as exc:
                    self._cleanup.append({"action": "cleanup_evidence", "success": False,
                                          "error_code": exc.code, "message": str(exc)})

        try:
            self.drain()
        except ToolError as exc:
            self._cleanup.append({"action": "drain_events", "success": False,
                                  "error_code": exc.code, "message": str(exc)})
        for entry in self.subscriptions.values():
            if entry["token"] is not None:
                await attempt("unsubscribe", lambda e=entry: self.backend.unsubscribe(e["token"]),
                              {**self.context(entry["char"]), "generation": entry["generation"]})
            entry["active"] = False
        await attempt("disconnect", self.backend.disconnect)
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            _, pending = await asyncio.wait(tuple(self._tasks), timeout=max(0, deadline - time.monotonic()))
            if pending:
                self._cleanup.append({"action": "join_tasks", "success": False,
                                      "error_code": "BLE_OPERATION_TIMEOUT", "message": "后台任务未在清理预算内结束"})
        try:
            self.drain()
        except ToolError as exc:
            self._cleanup.append({"action": "drain_events", "success": False,
                                  "error_code": exc.code, "message": str(exc)})
        self.buffer.close()
        return self._cleanup
