from __future__ import annotations

import ctypes as C
import os
import platform
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from can_gateway.core.frame import Frame
from can_gateway.core.models import HardwareConfig
from can_gateway.drivers.base import BusAdapter, DeviceInfo, DriverPlugin
from can_gateway.utils.app_paths import app_path, resource_path


_MAX_ONLINE_CHANNEL_COUNT = 64
_BUF_MIN_SIZE = 64
_BUF_MAX_SIZE = 256
_CAN_MSG_LENGTH = 64
_RX_SERVICE_UNION_SIZE = 16_400
_RX_SERVICE_UNION_OFFSET = 8
_RX_SERVICE_TYPE_BUS_ERROR = 0x00
_RX_SERVICE_TYPE_CAN_TRACE = 0x08
_H3_ERR_RECV_QUEUE_EMPTY = 0x1003
_H3_BUS_CAN = 0
_H3_BUS_CANFD = 3
_H3_BUS_CANCANFD_EX = 8
_CAN_EFF_FLAG = 0x80000000
_CAN_ID_MASK = 0x1FFFFFFF


class _H3ChannelConfig(C.Structure):
    _fields_ = [
        ("ucChannelIndex", C.c_ubyte),
        ("unChannelMask", C.c_ulonglong),
        ("szHwName", C.c_char * _BUF_MIN_SIZE),
        ("szHwType", C.c_ubyte),
        ("szHwAdditionalInfo", C.c_char * _BUF_MAX_SIZE),
        ("ucHwChannelNo", C.c_ubyte),
        ("ucHwTransMode", C.c_ubyte),
        ("ucBusCapabilities", C.c_ubyte),
    ]


class _H3DriverConfig(C.Structure):
    _fields_ = [
        ("nChannelCount", C.c_int),
        ("h3Channels", _H3ChannelConfig * _MAX_ONLINE_CHANNEL_COUNT),
    ]


class _CanParams(C.Structure):
    _fields_ = [
        ("ucListenFlag", C.c_ubyte),
        ("ucBaudrate", C.c_uint),
        ("ucSamplePoint", C.c_ubyte),
    ]


class _CanFdParams(C.Structure):
    _fields_ = [
        ("ucListenFlag", C.c_ubyte),
        ("abrBaudrate", C.c_uint),
        ("dbrBaudrate", C.c_uint),
        ("abrSamplePoint", C.c_ubyte),
        ("dbrSamplePoint", C.c_ubyte),
    ]


class _BusParamUnion(C.Union):
    # The SDK union also contains ETH_PARAMS (520 bytes) and extended CAN
    # timing structures. Keep the complete 524-byte union ABI while exposing
    # the CAN members used by this driver.
    _fields_ = [
        ("can", _CanParams),
        ("canFd", _CanFdParams),
        ("reserved", C.c_ubyte * 524),
    ]


class _ChannelBusParams(C.Structure):
    _fields_ = [("ucBusType", C.c_ubyte), ("busParam", _BusParamUnion)]


class _CanMsg(C.Structure):
    _fields_ = [
        ("unCanId", C.c_uint),
        ("ucTag", C.c_ubyte),
        ("ucEDL", C.c_ubyte),
        ("ucBRS", C.c_ubyte),
        ("ucDLC", C.c_ubyte),
        ("ucData", C.c_ubyte * _CAN_MSG_LENGTH),
        ("unTimestamp", C.c_uint),
    ]


class _GetCanTraceEx(C.Structure):
    _fields_ = [
        ("unCanId", C.c_uint),
        ("ucTag", C.c_ubyte),
        ("ucEDL", C.c_ubyte),
        ("ucBRS", C.c_ubyte),
        ("ucDLC", C.c_ubyte),
        ("ucData", C.c_ubyte * _CAN_MSG_LENGTH),
        ("unTimestamp", C.c_uint),
        ("ucESI", C.c_ubyte),
    ]


class _RxService(C.Structure):
    # The vendor RX_SERVICE union is 16,400 bytes in the SDK ABI. Keep its
    # full size so arrays use the correct stride, while decoding CAN records.
    _fields_ = [
        ("ucChannelIndex", C.c_ubyte),
        ("nServiceType", C.c_uint32),
        ("service", C.c_ubyte * _RX_SERVICE_UNION_SIZE),
    ]


class _BusError(C.Structure):
    _fields_ = [
        ("unTimestamp", C.c_uint),
        ("ucFrameType", C.c_ubyte),
        ("ucErrorCode", C.c_ubyte),
    ]


