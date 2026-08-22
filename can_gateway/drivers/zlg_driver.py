from __future__ import annotations

import ctypes as C
import ctypes.util
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from can_gateway.core.frame import Frame
from can_gateway.core.models import HardwareConfig
from can_gateway.drivers.base import BusAdapter, DeviceInfo, DriverPlugin
from can_gateway.utils.app_paths import app_path, resource_path


TYPE_CAN = 0
TYPE_CANFD = 1
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_ID_MASK = 0x1FFFFFFF
CANFD_BRS = 0x01

ZCAN_ERROR_CAN_OVERFLOW = 0x0001
ZCAN_ERROR_CAN_ERRALARM = 0x0002
ZCAN_ERROR_CAN_PASSIVE = 0x0004
ZCAN_ERROR_CAN_LOSE = 0x0008
ZCAN_ERROR_CAN_BUSERR = 0x0010
ZCAN_ERROR_CAN_BUSOFF = 0x0020
_VC120_RUNTIME_DLLS = ("MSVCR120.dll", "MSVCP120.dll")


@dataclass(frozen=True, slots=True)
class _ZlgProbeSpec:
    label: str
    device_type: int
    channels: int
    supports_fd: bool


_PROBE_SPECS: List[_ZlgProbeSpec] = [
    _ZlgProbeSpec("USBCAN1", 3, 1, False),
    _ZlgProbeSpec("USBCAN2", 4, 2, False),
    _ZlgProbeSpec("USBCAN-E-U", 20, 1, False),
    _ZlgProbeSpec("USBCAN-2E-U", 21, 2, False),
    _ZlgProbeSpec("USBCAN-4E-U", 31, 4, False),
    _ZlgProbeSpec("USBCAN-8E-U", 34, 8, False),
    _ZlgProbeSpec("USBCANFD-200U", 41, 2, True),
    _ZlgProbeSpec("USBCANFD-100U", 42, 1, True),
    _ZlgProbeSpec("USBCANFD-MINI", 43, 1, True),
    _ZlgProbeSpec("USBCANFD-800U", 59, 8, True),
    _ZlgProbeSpec("USBCANFD-400U", 76, 4, True),
]


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip(), 0)
    except Exception:
        return int(default)


def _safe_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _parse_channel_index(value: str) -> int:
    text = str(value or "").strip().upper()
    if not text:
        return 0
    if text.startswith("CH"):
        text = text[2:]
    if text.startswith("CAN"):
        text = text[3:]
    return max(0, int(text, 0))


def _is_64bit_process() -> bool:
    return C.sizeof(C.c_void_p) == 8


def _candidate_libpaths(extra: Dict[str, object] | None = None) -> List[str]:
    paths: List[str] = []

    def _add(path: str) -> None:
        normalized = str(path or "").strip()
        if normalized and normalized not in paths:
            paths.append(normalized)

    if extra:
        _add(str(extra.get("libpath", "")))
    for env_name in ("ZLGCAN_LIBPATH", "ZLGCAN_LIBRARY_PATH", "ZCAN_LIBRARY_PATH"):
        _add(os.environ.get(env_name, ""))
    preferred_dir = "zlgcan_x64" if _is_64bit_process() else "zlgcan_x86"
    alternate_dir = "zlgcan_x86" if _is_64bit_process() else "zlgcan_x64"
    _add(str(resource_path(preferred_dir)))
    _add(str(resource_path(alternate_dir)))
    _add(str(resource_path("library")))
    _add(str(app_path(preferred_dir)))
    _add(str(app_path(alternate_dir)))
    _add(str(app_path("library")))
    for vendor_root in (r"C:\ZLG", r"C:\Program Files\ZLG", r"C:\Program Files (x86)\ZLG"):
        _add(str(Path(vendor_root) / preferred_dir))
        _add(str(Path(vendor_root) / alternate_dir))
    return paths


def _dll_load_dirs(root: Path) -> List[Path]:
    paths: List[Path] = []

    def _add(path: Path) -> None:
        resolved = Path(path)
        if resolved not in paths:
            paths.append(resolved)

    _add(root)
    _add(root.parent)
    _add(root / "kerneldlls")
    for runtime_dir in _common_vc120_runtime_dirs():
        _add(runtime_dir)
    return paths


