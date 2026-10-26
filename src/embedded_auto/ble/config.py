"""BLE 的严格预检：配置不执行代码，整套用例先于任何后端操作验证。"""
import copy
import re
from pathlib import Path

from embedded_auto.core.config import number
from embedded_auto.core.errors import ToolError
from embedded_auto.core.paths import resolve_path


PROPERTIES = {"read", "write", "write-without-response", "notify", "indicate",
              "broadcast", "authenticated-signed-writes", "extended-properties"}


def require(condition, message):
    if not condition:
        raise ToolError("CONFIG_INVALID", message)


def mapping(value, allowed, name, required=()):
    require(isinstance(value, dict), f"{name} 必须是映射")
    require(not (set(value) - set(allowed)), f"{name} 含未知字段")
    require(set(required) <= set(value), f"{name} 缺少必需字段")
    return value


def nonempty(value, name):
    require(isinstance(value, str) and bool(value.strip()), f"{name} 必须是非空字符串")
    return value


def uuid128(value):
    require(isinstance(value, str) and re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value) is not None,
        "UUID 必须是完整的 128 位标准格式")
    return value.lower()


def address(value):
    require(isinstance(value, str) and re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", value) is not None,
            "真实设备只能按完整 MAC 地址选择，名称不是连接标识")
    return value.upper()


def hex_bytes(value, *, empty=True):
    require(isinstance(value, str), "hex 必须是字符串")
    try:
        result = bytes.fromhex(value)
    except ValueError as exc:
        raise ToolError("CONFIG_INVALID", "无效的 hex 数据") from exc
    require(empty or bool(result), "hex 数据不能为空")
    return result


def positive(value, name):
    return number(value, name, 0.000001)


def properties(value):
    require(isinstance(value, list) and all(isinstance(p, str) and p in PROPERTIES for p in value),
            "特征属性必须是已知属性列表")
    require(len(value) == len(set(value)), "特征属性不能重复")
    return value


def prepare_config(raw, config_path, overrides=None):
    config = copy.deepcopy(raw)
    mapping(config, ("version", "app", "ble"), "config", ("version", "app", "ble"))
    require(type(config["version"]) is int and config["version"] == 1, "config.version 必须为 1")
    app = mapping(config["app"], ("workspace", "runs_dir", "default_timeout_s"), "app")
    base = Path(config_path).resolve().parent
    # 与通用 report 的默认值相同；示例配置显式使用 ../..。
    workspace = resolve_path(app.get("workspace", ".."), base)
    app["workspace"] = str(workspace)
    app["runs_dir"] = str(resolve_path(app.get("runs_dir", "runs"), workspace))
    app["default_timeout_s"] = positive(app.get("default_timeout_s", 30), "app.default_timeout_s")
    ble = mapping(config["ble"], ("backend", "device", "fake_profile", "name", "service_uuids",
                  "scan_timeout_s", "connect_timeout_s", "operation_timeout_s", "cleanup_timeout_s",
                  "ingress_limits", "window_limits"), "ble", ("backend",))
    for key, value in (overrides or {}).items():
        require(key in ("backend", "device", "name", "scan_timeout_s"), "未知 BLE 配置覆盖")
        ble[key] = value
    require(ble["backend"] in ("fake", "bleak"), "ble.backend 仅支持 fake/bleak")
    if ble.get("device") is not None:
        ble["device"] = address(ble["device"]) if ble["backend"] == "bleak" else nonempty(ble["device"], "device")
    if "name" in ble:
        nonempty(ble["name"], "name")
    if "service_uuids" in ble:
        require(isinstance(ble["service_uuids"], list), "service_uuids 必须是列表")
        ble["service_uuids"] = [uuid128(v) for v in ble["service_uuids"]]
    for key, default in (("scan_timeout_s", 3), ("connect_timeout_s", 5),
                         ("operation_timeout_s", 2), ("cleanup_timeout_s", 5)):
        ble[key] = positive(ble.get(key, default), f"ble.{key}")
    for key, defaults in (("ingress_limits", (128, 65536)), ("window_limits", (256, 131072))):
        limits = ble.setdefault(key, {})
        mapping(limits, ("max_events", "max_payload_bytes"), key)
        for field, default in zip(("max_events", "max_payload_bytes"), defaults):
            v = limits.setdefault(field, default)
            require(type(v) is int and v > 0, f"{key}.{field} 必须是正整数")
    if ble["backend"] == "fake":
        ble["fake_profile"] = str(resolve_path(nonempty(ble.get("fake_profile"), "fake_profile"), base))
    elif "fake_profile" in ble:
        ble["fake_profile"] = str(resolve_path(ble["fake_profile"], base))
    return config


