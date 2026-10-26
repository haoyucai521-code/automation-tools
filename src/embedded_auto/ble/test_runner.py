"""BLE 回调边界上的字节断言，不复用串口字节流测试引擎。"""
from dataclasses import asdict

from embedded_auto.core.errors import ToolError


def assertions(data, expect):
    result = []
    for rule, expected in expect.items():
        if rule == "length":
            actual, success = len(data), len(data) == expected
        else:
            value = bytes.fromhex(expected)
            actual = data.hex()
            success = data == value if rule == "equals_hex" else value in data
        result.append({"rule": rule, "expected": expected, "actual": actual, "success": success})
    return result


class TestRunner:
    def __init__(self, session, suite):
        self.session, self.suite = session, suite
        self.cases = [{"name": c["name"], "status": "SKIP", "verdict": "NOT_EVALUATED",
                       "steps": [{"action": s["action"], "characteristic": s["characteristic"],
                                  "status": "SKIP", "verdict": "NOT_EVALUATED", "assertions": []}
                                 for s in c["steps"]]} for c in suite["tests"]]
        self.preflight = []
        self.error = None
        self.verdict = "NOT_EVALUATED"

    def precheck(self):
        chars = {}
        for alias, selector in self.suite["characteristics"].items():
            try:
                char = self.session.resolve(selector)
                missing = set(selector["required_properties"]) - set(char.properties)
                if missing:
                    raise ToolError("BLE_CAPABILITY_UNSUPPORTED", f"缺少声明属性: {sorted(missing)}")
                chars[alias] = char
                self.preflight.append({"characteristic": alias, "success": True})
            except ToolError as exc:
                if exc.code not in ("BLE_CHARACTERISTIC_NOT_FOUND", "BLE_CAPABILITY_UNSUPPORTED"):
                    raise
                self.preflight.append({"characteristic": alias, "success": False, "message": str(exc)})
        if any(not item["success"] for item in self.preflight):
            raise ToolError("BLE_ASSERTION_FAILED", "GATT 前置规格断言不满足", {"stage": "gatt_preflight"})
        return chars

    async def run(self):
        try:
            chars = self.precheck()
        except ToolError as exc:
            self.error = exc
            if exc.code == "BLE_ASSERTION_FAILED":
                self.verdict = "FAIL"
            return self
        for case, report in zip(self.suite["tests"], self.cases):
            checkpoints = {}
            report["status"] = "COMPLETED"
            for step, item in zip(case["steps"], report["steps"]):
                try:
                    self.session.check()
                    char, action = chars[step["characteristic"]], step["action"]
                    if action == "read":
                        data, event_id = await self.session.read(char)
                        item.update(actual_hex=data.hex(), event_ids=[event_id])
                        if "expect" in step:
                            item["assertions"] = assertions(data, step["expect"])
                    elif action == "write":
                        event = await self.session.write(char, bytes.fromhex(step["hex"]), step["response"])
                        item["event_ids"] = [event["event_id"]]
                    elif action == "subscribe":
                        await self.session.subscribe(char)
                    elif action == "checkpoint":
                        mark = self.session.checkpoint(char)
                        checkpoints[step["id"]] = mark
                        item["checkpoint"] = asdict(mark)
                    elif action == "wait_notification":
                        mark = checkpoints[step["after"]]
                        event = await self.session.wait(
                            mark, step["timeout_s"],
                            lambda data: all(a["success"] for a in assertions(data, step["expect"])))
                        item.update(checkpoint=asdict(mark), timeout_s=step["timeout_s"],
                                    event_ids=[e["event_id"] for e in self.session.last_wait_events])
                        if event is None:
                            item["status"], item["verdict"] = "COMPLETED", "FAIL"
                            item["assertions"] = [{"rule": "matching_notification", "success": False,
                                                   "expected": step["expect"],
                                                   "observed_hex": [e["payload_hex"] for e in self.session.last_wait_events]}]
                            raise ToolError("BLE_ASSERTION_FAILED", "有效通知窗口内没有匹配事件")
                        item["assertions"] = assertions(bytes.fromhex(event["payload_hex"]), step["expect"])
                        item["matched_event_id"] = event["event_id"]
                    item["status"] = "COMPLETED"
                    if item["assertions"]:
                        item["verdict"] = "PASS" if all(a["success"] for a in item["assertions"]) else "FAIL"
                        if item["verdict"] == "FAIL":
                            raise ToolError("BLE_ASSERTION_FAILED", "BLE 字节断言不满足")
                except ToolError as exc:
                    self.error = exc
                    item.update(error_code=exc.code, message=str(exc), error_data=exc.data)
                    if exc.code == "BLE_ASSERTION_FAILED":
                        item.update(status="COMPLETED", verdict="FAIL")
                        report.update(status="COMPLETED", verdict="FAIL")
                    else:
                        item.update(status="ERROR", verdict="NOT_EVALUATED")
                        report.update(status="ERROR", verdict="NOT_EVALUATED")
                    break
            else:
                report["verdict"] = "PASS"
            if self.error:
                break
        if any(c["verdict"] == "FAIL" for c in self.cases):
            self.verdict = "FAIL"
        elif self.cases and all(c["status"] == "COMPLETED" and c["verdict"] == "PASS" for c in self.cases):
            self.verdict = "PASS"
        return self

    def data(self):
        return {"cases": self.cases, "preflight": self.preflight, "verdict": self.verdict}