def _dll_search_dirs(root: Path) -> List[Path]:
    paths = _dll_load_dirs(root)

    def _add(path: Path) -> None:
        resolved = Path(path)
        if resolved not in paths:
            paths.append(resolved)

    for env_name in ("SystemRoot", "WINDIR"):
        windows_root = os.environ.get(env_name, "")
        if not windows_root:
            continue
        _add(Path(windows_root) / "System32")
        _add(Path(windows_root) / "SysWOW64")
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry:
            _add(Path(entry))
    return paths


def _common_vc120_runtime_dirs() -> List[Path]:
    paths: List[Path] = []

    def _add(path: Path) -> None:
        resolved = Path(path)
        if not resolved.exists():
            return
        if not all((resolved / dll_name).exists() for dll_name in _VC120_RUNTIME_DLLS):
            return
        if resolved not in paths:
            paths.append(resolved)

    for env_name, fallback in (
        ("ProgramFiles", r"C:\Program Files"),
        ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ):
        office_root = Path(os.environ.get(env_name, fallback)) / "Microsoft Office" / "root"
        _add(office_root / "Client")
        _add(office_root / "Office16")
    return paths


def _missing_vc120_runtimes(root: Path) -> List[str]:
    search_dirs = _dll_search_dirs(root)
    missing: List[str] = []
    for dll_name in _VC120_RUNTIME_DLLS:
        if ctypes.util.find_library(dll_name) or ctypes.util.find_library(dll_name[:-4]):
            continue
        if any((directory / dll_name).exists() for directory in search_dirs):
            continue
        missing.append(dll_name)
    return missing


def _describe_dll_load_error(root: Path, exc: OSError) -> str:
    message = str(exc)
    details = [message]
    normalized = message.lower()
    expected_dir = "zlgcan_x64" if _is_64bit_process() else "zlgcan_x86"
    actual_dir = root.name.lower()
    if "winerror 193" in normalized or "not a valid win32 application" in normalized:
        if actual_dir in {"zlgcan_x64", "zlgcan_x86"} and actual_dir != expected_dir:
            details.append(
                f"Architecture mismatch: the current app process expects '{expected_dir}', "
                f"but tried to load '{actual_dir}'."
            )
            return " ".join(details)
        details.append(
            "Architecture mismatch: the ZLG DLL bitness does not match the current Python/app process."
        )
        return " ".join(details)
    missing_runtimes = _missing_vc120_runtimes(root)
    if missing_runtimes:
        arch = "x64" if _is_64bit_process() else "x86"
        details.append(
            "ZLG runtime is missing Microsoft Visual C++ 2013 Redistributable "
            f"({arch}) components: {', '.join(missing_runtimes)}. "
            "Install the VC++ 2013 runtime that matches the app architecture, or place "
            "these DLLs beside zlgcan.dll."
        )
    elif not (root / "kerneldlls").exists():
        details.append(
            "ZLG runtime folder is incomplete: missing 'kerneldlls' beside zlgcan.dll."
        )
    return " ".join(details)


class _ZCAN_DEVICE_INFO(C.Structure):
    _fields_ = [
        ("hw_Version", C.c_ushort),
        ("fw_Version", C.c_ushort),
        ("dr_Version", C.c_ushort),
        ("in_Version", C.c_ushort),
        ("irq_Num", C.c_ushort),
        ("can_Num", C.c_ubyte),
        ("str_Serial_Num", C.c_ubyte * 20),
        ("str_hw_Type", C.c_ubyte * 40),
        ("reserved", C.c_ushort * 4),
    ]


class _ZCAN_CAN(C.Structure):
    _fields_ = [
        ("acc_code", C.c_uint),
        ("acc_mask", C.c_uint),
        ("reserved", C.c_uint),
        ("filter", C.c_ubyte),
        ("timing0", C.c_ubyte),
        ("timing1", C.c_ubyte),
        ("mode", C.c_ubyte),
    ]


class _ZCAN_CANFD(C.Structure):
    _fields_ = [
        ("acc_code", C.c_uint),
        ("acc_mask", C.c_uint),
        ("abit_timing", C.c_uint),
        ("dbit_timing", C.c_uint),
        ("brp", C.c_uint),
        ("filter", C.c_ubyte),
        ("mode", C.c_ubyte),
        ("pad", C.c_ushort),
        ("reserved", C.c_uint),
    ]