_SAMPLE_POINT_CODES = {
    50.0: 1,
    52.5: 2,
    55.0: 3,
    57.5: 4,
    60.0: 5,
    61.25: 6,
    62.5: 7,
    63.75: 8,
    65.0: 9,
    66.25: 10,
    67.5: 11,
    68.0: 12,
    68.75: 13,
    70.0: 14,
    71.25: 15,
    72.5: 16,
    73.75: 17,
    75.0: 0,
    76.25: 18,
    77.5: 19,
    78.75: 20,
    80.0: 21,
    81.25: 22,
    82.5: 23,
    85.0: 24,
    87.5: 25,
    90.0: 26,
    92.5: 27,
    95.0: 28,
}

_TESTBASE_FD_SAMPLE_POINTS = {
    "arbitration": {
        100_000: tuple(60 + 1.25 * index for index in range(18)),
        125_000: tuple(60 + 1.25 * index for index in range(18)),
        250_000: tuple(60 + 1.25 * index for index in range(18)),
        500_000: tuple(60 + 1.25 * index for index in range(18)),
        1_000_000: (
            52.5, 55, 57.5, 60, 62.5, 65, 67.5, 70, 72.5,
            75, 77.5, 80, 82.5, 85, 87.5, 90, 92.5, 95,
        ),
    },
    "data": {
        1_000_000: (60, 65, 70, 75, 80, 85),
        2_000_000: (60, 65, 70, 75, 80, 85),
        2_500_000: (62.5, 68.75, 75, 81.25),
        4_000_000: (60, 70, 80),
        5_000_000: (62.5, 75),
        8_000_000: (60,),
    },
}

_DLL_DIRECTORY_HANDLES: List[Any] = []


def _decode_text(value: bytes) -> str:
    return bytes(value).split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()


def _sample_point_code(sample_point: float, default: float = 80.0) -> int:
    target = float(sample_point)
    if not 50.0 <= target <= 95.0:
        target = float(default)
    value = min(_SAMPLE_POINT_CODES, key=lambda candidate: abs(candidate - target))
    return _SAMPLE_POINT_CODES[value]


def _fd_sample_point_code(sample_point: float, bitrate: int, phase: str) -> int:
    values = _TESTBASE_FD_SAMPLE_POINTS[phase].get(int(bitrate))
    if not values:
        return _sample_point_code(sample_point)
    selected = min(values, key=lambda value: abs(value - float(sample_point)))
    return _SAMPLE_POINT_CODES[selected]


def _bind_dll_functions(dll: Any) -> Any:
    signatures = {
        "H3GetDriverConfig": ([C.POINTER(_H3DriverConfig), C.c_char_p], C.c_uint32),
        "H3OpenPort": ([C.POINTER(C.c_size_t), C.c_uint64], C.c_uint32),
        "H3ClosePort": ([C.c_size_t], C.c_uint32),
        "H3ActiveChannel": (
            [C.c_size_t, C.c_ubyte, _ChannelBusParams, C.POINTER(C.c_uint32)],
            C.c_uint32,
        ),
        "H3DeactiveChannel": ([C.c_size_t], C.c_uint32),
        "H3StartStopBusTrace": (
            [C.c_size_t, C.c_ubyte, C.c_bool, C.POINTER(C.c_uint32)],
            C.c_uint32,
        ),
        "H3CANSetOneTimeTxMsgs": (
            [C.c_size_t, C.c_ubyte, C.c_ubyte, C.POINTER(_CanMsg), C.POINTER(C.c_uint32)],
            C.c_uint32,
        ),
        "H3GetRxService": (
            [C.c_size_t, C.POINTER(_RxService), C.POINTER(C.c_uint32)],
            C.c_uint32,
        ),
    }
    for name, (argtypes, restype) in signatures.items():
        function = getattr(dll, name)
        function.argtypes = argtypes
        function.restype = restype
    return dll


