"""Serial transport construction; importing this module needs no hardware dependency."""


def create_serial(**kwargs):
    """Construct a closed pySerial port (the caller supplies port=None).

    API reference: https://pyserial.readthedocs.io/en/latest/pyserial_api.html
    """
    import serial

    return serial.Serial(**kwargs)


def list_ports():
    """只枚举端口，不打开端口；结果结构保持原检查命令契约。"""
    from serial.tools import list_ports as ports

    return [{"device": p.device, "description": p.description, "hwid": p.hwid}
            for p in ports.comports()]