class _ZCAN_CHANNEL_INIT_UNION(C.Union):
    _fields_ = [("can", _ZCAN_CAN), ("canfd", _ZCAN_CANFD)]


class _ZCAN_CHANNEL_INIT_CONFIG(C.Structure):
    _anonymous_ = ("cfg",)
    _fields_ = [("can_type", C.c_uint), ("cfg", _ZCAN_CHANNEL_INIT_UNION)]


class _can_frame(C.Structure):
    _fields_ = [
        ("can_id", C.c_uint),
        ("can_dlc", C.c_ubyte),
        ("__pad", C.c_ubyte),
        ("__res0", C.c_ubyte),
        ("__res1", C.c_ubyte),
        ("data", C.c_ubyte * 8),
    ]


class _canfd_frame(C.Structure):
    _fields_ = [
        ("can_id", C.c_uint),
        ("len", C.c_ubyte),
        ("flags", C.c_ubyte),
        ("__res0", C.c_ubyte),
        ("__res1", C.c_ubyte),
        ("data", C.c_ubyte * 64),
    ]


class _ZCAN_Transmit_Data(C.Structure):
    _fields_ = [("frame", _can_frame), ("transmit_type", C.c_uint)]


class _ZCAN_Receive_Data(C.Structure):
    _fields_ = [("frame", _can_frame), ("timestamp", C.c_uint64)]


class _ZCAN_TransmitFD_Data(C.Structure):
    _fields_ = [("frame", _canfd_frame), ("transmit_type", C.c_uint)]


class _ZCAN_ReceiveFD_Data(C.Structure):
    _fields_ = [("frame", _canfd_frame), ("timestamp", C.c_uint64)]


class _ZCAN_CHANNEL_ERR_INFO(C.Structure):
    _fields_ = [
        ("error_code", C.c_uint),
        ("passive_ErrData", C.c_ubyte * 3),
        ("arLost_ErrData", C.c_ubyte),
    ]


