"""Serial transport construction; importing this module needs no hardware dependency."""


def create_serial(**kwargs):
    """Construct a closed pySerial port (the caller supplies port=None).

    API reference: https://pyserial.readthedocs.io/en/latest/pyserial_api.html
    """
    import serial

    return serial.Serial(**kwargs)
