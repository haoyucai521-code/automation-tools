"""具体后端在应用边界选择；领域实现仅接收注入依赖。

保留旧服务的默认构造签名，供 CLI 和兼容入口使用，不在领域层反向装配。
"""
from embedded_auto.adapters.build import GCCBackend, IARBackend
from embedded_auto.adapters.programmer import CubeProgrammer
from embedded_auto.adapters.serial import create_serial
from embedded_auto.firmware.build_service import BuildService as FirmwareBuildService
from embedded_auto.firmware.flash_service import FlashService as FirmwareFlashService
from embedded_auto.serial.serial_session import SerialSession as DomainSerialSession


def build_backend(name):
    backend = {"gcc": GCCBackend, "iar": IARBackend}.get(name)
    return backend() if backend is not None else None


class BuildService(FirmwareBuildService):
    def __init__(self, runner=None):
        super().__init__(runner, backend_factory=build_backend)


class FlashService(FirmwareFlashService):
    def __init__(self, programmer=None, runner=None):
        super().__init__(programmer if programmer is not None else CubeProgrammer(), runner)


class SerialSession(DomainSerialSession):
    def __init__(self, config: dict, factory=None):
        super().__init__(config, factory or create_serial)


def programmer():
    return CubeProgrammer()


def serial_ports():
    from embedded_auto.adapters.serial import list_ports
    return list_ports()


def check_ble_dependency():
    """只核对元数据，不导入蓝牙库、更不初始化扫描器。"""
    from importlib.metadata import PackageNotFoundError, version
    import sys
    from embedded_auto.core.errors import ToolError
    try:
        installed = version("bleak")
    except PackageNotFoundError:
        installed = None
    if installed != "3.0.2":
        raise ToolError("BLE_DEPENDENCY_MISSING", "真实 BLE 后端需要可选依赖 bleak==3.0.2",
                        {"required_version": "3.0.2", "installed_version": installed})
    if sys.platform != "win32":
        raise ToolError("BLE_CAPABILITY_UNSUPPORTED", "当前真实 BLE MVP 仅面向 Windows")


def ble_backend(config, profile):
    if config["ble"]["backend"] == "fake":
        from embedded_auto.adapters.ble.fake import FakeBackend
        return FakeBackend(profile, config["ble"]["operation_timeout_s"])
    check_ble_dependency()
    from embedded_auto.adapters.ble.bleak_backend import BleakBackend
    return BleakBackend(config["ble"]["operation_timeout_s"])
