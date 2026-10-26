"""兼容旧入口；默认后端在应用层装配。"""
from embedded_auto.firmware.flash_service import ResetAssistedConnectStrategy
from embedded_auto.application.composition import FlashService