def _load_testbase_dll() -> Any:
    dll_name = "himetal3api.dll" if platform.system() == "Windows" else "libhimetal3api.so"
    candidates: List[Path] = []
    configured = os.environ.get("TESTBASEVCI_LIBPATH", "").strip()
    if configured:
        configured_path = Path(configured).expanduser()
        candidates.append(
            configured_path / dll_name if configured_path.is_dir() else configured_path
        )
    candidates.extend((resource_path(dll_name), app_path(dll_name)))
    searched: List[str] = []
    loader = C.WinDLL if platform.system() == "Windows" else C.CDLL
    for candidate in candidates:
        if not candidate.is_file():
            continue
        searched.append(str(candidate))
        try:
            if platform.system() == "Windows" and hasattr(os, "add_dll_directory"):
                _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(candidate.parent)))
            return _bind_dll_functions(loader(str(candidate)))
        except OSError as exc:
            searched.append(f"{candidate}: {exc}")

    try:
        return _bind_dll_functions(loader(dll_name))
    except OSError as exc:
        searched.append(str(exc))
    detail = "; ".join(searched) if searched else f"'{dll_name}' was not found on the DLL search path"
    raise OSError(
        f"TestBase API library {dll_name} could not be loaded ({detail}). "
        "Install the vendor TestBaseVCI driver/runtime or set TESTBASEVCI_LIBPATH."
    )


def _check_status(operation: str, status: int) -> None:
    if int(status) != 0:
        raise RuntimeError(f"TestBase {operation} failed with SDK status 0x{int(status):X}.")


def _enumerate_channels(dll: Any) -> List[Dict[str, Any]]:
    config = _H3DriverConfig()
    status = dll.H3GetDriverConfig(C.byref(config), None)
    _check_status("device enumeration", status)
    count = max(0, min(int(config.nChannelCount), _MAX_ONLINE_CHANNEL_COUNT))
    channels: List[Dict[str, Any]] = []
    for index in range(count):
        channel = config.h3Channels[index]
        bus_capability = int(channel.ucBusCapabilities)
        if bus_capability not in {_H3_BUS_CAN, _H3_BUS_CANFD, _H3_BUS_CANCANFD_EX}:
            continue
        channels.append(
            {
                "channel_index": int(channel.ucChannelIndex),
                "channel_mask": int(channel.unChannelMask),
                "device_name": _decode_text(channel.szHwName),
                "hw_type": int(channel.szHwType),
                "hw_additional_info": _decode_text(channel.szHwAdditionalInfo),
                "hw_channel_no": int(channel.ucHwChannelNo),
                "trans_mode": int(channel.ucHwTransMode),
                "bus_capabilities": bus_capability,
            }
        )
    return channels


def _dlc_to_length(dlc: int) -> int:
    if dlc <= 8:
        return dlc
    return {9: 12, 10: 16, 11: 20, 12: 24, 13: 32, 14: 48, 15: 64}.get(dlc, 64)


def _length_to_dlc(length: int, is_fd: bool) -> tuple[int, bytes]:
    maximum = 64 if is_fd else 8
    if length < 0 or length > maximum:
        raise ValueError(f"TestBase CAN payload length must be between 0 and {maximum} bytes.")
    if length <= 8:
        return length, b""
    for dlc, payload_length in ((9, 12), (10, 16), (11, 20), (12, 24), (13, 32), (14, 48), (15, 64)):
        if length <= payload_length:
            return dlc, bytes(payload_length - length)
    raise ValueError(f"Unsupported TestBase CAN payload length {length}.")


