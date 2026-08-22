from __future__ import annotations

from typing import Any, Dict, List

from can_gateway.core.frame import Frame
from can_gateway.core.models import HardwareConfig
from can_gateway.drivers.base import BusAdapter, DeviceInfo, DriverPlugin

try:
    import can  # type: ignore
except Exception:  # pragma: no cover
    can = None

try:
    import nixnet  # type: ignore
    from nixnet import constants as nixnet_constants  # type: ignore
    from nixnet import system as nixnet_system  # type: ignore
except Exception as exc:  # pragma: no cover
    nixnet = None
    nixnet_constants = None
    nixnet_system = None
    _NIXNET_IMPORT_ERROR = str(exc)
else:
    _NIXNET_IMPORT_ERROR = ""


def _safe_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _nixnet_ready() -> bool:
    return nixnet is not None and nixnet_constants is not None and nixnet_system is not None


def _build_nixnet_missing_message() -> str:
    detail = _NIXNET_IMPORT_ERROR or "required nixnet submodules are unavailable"
    return (
        "NI scan unavailable: nixnet import failed "
        f"({detail}). Install NI-XNET runtime and the 'nixnet' Python package "
        "into the same Python environment that runs this app."
    )


def _friendly_scan_error(exc: Exception) -> List[str]:
    text = str(exc)
    details = [f"NI-XNET enumeration failed: {text}"]
    normalized = text.lower()
    if "internal file" in normalized and "could not be accessed" in normalized:
        details.append(
            "NI-XNET runtime files are inaccessible. Open NI MAX once as administrator, "
            "then verify NI-XNET was installed completely and its data folders are writable."
        )
    elif "module 'nixnet' has no attribute 'system'" in normalized:
        details.append(
            "The nixnet package is present but its 'system' API was not loaded. "
            "Reinstall or upgrade the NI nixnet Python package in the active Python environment."
        )
    return details


class NIAdapter(BusAdapter):
    def __init__(self, config: HardwareConfig):
        self._config = config
        self._bus = None

    @property
    def is_open(self) -> bool:
        return self._bus is not None

    def open(self) -> None:
        if can is None:
            raise RuntimeError("python-can is not installed.")
        if not _nixnet_ready():
            raise RuntimeError(
                "NI-XNET Python package is not available. Install NI-XNET runtime and the "
                "'nixnet' Python package in the same Python environment first."
            )
        kwargs: Dict[str, Any] = dict(self._config.extra)
        kwargs.pop("interface", None)
        kwargs.pop("registry_key", None)
        kwargs.pop("vendor", None)
        kwargs.pop("supports_fd", None)
        if self._config.bitrate:
            kwargs["bitrate"] = int(self._config.bitrate)
        if self._config.fd:
            kwargs["fd"] = True
            kwargs["fd_bitrate"] = int(self._config.data_bitrate)
        if "can_termination" in kwargs:
            kwargs["can_termination"] = _safe_bool(kwargs["can_termination"])
        self._bus = can.Bus(
            interface="nixnet",
            channel=self._config.channel,
            **kwargs,
        )

    def close(self) -> None:
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
                driver="ni",
            )
        return Frame(
            arbitration_id=int(msg.arbitration_id),
            data=bytes(msg.data),
            is_fd=bool(getattr(msg, "is_fd", False)),
            is_extended_id=bool(msg.is_extended_id),
            timestamp=float(getattr(msg, "timestamp", 0.0)),
            channel=str(getattr(msg, "channel", "")),
        )


class NIDriver(DriverPlugin):
    name = "ni"

    def __init__(self) -> None:
        self._diagnostics: List[str] = []

    def diagnostics(self) -> List[str]:
        return list(self._diagnostics)

    def scan_devices(self) -> List[DeviceInfo]:
        self._diagnostics = []
        if can is None:
            self._diagnostics.append("python-can is not installed.")
            return []
        if not _nixnet_ready():
            self._diagnostics.append(_build_nixnet_missing_message())
            return []

        devices: List[DeviceInfo] = []
        try:
            with nixnet_system.System() as system:
                for interface in system.intf_refs_can:
                    channel = str(interface)
                    supports_fd = (
                        interface.can_tcvr_cap is nixnet_constants.CanTcvrCap.HS
                    )
                    can_term_available = (
                        interface.can_term_cap is nixnet_constants.CanTermCap.YES
                    )
                    devices.append(
                        DeviceInfo(
                            device_id=f"ni-{interface.port_num}",
                            display_name=f"NI-XNET {channel}",
                            driver=self.name,
                            channel=channel,
                            capabilities={
                                "interface": "nixnet",
                                "fd": "yes" if supports_fd else "no",
                                "vendor": "ni",
                                "registry_key": self.name,
                                "supports_fd": "yes" if supports_fd else "no",
                                "can_termination_available": "yes"
                                if can_term_available
                                else "no",
                                "port_num": str(interface.port_num),
                            },
                        )
                    )
        except Exception as exc:
            self._diagnostics.extend(_friendly_scan_error(exc))
        if not devices and not self._diagnostics:
            self._diagnostics.append(
                "No NI-XNET CAN interfaces detected. Verify device is visible in NI MAX and NI-XNET runtime is installed."
            )
        return devices

    def create_adapter(self, config: HardwareConfig) -> BusAdapter:
        return NIAdapter(config)