class _ZlgDll:
    def __init__(self, libpath: str) -> None:
        self.libpath = libpath
        root = Path(libpath)
        self._dll_directories: List[object] = []
        if os.name == "nt":
            for candidate in _dll_load_dirs(root):
                if not candidate.exists():
                    continue
                try:
                    self._dll_directories.append(os.add_dll_directory(str(candidate)))
                except Exception:
                    pass
        dll_file = root / "zlgcan.dll"
        if not dll_file.exists():
            raise RuntimeError(f"zlgcan.dll not found in '{root}'.")
        try:
            self.dll = C.WinDLL(str(dll_file))
        except OSError as exc:
            raise RuntimeError(_describe_dll_load_error(root, exc)) from exc
        self._bind()

    def _bind(self) -> None:
        self.dll.ZCAN_OpenDevice.argtypes = [C.c_uint, C.c_uint, C.c_uint]
        self.dll.ZCAN_OpenDevice.restype = C.c_void_p
        self.dll.ZCAN_CloseDevice.argtypes = [C.c_void_p]
        self.dll.ZCAN_CloseDevice.restype = C.c_uint
        self.dll.ZCAN_GetDeviceInf.argtypes = [C.c_void_p, C.POINTER(_ZCAN_DEVICE_INFO)]
        self.dll.ZCAN_GetDeviceInf.restype = C.c_uint
        self.dll.ZCAN_SetValue.argtypes = [C.c_void_p, C.c_char_p, C.c_void_p]
        self.dll.ZCAN_SetValue.restype = C.c_uint
        self.dll.ZCAN_InitCAN.argtypes = [
            C.c_void_p,
            C.c_uint,
            C.POINTER(_ZCAN_CHANNEL_INIT_CONFIG),
        ]
        self.dll.ZCAN_InitCAN.restype = C.c_void_p
        self.dll.ZCAN_StartCAN.argtypes = [C.c_void_p]
        self.dll.ZCAN_StartCAN.restype = C.c_uint
        self.dll.ZCAN_ResetCAN.argtypes = [C.c_void_p]
        self.dll.ZCAN_ResetCAN.restype = C.c_uint
        self.dll.ZCAN_ClearBuffer.argtypes = [C.c_void_p]
        self.dll.ZCAN_ClearBuffer.restype = C.c_uint
        self.dll.ZCAN_GetReceiveNum.argtypes = [C.c_void_p, C.c_ubyte]
        self.dll.ZCAN_GetReceiveNum.restype = C.c_uint
        self.dll.ZCAN_Transmit.argtypes = [
            C.c_void_p,
            C.POINTER(_ZCAN_Transmit_Data),
            C.c_uint,
        ]
        self.dll.ZCAN_Transmit.restype = C.c_uint
        self.dll.ZCAN_Receive.argtypes = [
            C.c_void_p,
            C.POINTER(_ZCAN_Receive_Data),
            C.c_uint,
            C.c_int,
        ]
        self.dll.ZCAN_Receive.restype = C.c_uint
        self.dll.ZCAN_TransmitFD.argtypes = [
            C.c_void_p,
            C.POINTER(_ZCAN_TransmitFD_Data),
            C.c_uint,
        ]
        self.dll.ZCAN_TransmitFD.restype = C.c_uint
        self.dll.ZCAN_ReceiveFD.argtypes = [
            C.c_void_p,
            C.POINTER(_ZCAN_ReceiveFD_Data),
            C.c_uint,
            C.c_int,
        ]
        self.dll.ZCAN_ReceiveFD.restype = C.c_uint
        self.dll.ZCAN_ReadChannelErrInfo.argtypes = [
            C.c_void_p,
            C.POINTER(_ZCAN_CHANNEL_ERR_INFO),
        ]
        self.dll.ZCAN_ReadChannelErrInfo.restype = C.c_uint

    def open_device(self, device_type: int, device_index: int) -> int:
        return int(self.dll.ZCAN_OpenDevice(int(device_type), int(device_index), 0) or 0)

    def close_device(self, handle: int) -> int:
        return int(self.dll.ZCAN_CloseDevice(C.c_void_p(handle)))

    def get_device_info(self, handle: int) -> _ZCAN_DEVICE_INFO:
        info = _ZCAN_DEVICE_INFO()
        ret = int(self.dll.ZCAN_GetDeviceInf(C.c_void_p(handle), C.byref(info)))
        if ret != 1:
            raise RuntimeError("ZCAN_GetDeviceInf failed.")
        return info

    def set_value(self, handle: int, path: str, value: object) -> int:
        buffer = C.create_string_buffer(str(value).encode("ascii"))
        return int(
            self.dll.ZCAN_SetValue(
                C.c_void_p(handle),
                path.encode("ascii"),
                C.cast(buffer, C.c_void_p),
            )
        )

    def init_channel(self, handle: int, channel_index: int, config: HardwareConfig) -> int:
        channel_config = _ZCAN_CHANNEL_INIT_CONFIG()
        is_canfd_device = _is_canfd_series_device(config)
        channel_config.can_type = 1 if is_canfd_device else 0
        if is_canfd_device:
            channel_config.canfd.mode = 0
            channel_config.canfd.filter = 0
            channel_config.canfd.acc_code = 0
            channel_config.canfd.acc_mask = 0xFFFFFFFF
            channel_config.canfd.abit_timing = 0
            channel_config.canfd.dbit_timing = 0
            channel_config.canfd.brp = 0
        else:
            channel_config.can.mode = 0
            channel_config.can.filter = 0
            channel_config.can.acc_code = 0
            channel_config.can.acc_mask = 0xFFFFFFFF
            channel_config.can.timing0 = 0
            channel_config.can.timing1 = 0
        channel_handle = self.dll.ZCAN_InitCAN(
            C.c_void_p(handle),
            int(channel_index),
            C.byref(channel_config),
        )
        return int(channel_handle or 0)


_DLL_CACHE: Dict[str, _ZlgDll] = {}
_DLL_CACHE_LOCK = threading.Lock()


def _load_dll(libpath: str) -> _ZlgDll:
    with _DLL_CACHE_LOCK:
        dll = _DLL_CACHE.get(libpath)
        if dll is None:
            dll = _ZlgDll(libpath)
            _DLL_CACHE[libpath] = dll
        return dll


def _bytes_to_text(raw: object) -> str:
    data = bytes(raw)
    return data.split(b"\0", 1)[0].decode("ascii", errors="ignore").strip()