class TestBaseAdapter(BusAdapter):
    def __init__(self, config: HardwareConfig, dll: Any | None = None):
        self._config = config
        self._dll = dll
        self._port_handle = 0
        self._channel_index = 0
        self._trace_started = False
        self._api_lock = threading.RLock()

    @property
    def is_open(self) -> bool:
        return bool(self._port_handle)

    def _resolve_channel(self) -> Dict[str, Any]:
        serial = str(
            self._config.serial
            or self._config.extra.get("hwserial", "")
            or self._config.extra.get("device_name", "")
        ).strip()
        if not serial:
            raise RuntimeError("TestBase device identity is missing; rescan and import the device.")
        configured_index = self._config.extra.get("channel_index")
        configured_mask = self._config.extra.get("channel_mask")
        if configured_mask is not None:
            try:
                channel_mask = int(str(configured_mask), 0)
            except ValueError as exc:
                raise RuntimeError(f"Invalid TestBase channel mask '{configured_mask}'.") from exc
            for channel in _enumerate_channels(self._dll):
                if channel["channel_mask"] == channel_mask:
                    return channel
            raise RuntimeError(
                f"TestBase channel mask {channel_mask} is no longer available. Rescan devices."
            )
        if configured_index is not None:
            try:
                channel_index = int(str(configured_index), 0)
            except ValueError as exc:
                raise RuntimeError(f"Invalid TestBase channel index '{configured_index}'.") from exc
        else:
            try:
                local_index = int(str(self._config.channel), 0)
            except ValueError as exc:
                raise RuntimeError(f"Invalid TestBase channel '{self._config.channel}'.") from exc
            matching = [ch for ch in _enumerate_channels(self._dll) if ch["device_name"] == serial]
            if not 0 <= local_index < len(matching):
                raise RuntimeError(f"TestBase channel {local_index} is no longer available on {serial}.")
            return matching[local_index]

        for channel in _enumerate_channels(self._dll):
            if channel["device_name"] == serial and channel["channel_index"] == channel_index:
                return channel
        raise RuntimeError(
            f"TestBase channel {channel_index} on device {serial} is no longer available. Rescan devices."
        )

    def open(self) -> None:
        if self.is_open:
            return
        if self._dll is None:
            self._dll = _load_testbase_dll()
        channel_info = self._resolve_channel()
        channel_index = int(channel_info["channel_index"])
        port_handle = C.c_size_t()
        _check_status(
            "port open",
            self._dll.H3OpenPort(C.byref(port_handle), C.c_uint64(channel_info["channel_mask"])),
        )
        if not port_handle.value:
            raise RuntimeError("TestBase API returned an empty port handle.")
        self._port_handle = int(port_handle.value)
        self._channel_index = channel_index
        try:
            params = _ChannelBusParams()
            timestamp = C.c_uint32()
            if self._config.fd:
                params.ucBusType = _H3_BUS_CANFD
                params.busParam.canFd.ucListenFlag = 0
                params.busParam.canFd.abrBaudrate = int(self._config.bitrate)
                params.busParam.canFd.dbrBaudrate = int(self._config.data_bitrate)
                params.busParam.canFd.abrSamplePoint = _fd_sample_point_code(
                    float(self._config.extra.get("testbase_fd_sample_point_abr", 80.0)),
                    int(self._config.bitrate),
                    "arbitration",
                )
                params.busParam.canFd.dbrSamplePoint = _fd_sample_point_code(
                    float(self._config.extra.get("testbase_fd_sample_point_dbr", 80.0)),
                    int(self._config.data_bitrate),
                    "data",
                )
            else:
                params.ucBusType = _H3_BUS_CAN
                params.busParam.can.ucListenFlag = 0
                params.busParam.can.ucBaudrate = int(self._config.bitrate)
                params.busParam.can.ucSamplePoint = _SAMPLE_POINT_CODES[75.0]
            _check_status(
                "channel activation",
                self._dll.H3ActiveChannel(
                    C.c_size_t(self._port_handle),
                    C.c_ubyte(channel_index),
                    params,
                    C.byref(timestamp),
                ),
            )
            _check_status(
                "bus trace start",
                self._dll.H3StartStopBusTrace(
                    C.c_size_t(self._port_handle),
                    C.c_ubyte(channel_index),
                    C.c_bool(True),
                    C.byref(timestamp),
                ),
            )
            self._trace_started = True
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        with self._api_lock:
            port_handle = self._port_handle
            if not port_handle:
                return
            self._port_handle = 0
            try:
                if self._trace_started:
                    self._dll.H3StartStopBusTrace(
                        C.c_size_t(port_handle),
                        C.c_ubyte(self._channel_index),
                        C.c_bool(False),
                        None,
                    )
            finally:
                self._trace_started = False
                try:
                    self._dll.H3DeactiveChannel(C.c_size_t(port_handle))
                finally:
                    self._dll.H3ClosePort(C.c_size_t(port_handle))

    def send(self, frame: Frame) -> None:
        with self._api_lock:
            if not self.is_open:
                raise RuntimeError("Adapter not opened.")
            dlc, padding = _length_to_dlc(len(frame.data), frame.is_fd)
            payload = bytes(frame.data) + padding
            message = _CanMsg()
            message.unCanId = int(frame.arbitration_id) | (
                _CAN_EFF_FLAG if frame.is_extended_id else 0
            )
            message.ucTag = 0
            message.ucEDL = 1 if frame.is_fd else 0
            message.ucBRS = int(frame.is_fd and (frame.bitrate_switch if frame.bitrate_switch is not None else True))
            message.ucDLC = dlc
            if payload:
                message.ucData[: len(payload)] = payload
            timestamp = C.c_uint32()
            _check_status(
                "CAN transmit",
                self._dll.H3CANSetOneTimeTxMsgs(
                    C.c_size_t(self._port_handle),
                    C.c_ubyte(self._channel_index),
                    C.c_ubyte(1),
                    C.byref(message),
                    C.byref(timestamp),
                ),
            )

    def recv(self, timeout: float) -> Frame | None:
        if not self.is_open:
            return None
        deadline = time.monotonic() + max(0.0, float(timeout))
        while self.is_open:
            with self._api_lock:
                if not self.is_open:
                    return None
                service = _RxService()
                count = C.c_uint32(1)
                status = int(
                    self._dll.H3GetRxService(
                        C.c_size_t(self._port_handle),
                        C.byref(service),
                        C.byref(count),
                    )
                )
            if status == 0 and count.value:
                frame = self._frame_from_service(service)
                if frame is not None:
                    return frame
            elif status != _H3_ERR_RECV_QUEUE_EMPTY:
                _check_status("CAN receive", status)
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(0.001, max(0.0, deadline - time.monotonic())))
        return None

    def _frame_from_service(self, service: _RxService) -> Frame | None:
        service_type = int(service.nServiceType)
        if service_type == _RX_SERVICE_TYPE_CAN_TRACE:
            trace = _GetCanTraceEx.from_buffer(service, _RX_SERVICE_UNION_OFFSET)
            raw_id = int(trace.unCanId)
            dlc = int(trace.ucDLC)
            length = _dlc_to_length(dlc)
            return Frame(
                arbitration_id=raw_id & _CAN_ID_MASK,
                data=bytes(trace.ucData[:length]),
                is_fd=bool(trace.ucEDL),
                is_extended_id=bool(raw_id & _CAN_EFF_FLAG),
                timestamp=float(trace.unTimestamp) / 1_000_000.0,
                channel=str(self._config.channel),
                driver="testbase",
            )
        if service_type == _RX_SERVICE_TYPE_BUS_ERROR:
            error = _BusError.from_buffer(service, _RX_SERVICE_UNION_OFFSET)
            return Frame(
                arbitration_id=0,
                data=b"",
                timestamp=float(error.unTimestamp) / 1_000_000.0,
                channel=str(self._config.channel),
                is_error=True,
                error_source="ERROR_FRAME",
                error_code=int(error.ucErrorCode),
                error_text=f"TestBase bus error 0x{int(error.ucErrorCode):02X}",
                driver="testbase",
            )
        return None


