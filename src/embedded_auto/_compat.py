"""仅在包入口登记已有 Python 路径，不让 core 基础实现反向导入领域。

旧配置、接口与状态名称有历史文档或调用证据；新代码使用各自规范路径。
只在访问已知旧名称时延迟导入。普通 core 导入不加载应用、领域或适配器。
"""
from importlib import import_module

from .core import config, interfaces, models


def _config_attribute(name):
    if name not in ("load_config", "validate_config", "validation_issues"):
        raise AttributeError(name)
    return getattr(import_module(".application.config", __package__), name)


def _interface_attribute(name):
    if name in ("BuildBackend", "Builder", "Programmer", "Flasher"):
        module = ".firmware.interfaces"
    elif name == "SerialPort":
        module = ".serial.interfaces"
    else:
        raise AttributeError(name)
    return getattr(import_module(module, __package__), name)


def _model_attribute(name):
    if name != "WorkflowState":
        raise AttributeError(name)
    return import_module(".application.models", __package__).WorkflowState


def install():
    config.__getattr__ = _config_attribute
    config.__all__ = ["load_yaml", "merge", "number", "integer", "text", "pattern",
                      "load_config", "validate_config", "validation_issues"]
    interfaces.__getattr__ = _interface_attribute
    interfaces.__all__ = ["BuildBackend", "Builder", "Programmer", "Flasher", "SerialPort"]
    models.__getattr__ = _model_attribute
    models.__all__ = ["json_value", "OperationResult", "WorkflowState"]