def validate_profile(raw):
    profile = copy.deepcopy(raw)
    mapping(profile, ("version", "device", "services"), "profile", ("version", "device", "services"))
    require(type(profile["version"]) is int and profile["version"] == 1, "profile.version 必须为 1")
    device = mapping(profile["device"], ("id", "name"), "profile.device", ("id",))
    nonempty(device["id"], "device.id")
    if "name" in device:
        nonempty(device["name"], "device.name")
    require(isinstance(profile["services"], list), "services 必须是列表")
    for service in profile["services"]:
        mapping(service, ("uuid", "characteristics"), "service", ("uuid", "characteristics"))
        service["uuid"] = uuid128(service["uuid"])
        require(isinstance(service["characteristics"], list), "characteristics 必须是列表")
        for char in service["characteristics"]:
            mapping(char, ("uuid", "properties", "initial_hex", "on_write",
                           "max_write_with_response", "max_write_without_response"),
                    "characteristic", ("uuid", "properties"))
            char["uuid"] = uuid128(char["uuid"])
            properties(char["properties"])
            char["initial_hex"] = hex_bytes(char.get("initial_hex", "")).hex()
            effect = mapping(char.setdefault("on_write", {}), ("store", "emit_notification"), "on_write")
            for key in effect:
                require(type(effect[key]) is bool, f"on_write.{key} 必须是布尔值")
            for key in ("max_write_with_response", "max_write_without_response"):
                if key in char:
                    require(type(char[key]) is int and char[key] > 0, f"{key} 必须是正整数")
    return profile


def validate_expect(value):
    mapping(value, ("equals_hex", "contains_hex", "length"), "expect")
    require(bool(value), "expect 不能为空")
    result = copy.deepcopy(value)
    for key in result:
        if key == "length":
            require(type(result[key]) is int and result[key] >= 0, "length 必须是非负整数")
        else:
            result[key] = hex_bytes(result[key], empty=key != "contains_hex").hex()
    return result


def validate_suite(raw, operation_timeout_s=2):
    suite = copy.deepcopy(raw)
    mapping(suite, ("version", "characteristics", "tests"), "suite", ("version", "characteristics", "tests"))
    require(type(suite["version"]) is int and suite["version"] == 1, "suite.version 必须为 1")
    require(isinstance(suite["characteristics"], dict) and bool(suite["characteristics"]), "characteristics 不能为空")
    for alias, selector in suite["characteristics"].items():
        nonempty(alias, "characteristic alias")
        mapping(selector, ("service_uuid", "uuid", "required_properties"), "selector", ("service_uuid", "uuid"))
        selector["service_uuid"] = uuid128(selector["service_uuid"])
        selector["uuid"] = uuid128(selector["uuid"])
        properties(selector.setdefault("required_properties", []))
    require(isinstance(suite["tests"], list) and bool(suite["tests"]), "tests 不能为空")
    names = set()
    fields = {
        "read": {"action", "characteristic", "expect"},
        "write": {"action", "characteristic", "hex", "response"},
        "subscribe": {"action", "characteristic"},
        "checkpoint": {"action", "characteristic", "id"},
        "wait_notification": {"action", "characteristic", "after", "timeout_s", "expect"},
    }
    for case in suite["tests"]:
        mapping(case, ("name", "steps"), "case", ("name", "steps"))
        name = nonempty(case["name"], "case.name")
        require(name not in names, "case.name 不能重复")
        names.add(name)
        require(isinstance(case["steps"], list) and bool(case["steps"]), "steps 不能为空")
        subscribed, checkpoints, asserts = set(), {}, 0
        for step in case["steps"]:
            require(isinstance(step, dict) and isinstance(step.get("action"), str), "step.action 无效")
            action = step["action"]
            require(action in fields, "未知 step.action")
            mapping(step, fields[action], "step", ("action", "characteristic"))
            alias = step["characteristic"]
            require(isinstance(alias, str) and alias in suite["characteristics"], "未知 characteristic alias")
            if "expect" in step:
                step["expect"] = validate_expect(step["expect"])
                asserts += 1
            if action == "write":
                require("hex" in step and type(step.get("response")) is bool, "write 必须有 hex 和布尔 response")
                step["hex"] = hex_bytes(step["hex"], empty=False).hex()
            elif action == "subscribe":
                subscribed.add(alias)
            elif action == "checkpoint":
                label = nonempty(step.get("id"), "checkpoint.id")
                require(alias in subscribed and label not in checkpoints, "checkpoint 必须在订阅后且不可重复")
                checkpoints[label] = alias
            elif action == "wait_notification":
                require(alias in subscribed and isinstance(step.get("after"), str) and
                        checkpoints.get(step["after"]) == alias, "wait 引用了未建立或跨特征的 checkpoint")
                timeout = positive(step.setdefault("timeout_s", operation_timeout_s), "wait.timeout_s")
                require(timeout <= operation_timeout_s, "wait.timeout_s 不能超过 operation_timeout_s")
                require("expect" in step, "wait_notification 必须有 expect")
        require(asserts > 0, "每例至少需要一个 expect")
    return suite


def has_writes(suite):
    return any(s["action"] == "write" for c in suite["tests"] for s in c["steps"])


def authorize(config, writes, allow_write, confirm_device=None):
    if writes:
        require(allow_write is True, "写入必须在本次调用给出 --allow-write")
        if config["ble"]["backend"] == "bleak":
            require(confirm_device is not None and address(confirm_device) == config["ble"].get("device"),
                    "真实写入必须 --confirm-device 与目标地址完全相同")