def _describe_error_code(error_code: int) -> str:
    flags: List[str] = []
    if error_code & ZCAN_ERROR_CAN_OVERFLOW:
        flags.append("RX overflow")
    if error_code & ZCAN_ERROR_CAN_ERRALARM:
        flags.append("error warning")
    if error_code & ZCAN_ERROR_CAN_PASSIVE:
        flags.append("error passive")
    if error_code & ZCAN_ERROR_CAN_LOSE:
        flags.append("arbitration lost")
    if error_code & ZCAN_ERROR_CAN_BUSERR:
        flags.append("bus error")
    if error_code & ZCAN_ERROR_CAN_BUSOFF:
        flags.append("bus off")
    if not flags:
        return "unknown"
    return ", ".join(flags)


def _normalize_hw_type(value: str) -> str:
    return value.upper().replace("_", "").replace("-", "").replace(" ", "")


_HW_TYPE_TO_DEVICE_TYPE = {
    _normalize_hw_type(spec.label): spec.device_type for spec in _PROBE_SPECS
}
_CANFD_DEVICE_TYPES = {spec.device_type for spec in _PROBE_SPECS if spec.supports_fd}


def _is_canfd_series_device(config: HardwareConfig) -> bool:
    device_type = _safe_int(config.extra.get("device_type"), 0)
    if device_type in _CANFD_DEVICE_TYPES:
        return True
    hw_type = _normalize_hw_type(str(config.extra.get("hw_type", "")))
    if "FD" in hw_type:
        return True
    return _safe_bool(config.extra.get("fd", False), False)


class _ZlgSharedDevice:
    def __init__(self, dll: _ZlgDll, device_type: int, device_index: int) -> None:
        self.dll = dll
        self.device_type = int(device_type)
        self.device_index = int(device_index)
        self.lock = threading.RLock()
        self.refcount = 0
        self.device_handle = 0
        self.channels: Dict[int, int] = {}

    def acquire_channel(self, channel_index: int, hw: HardwareConfig) -> int:
        with self.lock:
            opened_device_now = False
            channel_handle = 0
            if self.device_handle == 0:
                self.device_handle = self.dll.open_device(self.device_type, self.device_index)
                if self.device_handle == 0:
                    raise RuntimeError(
                        f"ZLG open failed for device_type={self.device_type}, device_index={self.device_index}."
                    )
                opened_device_now = True
            channel_key = int(channel_index)
            channel_handle = self.channels.get(channel_key, 0)
            if channel_handle:
                self.refcount += 1
                return channel_handle
            try:
                self._configure_channel_before_init(channel_index, hw)
                channel_handle = self.dll.init_channel(self.device_handle, channel_index, hw)
                if channel_handle == 0:
                    mode = "CANFD" if hw.fd else "CAN"
                    device_kind = "CANFD-series" if _is_canfd_series_device(hw) else "classic-CAN"
                    raise RuntimeError(
                        f"ZLG InitCAN failed for channel {channel_index}. mode={mode}, device_kind={device_kind}, "
                        f"bitrate={int(hw.bitrate)}, data_bitrate={int(hw.data_bitrate)}."
                    )
                if int(self.dll.dll.ZCAN_StartCAN(C.c_void_p(channel_handle))) != 1:
                    raise RuntimeError(f"ZLG StartCAN failed for channel {channel_index}.")
                self.dll.dll.ZCAN_ClearBuffer(C.c_void_p(channel_handle))
                self._configure_channel_after_start(channel_index, hw)
                self.channels[channel_key] = int(channel_handle)
                self.refcount += 1
                return int(channel_handle)
            except Exception:
                if channel_handle:
                    try:
                        self.dll.dll.ZCAN_ResetCAN(C.c_void_p(channel_handle))
                    except Exception:
                        pass
                if opened_device_now and self.device_handle and not self.channels:
                    try:
                        self.dll.close_device(self.device_handle)
                    except Exception:
                        pass
                    self.device_handle = 0
                raise

    def _set_channel_value(self, channel_index: int, name: str, value: object) -> None:
        path = f"{int(channel_index)}/{name}"
        ret = self.dll.set_value(self.device_handle, path, value)
        if ret != 1:
            raise RuntimeError(
                f"ZLG SetValue failed for '{path}' = {value!r}, ret={ret}."
            )

    def _configure_channel_before_init(self, channel_index: int, hw: HardwareConfig) -> None:
        if _is_canfd_series_device(hw):
            self._set_channel_value(channel_index, "protocol", 1 if hw.fd else 0)
            self._set_channel_value(channel_index, "canfd_abit_baud_rate", int(hw.bitrate))
            if hw.fd:
                self._set_channel_value(channel_index, "canfd_standard", 0)
                self._set_channel_value(channel_index, "canfd_exp", 1)
                self._set_channel_value(channel_index, "canfd_dbit_baud_rate", int(hw.data_bitrate))
        else:
            self._set_channel_value(channel_index, "protocol", 0)
            self._set_channel_value(channel_index, "baud_rate", int(hw.bitrate))
        self._set_channel_value(channel_index, "work_mode", 0)

    def _configure_channel_after_start(self, channel_index: int, hw: HardwareConfig) -> None:
        self._set_channel_value(channel_index, "set_tx_retry_policy", 1)
        self._set_channel_value(
            channel_index,
            "initenal_resistance",
            1 if _safe_bool(hw.extra.get("resistance", True), True) else 0,
        )
        tx_echo = 1 if _safe_bool(hw.extra.get("tx_echo", False), False) else 0
        self._set_channel_value(channel_index, "set_device_tx_echo", tx_echo)

    def release(self) -> None:
        with self.lock:
            self.refcount = max(0, self.refcount - 1)
            if self.refcount == 0 and self.device_handle:
                self.dll.close_device(self.device_handle)
                self.device_handle = 0
                self.channels.clear()


