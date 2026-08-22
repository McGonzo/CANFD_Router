from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List

from can_gateway.core.frame import Frame
from can_gateway.core.models import HardwareConfig


@dataclass(slots=True)
class DeviceInfo:
    device_id: str
    display_name: str
    driver: str
    channel: str
    capabilities: Dict[str, str] = field(default_factory=dict)


class BusAdapter(ABC):
    @abstractmethod
    def open(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def send(self, frame: Frame) -> None:
        raise NotImplementedError

    @abstractmethod
    def recv(self, timeout: float) -> Frame | None:
        raise NotImplementedError

    @property
    @abstractmethod
    def is_open(self) -> bool:
        raise NotImplementedError


class DriverPlugin(ABC):
    name: str

    def diagnostics(self) -> List[str]:
        return []

    @abstractmethod
    def scan_devices(self) -> List[DeviceInfo]:
        raise NotImplementedError

    @abstractmethod
    def create_adapter(self, config: HardwareConfig) -> BusAdapter:
        raise NotImplementedError
