"""兼容旧入口；默认后端在应用层装配。"""
from embedded_auto.firmware.build_service import validate_artifact
from embedded_auto.application.composition import BuildService