class _ZlgSharedRegistry:
    _lock = threading.Lock()
    _devices: Dict[Tuple[str, int, int], _ZlgSharedDevice] = {}

    @classmethod
    def acquire(
        cls,
        *,
        dll: _ZlgDll,
        device_type: int,
        device_index: int,
        channel_index: int,
        hw: HardwareConfig,
    ) -> tuple[_ZlgSharedDevice, int]:
        key = (dll.libpath, int(device_type), int(device_index))
        with cls._lock:
            shared = cls._devices.get(key)
            if shared is None:
                shared = _ZlgSharedDevice(dll, int(device_type), int(device_index))
                cls._devices[key] = shared
        try:
            channel_handle = shared.acquire_channel(int(channel_index), hw)
            return shared, channel_handle
        except Exception:
            with cls._lock:
                if shared.refcount == 0 and shared.device_handle == 0 and not shared.channels:
                    stale = [item for item, value in cls._devices.items() if value is shared]
                    for item in stale:
                        cls._devices.pop(item, None)
            raise

    @classmethod
    def release(cls, shared: _ZlgSharedDevice) -> None:
        shared.release()
        with cls._lock:
            if shared.refcount == 0:
                stale = [key for key, value in cls._devices.items() if value is shared]
                for key in stale:
                    cls._devices.pop(key, None)