class TestBaseDriver(DriverPlugin):
    name = "testbase"

    def __init__(self) -> None:
        self._diagnostics: List[str] = []
        self._dll: Any | None = None

    def diagnostics(self) -> List[str]:
        return list(self._diagnostics)

    def scan_devices(self) -> List[DeviceInfo]:
        self._diagnostics = []
        try:
            self._dll = _load_testbase_dll()
            channels = _enumerate_channels(self._dll)
        except Exception as exc:
            self._diagnostics.append(
                f"TestBase native scan failed: {exc}. Install the vendor TestBaseVCI driver/runtime."
            )
            return []

        channel_ordinals: Dict[str, int] = {}
        devices: List[DeviceInfo] = []
        for channel in channels:
            serial = str(channel["device_name"])
            if not serial:
                continue
            local_index = channel_ordinals.get(serial, 0)
            channel_ordinals[serial] = local_index + 1
            capability = int(channel["bus_capabilities"])
            supports_fd = capability in {_H3_BUS_CANFD, _H3_BUS_CANCANFD_EX}
            channel_index = int(channel["channel_index"])
            devices.append(
                DeviceInfo(
                    device_id=f"testbase-{serial}-{channel_index}",
                    display_name=f"TestBase {serial} CH{channel['hw_channel_no']}",
                    driver=self.name,
                    channel=str(local_index),
                    capabilities={
                        "interface": "testbase-native",
                        "vendor": "testbase",
                        "registry_key": self.name,
                        "serial": serial,
                        "device_name": serial,
                        "channel_index": str(channel_index),
                        "channel_mask": str(channel["channel_mask"]),
                        "hw_channel_no": str(channel["hw_channel_no"]),
                        "hw_type": str(channel["hw_type"]),
                        "hw_additional_info": str(channel["hw_additional_info"]),
                        "trans_mode": str(channel["trans_mode"]),
                        "bus_capabilities": str(capability),
                        "fd": "yes" if supports_fd else "no",
                        "supports_fd": "yes" if supports_fd else "no",
                    },
                )
            )

        if not devices and not self._diagnostics:
            self._diagnostics.append(
                "No TestBase CAN channels detected. Check the device connection and vendor driver."
            )
        return devices

    def create_adapter(self, config: HardwareConfig) -> BusAdapter:
        return TestBaseAdapter(config, dll=self._dll)
