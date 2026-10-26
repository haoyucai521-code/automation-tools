"""现有串口帧、RTS 复位和电气确认规则。"""
import codecs

from embedded_auto.core.config import number, text
from embedded_auto.core.errors import ToolError


def validate_serial_config(config):
    s, r = config["serial"], config["reset"]
    text(s.get("port"), "serial.port")
    number(s.get("baudrate"), "serial.baudrate", 1)
    if s.get("bytesize", 8) not in (5, 6, 7, 8) or s.get("parity", "N") not in ("N", "E", "O", "M", "S") or s.get("stopbits", 1) not in (1, 1.5, 2):
        raise ToolError("CONFIG_INVALID", "串口帧参数无效")
    number(s.get("read_timeout_s", 0.1), "serial.read_timeout_s", 0.001, 1)
    number(s.get("write_timeout_s", 1), "serial.write_timeout_s", 0.001, 60)
    try:
        codecs.lookup(s.get("encoding", "utf-8"))
    except (LookupError, TypeError) as exc:
        raise ToolError("CONFIG_INVALID", "serial.encoding 不是有效文本编码") from exc
    if s.get("rtscts", False) is not False or s.get("dsrdtr", False) is not False:
        raise ToolError("CONFIG_INVALID", "RTS 复位模式禁止硬件流控")
    if type(r.get("asserted_rts_value")) is not bool:
        raise ToolError("CONFIG_INVALID", "reset.asserted_rts_value 必须是实测布尔值")
    for field in ("polarity_verified", "voltage_verified", "wiring_verified", "open_transient_verified"):
        if r.get(field) is not True:
            raise ToolError("CONFIG_INVALID", f"reset.{field} 未确认，禁止打开串口")
    number(r.get("pulse_ms", 100), "reset.pulse_ms", 0.001, 60000)
    number(r.get("post_pulse_delay_ms", 0), "reset.post_pulse_delay_ms", 0, 60000)