class ZLGAdapter(BusAdapter):
    def __init__(self, config: HardwareConfig):
        self._config = config
        self._shared: _ZlgSharedDevice | None = None
        self._channel_handle = 0

    @property
    def is_open(self) -> bool:
        return self._shared is not None and self._channel_handle != 0

    def open(self) -> None:
        if self.is_open:
            return
        device_type = _safe_int(self._config.extra.get("device_type"), 0)
        device_index = _safe_int(self._config.extra.get("device_index"), 0)
        channel_index = _safe_int(
            self._config.extra.get("channel_index"),
            _parse_channel_index(self._config.channel),
        )
        if device_type <= 0:
            raise RuntimeError(
                "ZLG port is missing device_type. Please import it from Scan Device."
            )
        libpaths = _candidate_libpaths(self._config.extra)
        runtime_errors: List[str] = []
        load_errors: List[str] = []
        for libpath in libpaths:
            try:
                dll = _load_dll(libpath)
            except Exception as exc:
                load_errors.append(f"runtime '{libpath}': {exc}")
                continue
            try:
                shared, channel_handle = _ZlgSharedRegistry.acquire(
                    dll=dll,
                    device_type=device_type,
                    device_index=device_index,
                    channel_index=channel_index,
                    hw=self._config,
                )
                self._shared = shared
                self._channel_handle = channel_handle
                self._config.extra["libpath"] = libpath
                self._config.extra["channel_index"] = str(channel_index)
                return
            except Exception as exc:
                runtime_errors.append(f"runtime '{libpath}': {exc}")
        if runtime_errors:
            raise RuntimeError(runtime_errors[0])
        if load_errors:
            raise RuntimeError(load_errors[0])
        raise RuntimeError("Failed to open ZLG device.")

    def close(self) -> None:
        shared = self._shared
        self._shared = None
        self._channel_handle = 0
        if shared is not None:
            _ZlgSharedRegistry.release(shared)

    def send(self, frame: Frame) -> None:
        if self._shared is None:
            raise RuntimeError("Adapter not opened.")
        channel = C.c_void_p(self._channel_handle)
        arbitration_id = int(frame.arbitration_id) & CAN_ID_MASK
        if frame.is_fd:
            tx = _ZCAN_TransmitFD_Data()
            tx.transmit_type = 0
            tx.frame.can_id = arbitration_id | (
                CAN_EFF_FLAG if frame.is_extended_id else 0
            )
            tx.frame.len = max(0, min(len(frame.data), 64))
            tx.frame.flags = CANFD_BRS if self._config.fd else 0
            for idx, value in enumerate(frame.data[:64]):
                tx.frame.data[idx] = int(value) & 0xFF
            sent = int(self._shared.dll.dll.ZCAN_TransmitFD(channel, C.byref(tx), 1))
        else:
            tx = _ZCAN_Transmit_Data()
            tx.transmit_type = 0
            tx.frame.can_id = arbitration_id | (
                CAN_EFF_FLAG if frame.is_extended_id else 0
            )
            tx.frame.can_dlc = max(0, min(len(frame.data), 8))
            for idx, value in enumerate(frame.data[:8]):
                tx.frame.data[idx] = int(value) & 0xFF
            sent = int(self._shared.dll.dll.ZCAN_Transmit(channel, C.byref(tx), 1))
        if sent <= 0:
            err = _ZCAN_CHANNEL_ERR_INFO()
            self._shared.dll.dll.ZCAN_ReadChannelErrInfo(channel, C.byref(err))
            error_code = int(err.error_code)
            description = _describe_error_code(error_code)
            raise RuntimeError(
                f"ZLG transmit failed, error_code=0x{error_code:X} ({description}). "
                "Usually this means the CAN bus has no ACK, bitrate/FD settings do not match, or termination/wiring is wrong."
            )

    def recv(self, timeout: float) -> Frame | None:
        if self._shared is None:
            return None
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            frame = self._recv_once()
            if frame is not None:
                return frame
            if timeout <= 0 or time.monotonic() >= deadline:
                return None
            time.sleep(0.005)

    def _recv_once(self) -> Frame | None:
        channel = C.c_void_p(self._channel_handle)
        dll = self._shared.dll.dll
        can_count = int(dll.ZCAN_GetReceiveNum(channel, TYPE_CAN))
        if can_count > 0:
            item = _ZCAN_Receive_Data()
            got = int(dll.ZCAN_Receive(channel, C.byref(item), 1, 0))
            if got > 0:
                raw_can_id = int(item.frame.can_id)
                if raw_can_id & CAN_ERR_FLAG:
                    error_code = raw_can_id & CAN_ID_MASK
                    return Frame(
                        arbitration_id=0,
                        data=bytes(item.frame.data[: int(item.frame.can_dlc)]),
                        is_fd=False,
                        timestamp=float(item.timestamp) / 1_000_000.0,
                        channel=self._config.channel,
                        is_error=True,
                        error_source="ERROR_FRAME",
                        error_code=error_code,
                        error_text=_describe_error_code(error_code),
                        driver="zlg",
                    )
                arbitration_id = int(item.frame.can_id & CAN_ID_MASK)
                is_extended = bool(item.frame.can_id & CAN_EFF_FLAG)
                return Frame(
                    arbitration_id=arbitration_id,
                    data=bytes(item.frame.data[: int(item.frame.can_dlc)]),
                    is_fd=False,
                    is_extended_id=is_extended,
                    timestamp=float(item.timestamp) / 1_000_000.0,
                    channel=self._config.channel,
                )

        fd_count = int(dll.ZCAN_GetReceiveNum(channel, TYPE_CANFD))
        if fd_count > 0:
            item = _ZCAN_ReceiveFD_Data()
            got = int(dll.ZCAN_ReceiveFD(channel, C.byref(item), 1, 0))
            if got > 0:
                raw_can_id = int(item.frame.can_id)
                if raw_can_id & CAN_ERR_FLAG:
                    error_code = raw_can_id & CAN_ID_MASK
                    return Frame(
                        arbitration_id=0,
                        data=bytes(item.frame.data[: int(item.frame.len)]),
                        is_fd=True,
                        timestamp=float(item.timestamp) / 1_000_000.0,
                        channel=self._config.channel,
                        is_error=True,
                        error_source="ERROR_FRAME",
                        error_code=error_code,
                        error_text=_describe_error_code(error_code),
                        driver="zlg",
                    )
                arbitration_id = int(item.frame.can_id & CAN_ID_MASK)
                is_extended = bool(item.frame.can_id & CAN_EFF_FLAG)
                return Frame(
                    arbitration_id=arbitration_id,
                    data=bytes(item.frame.data[: int(item.frame.len)]),
                    is_fd=True,
                    is_extended_id=is_extended,
                    timestamp=float(item.timestamp) / 1_000_000.0,
                    channel=self._config.channel,
                )
        return None


