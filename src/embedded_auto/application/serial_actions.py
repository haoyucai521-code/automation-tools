"""单次串口动作；接收快照使用已归档的字节，不另读串口。"""
import time

from embedded_auto.core.config import number
from embedded_auto.core.errors import ToolError


SERIAL_ACTIONS = ("serial read", "serial send", "serial capture")
MAX_SEND_BYTES = 4096
PREVIEW_BYTES = 256


def serial_options(operation, duration, send_text, send_hex, listen_after):
    if operation != "serial send":
        return number(duration, "duration", 0.001), None
    duration = number(listen_after, "listen-after", 0)
    if (send_text is None) == (send_hex is None):
        raise ToolError("CONFIG_INVALID", "必须明确提供 --text 或 --hex 中的一种")
    if (send_text is not None and not isinstance(send_text, str)) or (send_hex is not None and not isinstance(send_hex, str)):
        raise ToolError("CONFIG_INVALID", "发送内容必须是文本或十六进制字符串")
    try:
        payload = send_text.encode("utf-8") if send_text is not None else bytes.fromhex(send_hex)
    except (TypeError, ValueError) as exc:
        raise ToolError("CONFIG_INVALID", "发送数据不是有效的 UTF-8 文本或十六进制字节") from exc
    if not 1 <= len(payload) <= MAX_SEND_BYTES:
        raise ToolError("CONFIG_INVALID", f"一次发送须为 1–{MAX_SEND_BYTES} 字节")
    return duration, payload


def check_serial_budget(config, ctx, duration, payload):
    ctx.check()
    write_budget = config["serial"].get("write_timeout_s", 1) if payload is not None else 0
    if write_budget + duration >= ctx.remaining:
        raise ToolError("WORKFLOW_TIMEOUT", "剩余预算不足以覆盖本次串口动作",
                        {"window_s": duration, "write_timeout_s": write_budget, "remaining_s": ctx.remaining})


def perform_serial_action(config, ctx, session, operation, duration, payload, data):
    rx_path = ctx.path / "serial.bin"
    data["evidence_paths"] = {name: str(ctx.path / name) for name in ("serial.bin", "serial.log", "serial.jsonl")}
    start = 0
    if operation == "serial send":
        data.update(tx_requested_hex=payload.hex(), tx_status="NOT_STARTED", listen_after_s=duration)
        ctx.event("SERIAL_SEND", byte_count=len(payload), listen_after_s=duration)
        start = session.mark()
        check_serial_budget(config, ctx, duration, payload)
        # 在同一会话里先定接收起点再发送，避免遗漏即时回复。窗口也可能包含主动上报。
        data["tx_status"] = "UNKNOWN"
        session.write(payload)
        data.update(tx_status="WRITE_COMPLETED", tx_bytes=len(payload))
        if not duration:
            return
    else:
        ctx.event("SERIAL_CAPTURE" if operation == "serial capture" else "SERIAL_READ", duration_s=duration)
        check_serial_budget(config, ctx, duration, payload)

    deadline = time.monotonic() + duration
    while True:
        ctx.check()
        session.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ctx.sleep(min(remaining, 0.05))
    end = session.mark()
    data.update(rx_start_offset=start, rx_end_offset=end, rx_bytes=end - start)
    if operation == "serial capture":
        return
    preview_size = min(end - start, PREVIEW_BYTES)
    try:
        with rx_path.open("rb") as stream:
            stream.seek(start)
            preview = stream.read(preview_size)
    except OSError as exc:
        raise ToolError("LOG_WRITE_FAILED", f"无法读取串口证据预览: {exc}") from exc
    if len(preview) != preview_size:
        raise ToolError("LOG_WRITE_FAILED", "串口证据短于已收到的字节范围")
    data.update(rx_preview_hex=preview.hex(),
                rx_preview_text=preview.decode(config["serial"].get("encoding", "utf-8"), "replace"),
                rx_preview_truncated=end - start > preview_size)
