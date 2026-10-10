from dataclasses import dataclass, field
from time import time


@dataclass(slots=True)
class Frame:
    arbitration_id: int
    data: bytes
    is_fd: bool = False
    is_extended_id: bool = False
    bitrate_switch: bool | None = None
    timestamp: float = field(default_factory=time)
    channel: str = ""
    is_error: bool = False
    error_source: str = ""
    error_code: int | None = None
    error_text: str = ""
    bus_status: str = ""
    tx_error_count: int | None = None
    rx_error_count: int | None = None
    driver: str = ""

    @property
    def dlc(self) -> int:
        return len(self.data)
