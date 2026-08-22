from __future__ import annotations

import contextlib
import io
import threading
from typing import Any, Dict, List

from can_gateway.core.frame import Frame
from can_gateway.core.models import HardwareConfig
from can_gateway.drivers.base import BusAdapter, DeviceInfo, DriverPlugin

try:
    import can  # type: ignore
except Exception:  # pragma: no cover - optional dependency at runtime
    can = None


class PythonCanAdapter(BusAdapter):
    def __init__(self, interface: str, config: HardwareConfig):
        self._interface = interface
        self._config = config
        self._bus = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return self._bus is not None

    def open(self) -> None:
        if can is None:
            raise RuntimeError("python-can is not installed.")
        kwargs: Dict[str, Any] = dict(self._config.extra)
        if self._config.bitrate:
            kwargs["bitrate"] = self._config.bitrate
        if self._config.fd:
            kwargs["fd"] = True
            kwargs["data_bitrate"] = self._config.data_bitrate
        self._bus = can.Bus(
            interface=self._interface,
            channel=self._config.channel,
            **kwargs,
        )

    def close(self) -> None:
        with self._lock:
            bus = self._bus
            self._bus = None
        if bus is not None:
            bus.shutdown()

    def send(self, frame: Frame) -> None:
        if self._bus is None:
            raise RuntimeError("Adapter not opened.")
        msg = can.Message(
            arbitration_id=frame.arbitration_id,
            data=frame.data,
            is_fd=frame.is_fd,
            is_extended_id=frame.is_extended_id,
        )
        self._bus.send(msg)

    def recv(self, timeout: float) -> Frame | None:
        if self._bus is None:
            return None
        msg = self._bus.recv(timeout=timeout)
        if msg is None:
            return None
        if bool(getattr(msg, "is_error_frame", False)):
            error_state = getattr(msg, "error_state_indicator", None)
            status = ""
            if error_state is not None:
                status = "error-state-indicator" if bool(error_state) else ""
            return Frame(
                arbitration_id=int(getattr(msg, "arbitration_id", 0) or 0),
                data=bytes(getattr(msg, "data", b"") or b""),
                is_fd=bool(getattr(msg, "is_fd", False)),
                is_extended_id=bool(getattr(msg, "is_extended_id", False)),
                timestamp=float(getattr(msg, "timestamp", 0.0)),
                channel=str(getattr(msg, "channel", "")),
                is_error=True,
                error_source="ERROR_FRAME",
                error_text="CAN error frame",
                bus_status=status,
                driver=f"python-can:{self._interface}",
            )
        return Frame(
            arbitration_id=int(msg.arbitration_id),
            data=bytes(msg.data),
            is_fd=bool(getattr(msg, "is_fd", False)),
            is_extended_id=bool(msg.is_extended_id),
            timestamp=float(getattr(msg, "timestamp", 0.0)),
            channel=str(getattr(msg, "channel", "")),
        )


class PythonCanDriver(DriverPlugin):
    def __init__(self, interface: str = "pcan"):
        self.interface = interface
        self.name = f"python-can:{interface}"

    def scan_devices(self) -> List[DeviceInfo]:
        if can is None:
            return []
        devices: List[DeviceInfo] = []
        try:
            # Silence backend probe noise from unrelated interfaces (e.g. Kvaser/Vector)
            # so the UI only surfaces actionable diagnostics for the selected driver.
            with contextlib.redirect_stderr(io.StringIO()):
                candidates = can.detect_available_configs(interfaces=[self.interface])
        except Exception:
            return []
        for idx, item in enumerate(candidates):
            channel = str(item.get("channel", f"{self.interface}:{idx}"))
            devices.append(
                DeviceInfo(
                    device_id=f"{self.interface}-{idx}",
                    display_name=f"{self.interface.upper()} {channel}",
                    driver=self.name,
                    channel=channel,
                    capabilities={"interface": self.interface},
                )
            )
        return devices

    def create_adapter(self, config: HardwareConfig) -> BusAdapter:
        interface = str(config.extra.get("interface", self.interface))
        return PythonCanAdapter(interface=interface, config=config)