class ZLGDriver(DriverPlugin):
    name = "zlg"

    def __init__(self) -> None:
        self._diagnostics: List[str] = []

    def diagnostics(self) -> List[str]:
        return list(self._diagnostics)

    def scan_devices(self) -> List[DeviceInfo]:
        self._diagnostics = []
        devices: List[DeviceInfo] = []
        for libpath in _candidate_libpaths():
            try:
                dll = _load_dll(libpath)
            except Exception as exc:
                self._diagnostics.append(f"runtime '{libpath}': {exc}")
                continue

            found: Dict[Tuple[str, int], DeviceInfo] = {}
            for spec in _PROBE_SPECS:
                for device_index in range(8):
                    handle = dll.open_device(spec.device_type, device_index)
                    if handle == 0:
                        continue
                    try:
                        info = dll.get_device_info(handle)
                    except Exception:
                        dll.close_device(handle)
                        continue
                    hw_type = _bytes_to_text(info.str_hw_Type)
                    serial = _bytes_to_text(info.str_Serial_Num)
                    dll.close_device(handle)

                    normalized_hw = _normalize_hw_type(hw_type)
                    preferred_type = _HW_TYPE_TO_DEVICE_TYPE.get(normalized_hw, spec.device_type)
                    if preferred_type != spec.device_type:
                        continue

                    key = (serial or hw_type or f"dev{device_index}", device_index)
                    channel_count = max(int(info.can_Num), spec.channels)
                    found[key] = DeviceInfo(
                        device_id=f"zlg-{preferred_type}-{device_index}",
                        display_name=f"ZLG {hw_type or spec.label} #{device_index}",
                        driver=self.name,
                        channel="",
                        capabilities={
                            "interface": "zlgcan",
                            "vendor": "zlg",
                            "registry_key": self.name,
                            "device_type": str(preferred_type),
                            "device_index": str(device_index),
                            "channel_count": str(channel_count),
                            "fd": "yes" if spec.supports_fd or "FD" in normalized_hw else "no",
                            "serial": serial,
                            "hw_type": hw_type,
                            "libpath": libpath,
                        },
                    )

            if found:
                for item in found.values():
                    channel_count = _safe_int(item.capabilities.get("channel_count"), 1)
                    for channel_index in range(max(1, channel_count)):
                        devices.append(
                            DeviceInfo(
                                device_id=f"{item.device_id}-{channel_index}",
                                display_name=f"{item.display_name} CH{channel_index}",
                                driver=item.driver,
                                channel=str(channel_index),
                                capabilities={
                                    **item.capabilities,
                                    "channel_index": str(channel_index),
                                },
                            )
                        )
                return devices

        if any(
            shared.device_handle and shared.refcount > 0
            for shared in _ZlgSharedRegistry._devices.values()
        ):
            self._diagnostics.insert(
                0,
                "A ZLG device is already open in this app (for example in Direct CAN Tool or the router). "
                "Close that session before scanning again."
            )
        if not devices and not self._diagnostics:
            self._diagnostics.append(
                "No ZLG devices detected via official zlgcan.dll. Verify the device is connected and the vendor driver is installed."
            )
        return devices

    def create_adapter(self, config: HardwareConfig) -> BusAdapter:
        return ZLGAdapter(config)
