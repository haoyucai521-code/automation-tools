"""BLE 边界数据；不泄露平台或第三方库对象。"""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    address: str
    name: str | None = None
    rssi: int | None = None
    service_uuids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Characteristic:
    ref: str
    service_uuid: str
    uuid: str
    handle: int
    properties: tuple[str, ...]
    max_write_with_response: int | None = 512
    max_write_without_response: int | None = None


@dataclass(frozen=True)
class Service:
    uuid: str
    handle: int
    characteristics: tuple[Characteristic, ...]


@dataclass(frozen=True)
class Checkpoint:
    epoch: str
    generation: str
    ref: str
    seq: int


def record(value):
    return asdict(value)
