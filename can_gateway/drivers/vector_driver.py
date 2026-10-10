"""Vector XL Driver Library (vxlapi) native driver for CANFD_Router.

Provides direct ctypes bindings to vxlapi.dll / vxlapi64.dll, enabling
device scanning, CAN/CANFD channel configuration, and frame-level
transmit/receive — matching the architecture of zlg_driver.py.
"""
from __future__ import annotations

import ctypes as C
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Tuple

from can_gateway.core.frame import Frame
from can_gateway.core.models import HardwareConfig
from can_gateway.drivers.base import BusAdapter, DeviceInfo, DriverPlugin
from can_gateway.utils.app_paths import app_path, resource_path

# ---------------------------------------------------------------------------
# Constants from vxlapi.h
# ---------------------------------------------------------------------------

XL_BUS_TYPE_CAN = 0x00000001
XL_INTERFACE_VERSION_V3 = 3
XL_INTERFACE_VERSION_V4 = 4

# Channel capability flags
XL_CHANNEL_FLAG_CANFD_BOSCH_SUPPORT = 0x20000000
XL_CHANNEL_FLAG_CANFD_ISO_SUPPORT = 0x80000000

# CAN bus capabilities (channelBusCapabilities)
XL_BUS_COMPATIBLE_CAN = XL_BUS_TYPE_CAN
XL_BUS_ACTIVE_CAP_CAN = XL_BUS_COMPATIBLE_CAN << 16
XL_BUS_CAN_CAP_MASK = XL_BUS_COMPATIBLE_CAN | XL_BUS_ACTIVE_CAP_CAN

# XLbusParams::data::can/canFD::canOpMode
XL_BUS_PARAMS_CANOPMODE_CAN20 = 0x01
XL_BUS_PARAMS_CANOPMODE_CANFD = 0x02

# hwType values
XL_HWTYPE_NONE = 0
XL_HWTYPE_VIRTUAL = 1
XL_HWTYPE_VN2600 = 29
XL_HWTYPE_VN3600 = 39
XL_HWTYPE_VN7600 = 41
XL_HWTYPE_VN8900 = 45
XL_HWTYPE_VN8950 = 47
XL_HWTYPE_VN2640 = 53
XL_HWTYPE_VN1610 = 55
XL_HWTYPE_VN1614 = 56
XL_HWTYPE_VN1630 = 57
XL_HWTYPE_VN1615 = 58
XL_HWTYPE_VN1640 = 59
XL_HWTYPE_VN8970 = 61
XL_HWTYPE_VN1611 = 63
XL_HWTYPE_VN5240 = 64
XL_HWTYPE_VN5610 = 65
XL_HWTYPE_VN5620 = 66
XL_HWTYPE_VN7570 = 67
XL_HWTYPE_VN5650 = 68
XL_HWTYPE_VN5611 = 70
XL_HWTYPE_VN5612 = 72
XL_HWTYPE_VN5614 = 78
XL_HWTYPE_VN1630_LOG = 79
XL_HWTYPE_VN7610 = 81
XL_HWTYPE_VN7572 = 83
XL_HWTYPE_VN8972 = 85
XL_HWTYPE_VN1641 = 86
XL_HWTYPE_VN0601 = 87
XL_HWTYPE_VN5640 = 89
XL_HWTYPE_VN8800 = 95
XL_HWTYPE_VN5610A = 101
XL_HWTYPE_VN7640 = 102
XL_HWTYPE_VN4610 = 105
XL_HWTYPE_VN5430 = 109
XL_HWTYPE_VN1530 = 112
XL_HWTYPE_VN1531 = 113
XL_HWTYPE_VN1670 = 120
XL_HWTYPE_VN5620A = 121

_HWTYPE_NAMES: Dict[int, str] = {
    XL_HWTYPE_VIRTUAL: "Virtual",
    XL_HWTYPE_VN2600: "VN2600",
    XL_HWTYPE_VN3600: "VN3600",
    XL_HWTYPE_VN7600: "VN7600",
    XL_HWTYPE_VN8900: "VN8900",
    XL_HWTYPE_VN8950: "VN8950",
    XL_HWTYPE_VN2640: "VN2640",
    XL_HWTYPE_VN1610: "VN1610",
    XL_HWTYPE_VN1614: "VN1614",
    XL_HWTYPE_VN1630: "VN1630",
    XL_HWTYPE_VN1615: "VN1615",
    XL_HWTYPE_VN1640: "VN1640",
    XL_HWTYPE_VN1611: "VN1611",
    XL_HWTYPE_VN8970: "VN8970",
    XL_HWTYPE_VN5240: "VN5240",
    XL_HWTYPE_VN5610: "VN5610",
    XL_HWTYPE_VN5620: "VN5620",
    XL_HWTYPE_VN7570: "VN7570",
    XL_HWTYPE_VN5650: "VN5650",
    XL_HWTYPE_VN5611: "VN5611",
    XL_HWTYPE_VN5612: "VN5612",
    XL_HWTYPE_VN5614: "VN5614",
    XL_HWTYPE_VN1630_LOG: "VN1630_LOG",
    XL_HWTYPE_VN7610: "VN7610",
    XL_HWTYPE_VN7572: "VN7572",
    XL_HWTYPE_VN8972: "VN8972",
    XL_HWTYPE_VN1641: "VN1641",
    XL_HWTYPE_VN0601: "VN0601",
    XL_HWTYPE_VN5640: "VN5640",
    XL_HWTYPE_VN8800: "VN8800",
    XL_HWTYPE_VN5610A: "VN5610A",
    XL_HWTYPE_VN7640: "VN7640",
    XL_HWTYPE_VN4610: "VN4610",
    XL_HWTYPE_VN5430: "VN5430",
    XL_HWTYPE_VN1530: "VN1530",
    XL_HWTYPE_VN1531: "VN1531",
    XL_HWTYPE_VN1670: "VN1670",
    XL_HWTYPE_VN5620A: "VN5620A",
}

# Transceiver types relevant for CAN
XL_TRANSCEIVER_TYPE_CAN_HS = 3
XL_TRANSCEIVER_TYPE_CAN_LS = 4
XL_TRANSCEIVER_TYPE_CAN_SW = 5
XL_TRANSCEIVER_TYPE_CAN_FT = 6
XL_TRANSCEIVER_TYPE_CAN_FD = 17
XL_TRANSCEIVER_TYPE_CAN_FD_HS = 19
XL_TRANSCEIVER_TYPE_CAN_FD_HSS = 22

_CAN_TRANSCIEVERS = {
    XL_TRANSCEIVER_TYPE_CAN_HS,
    XL_TRANSCEIVER_TYPE_CAN_LS,
    XL_TRANSCEIVER_TYPE_CAN_SW,
    XL_TRANSCEIVER_TYPE_CAN_FT,
    XL_TRANSCEIVER_TYPE_CAN_FD,
    XL_TRANSCEIVER_TYPE_CAN_FD_HS,
    XL_TRANSCEIVER_TYPE_CAN_FD_HSS,
}

# XLevent tag values
XL_TRANSMIT_MSG = 10
XL_RECEIVE_MSG = 11
XL_CAN_EV_TAG_RX_OK = 0x0400
XL_CAN_EV_TAG_RX_ERROR = 0x0401
XL_CAN_EV_TAG_TX_ERROR = 0x0402
XL_CAN_EV_TAG_TX_OK = 0x0404
XL_CAN_EV_TAG_CHIP_STATE = 0x0409
XL_CAN_EV_TAG_TX_MSG = 0x0440

XL_CAN_ERRC_BIT_ERROR = 1
XL_CAN_ERRC_FORM_ERROR = 2
XL_CAN_ERRC_STUFF_ERROR = 3
XL_CAN_ERRC_OTHER_ERROR = 4
XL_CAN_ERRC_CRC_ERROR = 5
XL_CAN_ERRC_ACK_ERROR = 6
XL_CAN_ERRC_NACK_ERROR = 7
XL_CAN_ERRC_OVLD_ERROR = 8
XL_CAN_ERRC_EXCPT_ERROR = 9

# xlCanTransmit flags
XL_CAN_TXMSG_FLAG_EDL = 0x0001
XL_CAN_TXMSG_FLAG_BRS = 0x0002
XL_CAN_TXMSG_FLAG_RTR = 0x0004

# xlCanReceive flags
XL_CAN_RXMSG_FLAG_EDL = 0x0001
XL_CAN_RXMSG_FLAG_BRS = 0x0002
XL_CAN_RXMSG_FLAG_RTR = 0x0004
XL_CAN_RXMSG_FLAG_EF = 0x0008  # error frame

# xlActivateChannel flags
XL_ACTIVATE_NONE = 0
XL_ACTIVATE_RESET_CLOCK = 0x0001

# Return codes
XL_SUCCESS = 0
XL_ERR_QUEUE_IS_EMPTY = 10

# XLaccess special value
XL_INVALID_PORTHANDLE = C.c_long(-1).value  # -1

_CAN_ERROR_NAMES: Dict[int, str] = {
    XL_CAN_ERRC_BIT_ERROR: "Bit error",
    XL_CAN_ERRC_FORM_ERROR: "Form error",
    XL_CAN_ERRC_STUFF_ERROR: "Stuff error",
    XL_CAN_ERRC_OTHER_ERROR: "Other error",
    XL_CAN_ERRC_CRC_ERROR: "CRC error",
    XL_CAN_ERRC_ACK_ERROR: "ACK error",
    XL_CAN_ERRC_NACK_ERROR: "NACK error",
    XL_CAN_ERRC_OVLD_ERROR: "Overload error",
    XL_CAN_ERRC_EXCPT_ERROR: "Exception error",
}

_CAN_CHIP_STATE_NAMES: Dict[int, str] = {
    0: "error-active",
    1: "error-warning",
    2: "error-passive",
    3: "bus-off",
}


def _describe_can_error_code(error_code: int) -> str:
    return _CAN_ERROR_NAMES.get(int(error_code), f"Unknown error {int(error_code)}")


def _describe_chip_state(bus_status: int) -> str:
    return _CAN_CHIP_STATE_NAMES.get(int(bus_status), f"bus-status-{int(bus_status)}")

# Max data length for CAN FD
XL_CAN_MAX_DLC = 15

# DLC to byte count mapping for CAN FD
_DLC_TO_LEN = [0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64]
_LEN_TO_DLC = {v: i for i, v in enumerate(_DLC_TO_LEN)}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _is_64bit_process() -> bool:
    return C.sizeof(C.c_void_p) == 8


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip(), 0)
    except Exception:
        return int(default)


def _safe_positive_int(value: object, default: int) -> int:
    parsed = _safe_int(value, default)
    return parsed if parsed > 0 else int(default)


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return float(default)


def _safe_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _hw_type_name(hw_type: int) -> str:
    return _HWTYPE_NAMES.get(hw_type, f"Unknown({hw_type})")


def _bytes_to_text(raw: object) -> str:
    data = bytes(raw)
    return data.split(b"\0", 1)[0].decode("ascii", errors="ignore").strip()


def _len_to_dlc(length: int) -> int:
    """Convert byte count to DLC value (handles CAN FD DLC encoding)."""
    if length <= 8:
        return min(length, 8)
    for dlc, sz in enumerate(_DLC_TO_LEN):
        if sz >= length:
            return dlc
    return 15


def _dlc_to_len(dlc: int) -> int:
    """Convert DLC value to byte count."""
    if dlc < len(_DLC_TO_LEN):
        return _DLC_TO_LEN[dlc]
    return 64


def _extra_timing_value(extra: Dict[str, object], *keys: str, default: int) -> int:
    for key in keys:
        if key in extra:
            return _safe_positive_int(extra.get(key), default)
    return int(default)


def _extra_sample_point(
    extra: Dict[str, object],
    *keys: str,
    default: float = 80.0,
) -> float:
    for key in keys:
        if key in extra:
            value = _safe_float(extra.get(key), default)
            return min(90.0, max(50.0, value))
    return float(default)


def _timing_from_sample_point(sample_point_percent: float) -> tuple[int, int, int]:
    sample_point = min(90.0, max(50.0, float(sample_point_percent)))
    best: tuple[float, int, int, int, int] | None = None
    for total_tq in range(8, 26):
        sample_tq = int((total_tq * sample_point / 100.0) + 0.5)
        tseg1 = max(1, min(sample_tq - 1, total_tq - 2))
        tseg2 = total_tq - 1 - tseg1
        if tseg2 <= 0:
            continue
        actual = ((1 + tseg1) / total_tq) * 100.0
        rank = (abs(actual - sample_point), abs(total_tq - 10), total_tq, tseg1, tseg2)
        if best is None or rank < best:
            best = rank
    if best is None:
        return (2, 7, 2)
    _, _, _, tseg1, tseg2 = best
    return (min(2, tseg2), int(tseg1), int(tseg2))


# ---------------------------------------------------------------------------
# DLL search paths
# ---------------------------------------------------------------------------

def _candidate_libpaths(extra: Dict[str, object] | None = None) -> List[str]:
    paths: List[str] = []

    def _add(path: str) -> None:
        normalized = str(path or "").strip()
        if normalized and normalized not in paths:
            paths.append(normalized)

    # User-provided override
    if extra:
        _add(str(extra.get("libpath", "")))

    # Environment variable overrides
    for env_name in ("VXLAPI_LIBPATH", "VXLAPI_LIBRARY_PATH", "VECTOR_LIBPATH"):
        _add(os.environ.get(env_name, ""))

    # Architecture-specific DLL name
    dll_name = "vxlapi64.dll" if _is_64bit_process() else "vxlapi.dll"

    # Common Vector Driver install paths
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")

    vector_dirs = [
        os.path.join(program_files, "Vector", "vXApi"),
        os.path.join(program_files, "Vector", "Vector Driver", "bin"),
        os.path.join(program_files_x86, "Vector", "vXApi"),
        os.path.join(program_files_x86, "Vector", "Vector Driver", "bin"),
        r"C:\Vector",
        r"C:\Vector\vXApi",
        r"C:\Vector\Vector Driver\bin",
    ]
    for vdir in vector_dirs:
        _add(os.path.join(vdir, dll_name))

    # Current Vector installers use versioned "XL Driver Library ..." folders.
    for root in (Path(program_files) / "Vector", Path(program_files_x86) / "Vector"):
        try:
            versioned_dirs = sorted(root.glob("XL Driver Library*"), reverse=True)
        except Exception:
            versioned_dirs = []
        for vdir in versioned_dirs:
            _add(str(vdir / "bin" / dll_name))
            _add(str(vdir / "exec" / dll_name))

    # System PATH directories
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry:
            _add(os.path.join(entry, dll_name))

    # Resource/app directories (for bundled distribution)
    _add(str(resource_path(dll_name)))
    _add(str(app_path(dll_name)))
    _add(str(resource_path("vector")))
    _add(str(app_path("vector")))

    return paths


def _describe_dll_load_error(libpath: str, exc: OSError) -> str:
    message = str(exc)
    details = [message]
    normalized = message.lower()
    expected_arch = "64-bit" if _is_64bit_process() else "32-bit"
    if "winerror 193" in normalized or "not a valid win32 application" in normalized:
        details.append(
            f"Architecture mismatch: the current app process is {expected_arch}, "
            f"but the DLL at '{libpath}' has a different bitness."
        )
    elif "could not find" in normalized or "not found" in normalized:
        details.append(
            "Vector XL Driver Library not found. Install the Vector Driver from "
            "https://www.vector.com/ and ensure vxlapi64.dll is on the system PATH."
        )
    else:
        details.append(
            "Failed to load vxlapi.dll. Ensure the Vector XL Driver Library is "
            f"installed correctly for {expected_arch} applications."
        )
    return " ".join(details)


# ---------------------------------------------------------------------------
# ctypes structure definitions for vxlapi
# ---------------------------------------------------------------------------

class XLaccess(C.c_uint64):
    """Alias for channel mask / access mask."""


class XLportHandle(C.c_long):
    """Alias for port handle."""


# Bus parameters for CAN / CAN FD
class XLbusParams(C.Structure):
    pass  # forward declaration for self-referential union


class _XLbusParamsCan(C.Structure):
    _pack_ = 1
    _fields_ = [
        ("bitRate", C.c_uint32),
        ("sjw", C.c_ubyte),
        ("tseg1", C.c_ubyte),
        ("tseg2", C.c_ubyte),
        ("sam", C.c_ubyte),
        ("outputMode", C.c_ubyte),
        ("reserved1", C.c_ubyte * 7),
        ("canOpMode", C.c_ubyte),
    ]


class _XLbusParamsCanFd(C.Structure):
    _pack_ = 1
    _fields_ = [
        ("arbitrationBitRate", C.c_uint32),
        ("sjwAbr", C.c_ubyte),
        ("tseg1Abr", C.c_ubyte),
        ("tseg2Abr", C.c_ubyte),
        ("samAbr", C.c_ubyte),
        ("outputMode", C.c_ubyte),
        ("sjwDbr", C.c_ubyte),
        ("tseg1Dbr", C.c_ubyte),
        ("tseg2Dbr", C.c_ubyte),
        ("dataBitRate", C.c_uint32),
        ("canOpMode", C.c_ubyte),
    ]


class _XLbusParamsUnion(C.Union):
    _pack_ = 1
    _fields_ = [
        ("can", _XLbusParamsCan),
        ("canFd", _XLbusParamsCanFd),
        ("raw", C.c_ubyte * 28),
    ]


class XLbusParams(C.Structure):
    _anonymous_ = ("busParams",)
    _pack_ = 1
    _fields_ = [
        ("busType", C.c_uint32),
        ("busParams", _XLbusParamsUnion),
    ]


class XLcanFdConf(C.Structure):
    _pack_ = 1
    _fields_ = [
        ("arbitrationBitRate", C.c_uint32),
        ("sjwAbr", C.c_uint32),
        ("tseg1Abr", C.c_uint32),
        ("tseg2Abr", C.c_uint32),
        ("dataBitRate", C.c_uint32),
        ("sjwDbr", C.c_uint32),
        ("tseg1Dbr", C.c_uint32),
        ("tseg2Dbr", C.c_uint32),
        ("reserved", C.c_ubyte),
        ("options", C.c_ubyte),
        ("reserved1", C.c_ubyte * 2),
        ("reserved2", C.c_uint32),
    ]


class XLchannelConfig(C.Structure):
    _pack_ = 1
    _fields_ = [
        ("name", C.c_char * 32),               # XL_MAX_LENGTH + 1
        ("hwType", C.c_ubyte),
        ("hwIndex", C.c_ubyte),
        ("hwChannel", C.c_ubyte),
        ("transceiverType", C.c_uint16),
        ("transceiverState", C.c_uint16),
        ("configError", C.c_uint16),
        ("channelIndex", C.c_ubyte),
        ("channelMask", C.c_uint64),           # XLaccess
        ("channelCapabilities", C.c_uint32),
        ("channelBusCapabilities", C.c_uint32),
        ("isOnBus", C.c_ubyte),
        ("connectedBusType", C.c_uint32),
        ("channelBusParams", XLbusParams),
        ("_doNotUse", C.c_uint32),
        ("driverVersion", C.c_uint32),
        ("interfaceVersion", C.c_uint32),
        ("raw_data", C.c_uint32 * 10),
        ("serialNumber", C.c_uint32),
        ("articleNumber", C.c_uint32),
        ("transceiverName", C.c_char * 32),
        ("specialCabFlags", C.c_uint32),
        ("dominantTimeout", C.c_uint32),
        ("dominantRecessiveDelay", C.c_ubyte),
        ("recessiveDominantDelay", C.c_ubyte),
        ("connectionInfo", C.c_ubyte),
        ("currentlyAvailableTimestamps", C.c_ubyte),
        ("minimalSupplyVoltage", C.c_uint16),
        ("maximalSupplyVoltage", C.c_uint16),
        ("maximalBaudrate", C.c_uint32),
        ("fpgaCoreCapabilities", C.c_ubyte),
        ("specialDeviceStatus", C.c_ubyte),
        ("channelBusActiveCapabilities", C.c_uint16),
        ("breakOffset", C.c_uint16),
        ("delimiterOffset", C.c_uint16),
        ("reserved", C.c_uint32 * 3),
    ]


class XLdriverConfig(C.Structure):
    _pack_ = 1
    _fields_ = [
        ("dllVersion", C.c_uint32),
        ("channelCount", C.c_uint32),
        ("reserved", C.c_uint32 * 10),
        ("channel", XLchannelConfig * 64),
    ]


# --- XLevent structures for CAN transmit/receive ---

class _XLcanTxMsg(C.Structure):
    _fields_ = [
        ("canId", C.c_uint32),
        ("flags", C.c_uint16),
        ("dlc", C.c_uint8),
        ("reserved", C.c_uint8),
        ("data", C.c_uint8 * 64),
    ]


class _XLcanRxMsg(C.Structure):
    _fields_ = [
        ("canId", C.c_uint32),
        ("flags", C.c_uint16),
        ("dlc", C.c_uint8),
        ("reserved", C.c_uint8),
        ("data", C.c_uint8 * 64),
    ]


class _XLtagData(C.Union):
    _fields_ = [
        ("txMsg", _XLcanTxMsg),
        ("rxMsg", _XLcanRxMsg),
    ]


class XLevent(C.Structure):
    _anonymous_ = ("tagData",)
    _fields_ = [
        ("tag", C.c_byte),
        ("chanIndex", C.c_byte),
        ("reserved1", C.c_byte),
        ("reserved2", C.c_byte),
        ("tagData", _XLtagData),
        ("timeStamp", C.c_uint64),
        ("reserved3", C.c_uint32 * 3),
    ]


class XLcanTxMsg(C.Structure):
    _pack_ = 8
    _fields_ = [
        ("canId", C.c_uint32),
        ("msgFlags", C.c_uint32),
        ("dlc", C.c_ubyte),
        ("reserved", C.c_ubyte * 7),
        ("data", C.c_ubyte * 64),
    ]


class _XLcanTxTagData(C.Union):
    _pack_ = 8
    _fields_ = [("canMsg", XLcanTxMsg)]


class XLcanTxEvent(C.Structure):
    _anonymous_ = ("tagData",)
    _pack_ = 8
    _fields_ = [
        ("tag", C.c_uint16),
        ("transId", C.c_uint16),
        ("channelIndex", C.c_ubyte),
        ("reserved", C.c_ubyte * 3),
        ("tagData", _XLcanTxTagData),
    ]


class XLcanRxMsg(C.Structure):
    _pack_ = 8
    _fields_ = [
        ("canId", C.c_uint32),
        ("msgFlags", C.c_uint32),
        ("crc", C.c_uint32),
        ("reserved1", C.c_ubyte * 12),
        ("totalBitCnt", C.c_uint16),
        ("dlc", C.c_ubyte),
        ("reserved", C.c_ubyte * 5),
        ("data", C.c_ubyte * 64),
    ]


class XLcanTxRequest(C.Structure):
    _pack_ = 8
    _fields_ = [
        ("canId", C.c_uint32),
        ("msgFlags", C.c_uint32),
        ("dlc", C.c_ubyte),
        ("reserved1", C.c_ubyte),
        ("reserved", C.c_uint16),
        ("data", C.c_ubyte * 64),
    ]


class XLcanChipState(C.Structure):
    _pack_ = 8
    _fields_ = [
        ("busStatus", C.c_ubyte),
        ("txErrorCounter", C.c_ubyte),
        ("rxErrorCounter", C.c_ubyte),
        ("reserved", C.c_ubyte),
        ("reserved0", C.c_uint32),
    ]


class XLcanError(C.Structure):
    _pack_ = 8
    _fields_ = [
        ("errorCode", C.c_ubyte),
        ("reserved", C.c_ubyte * 95),
    ]


class _XLcanRxTagData(C.Union):
    _pack_ = 8
    _fields_ = [
        ("raw", C.c_ubyte * 96),
        ("canRxOkMsg", XLcanRxMsg),
        ("canTxOkMsg", XLcanRxMsg),
        ("canTxRequest", XLcanTxRequest),
        ("canError", XLcanError),
        ("canChipState", XLcanChipState),
    ]


class XLcanRxEvent(C.Structure):
    _anonymous_ = ("tagData",)
    _pack_ = 8
    _fields_ = [
        ("size", C.c_uint32),
        ("tag", C.c_uint16),
        ("channelIndex", C.c_uint16),
        ("userHandle", C.c_uint32),
        ("flagsChip", C.c_uint16),
        ("reserved0", C.c_uint16),
        ("reserved1", C.c_uint64),
        ("timeStampSync", C.c_uint64),
        ("tagData", _XLcanRxTagData),
    ]


# ---------------------------------------------------------------------------
# vxlapi DLL wrapper
# ---------------------------------------------------------------------------

class _VxlApiDll:
    """Lazy-binding wrapper around vxlapi.dll."""

    def __init__(self, libpath: str) -> None:
        self.libpath = libpath
        self._dll_directories: List[object] = []

        # Add DLL search directories for dependencies
        lib_dir = str(Path(libpath).parent)
        if os.name == "nt" and Path(lib_dir).exists():
            try:
                self._dll_directories.append(os.add_dll_directory(lib_dir))
            except Exception:
                pass

        # Load the DLL
        if not Path(libpath).exists():
            raise RuntimeError(f"vxlapi DLL not found at '{libpath}'.")
        try:
            self.dll = C.WinDLL(str(libpath))
        except OSError as exc:
            raise RuntimeError(_describe_dll_load_error(libpath, exc)) from exc

        self._bind()

    def _bind(self) -> None:
        dll = self.dll

        # Driver management
        dll.xlOpenDriver.argtypes = []
        dll.xlOpenDriver.restype = C.c_int

        dll.xlCloseDriver.argtypes = []
        dll.xlCloseDriver.restype = C.c_int

        dll.xlGetDriverConfig.argtypes = [C.POINTER(XLdriverConfig)]
        dll.xlGetDriverConfig.restype = C.c_int

        # Port management
        dll.xlOpenPort.argtypes = [
            C.POINTER(XLportHandle),  # portHandle
            C.c_char_p,               # appName
            C.c_uint64,               # accessMask
            C.POINTER(C.c_uint64),    # permissionMask
            C.c_uint32,               # rxQueueSize
            C.c_uint32,               # xlInterfaceVersion
            C.c_uint32,               # busType
        ]
        dll.xlOpenPort.restype = C.c_int

        dll.xlClosePort.argtypes = [XLportHandle]
        dll.xlClosePort.restype = C.c_int

        dll.xlActivateChannel.argtypes = [
            XLportHandle,             # portHandle
            C.c_uint64,               # accessMask
            C.c_uint32,               # busType
            C.c_uint32,               # flags
        ]
        dll.xlActivateChannel.restype = C.c_int

        dll.xlDeactivateChannel.argtypes = [
            XLportHandle,             # portHandle
            C.c_uint64,               # accessMask
        ]
        dll.xlDeactivateChannel.restype = C.c_int

        # CAN parameter configuration
        dll.xlCanSetChannelParams.argtypes = [
            XLportHandle,             # portHandle
            C.c_uint64,               # accessMask
            C.c_uint32,               # bitrate
        ]
        dll.xlCanSetChannelParams.restype = C.c_int

        dll.xlCanSetChannelParamsC200.argtypes = [
            XLportHandle,             # portHandle
            C.c_uint64,               # accessMask
            C.c_uint8,                # btr0
            C.c_uint8,                # btr1
        ]
        dll.xlCanSetChannelParamsC200.restype = C.c_int

        dll.xlCanSetChannelBitrate.argtypes = [
            XLportHandle,             # portHandle
            C.c_uint64,               # accessMask
            C.c_uint32,               # bitrate
        ]
        dll.xlCanSetChannelBitrate.restype = C.c_int

        # CAN FD configuration
        dll.xlCanFdSetConfiguration.argtypes = [
            XLportHandle,             # portHandle
            C.c_uint64,               # accessMask
            C.POINTER(XLcanFdConf),   # pCanFdConf
        ]
        dll.xlCanFdSetConfiguration.restype = C.c_int

        # Transmit / Receive
        dll.xlCanTransmit.argtypes = [
            XLportHandle,             # portHandle
            C.c_uint64,               # accessMask
            C.POINTER(C.c_uint32),    # pMsgCount
            C.POINTER(XLevent),       # pEvent
        ]
        dll.xlCanTransmit.restype = C.c_int

        dll.xlCanTransmitEx.argtypes = [
            XLportHandle,             # portHandle
            C.c_uint64,               # accessMask
            C.c_uint32,               # msgCnt
            C.POINTER(C.c_uint32),    # pMsgCntSent
            C.POINTER(XLcanTxEvent),  # pXlCanTxEvt
        ]
        dll.xlCanTransmitEx.restype = C.c_int

        dll.xlCanReceive.argtypes = [
            XLportHandle,             # portHandle
            C.POINTER(XLcanRxEvent),  # pXlCanRxEvt
        ]
        dll.xlCanReceive.restype = C.c_int

        # Queue / notification
        try:
            dll.xlCanReceiveEx.argtypes = [
                XLportHandle,             # portHandle
                C.POINTER(C.c_uint32),    # pMsgCount
                C.POINTER(XLevent),       # pEvent
            ]
            dll.xlCanReceiveEx.restype = C.c_int
        except AttributeError:
            pass

        # Reset / flush
        dll.xlCanResetAcceptance.argtypes = [
            XLportHandle,             # portHandle
            C.c_uint64,               # accessMask
        ]
        dll.xlCanResetAcceptance.restype = C.c_int

        # Get channel mask from serial and hwChannel
        dll.xlGetChannelMask.argtypes = [
            C.c_int,                  # hwType
            C.c_int,                  # hwIndex
            C.c_int,                  # hwChannel
        ]
        dll.xlGetChannelMask.restype = C.c_uint64

        # Optional helpers. Some XL Driver Library versions do not export these.
        try:
            dll.xlGetSerialNumber.argtypes = [
                C.c_uint64,               # channelMask
                C.POINTER(C.c_uint32),    # pSerialNumber
            ]
            dll.xlGetSerialNumber.restype = C.c_int
        except AttributeError:
            pass

        try:
            dll.xlGetChannelIndex.argtypes = [
                C.c_int,                  # hwType
                C.c_int,                  # hwIndex
                C.c_int,                  # hwChannel
            ]
            dll.xlGetChannelIndex.restype = C.c_int
        except AttributeError:
            pass

        # Flush transmit queue
        dll.xlCanFlushTransmitQueue.argtypes = [
            XLportHandle,             # portHandle
            C.c_uint64,               # accessMask
        ]
        dll.xlCanFlushTransmitQueue.restype = C.c_int

        # Get receive queue level
        try:
            dll.xlCanGetReceiveQueueLevel.argtypes = [
                XLportHandle,             # portHandle
            ]
            dll.xlCanGetReceiveQueueLevel.restype = C.c_int
        except AttributeError:
            pass

        # Set notification (for event-based receive)
        dll.xlSetNotification.argtypes = [
            XLportHandle,             # portHandle
            C.POINTER(C.c_void_p),    # hEvent
            C.c_int,                  # queueLevel
        ]
        dll.xlSetNotification.restype = C.c_int

    # --- High-level API wrappers ---

    def open_driver(self) -> int:
        return int(self.dll.xlOpenDriver())

    def close_driver(self) -> int:
        return int(self.dll.xlCloseDriver())

    def get_driver_config(self) -> XLdriverConfig:
        config = XLdriverConfig()
        ret = int(self.dll.xlGetDriverConfig(C.byref(config)))
        if ret != XL_SUCCESS:
            raise RuntimeError(f"xlGetDriverConfig failed, error code = {ret}.")
        return config

    def open_port(
        self,
        app_name: str,
        access_mask: int,
        rx_queue_size: int = 256,
        interface_version: int = XL_INTERFACE_VERSION_V3,
        bus_type: int = XL_BUS_TYPE_CAN,
    ) -> Tuple[int, int]:
        """Open a port and return (portHandle, permissionMask)."""
        port_handle = XLportHandle(XL_INVALID_PORTHANDLE)
        permission_mask = C.c_uint64(access_mask)
        ret = int(self.dll.xlOpenPort(
            C.byref(port_handle),
            app_name.encode("ascii"),
            C.c_uint64(access_mask),
            C.byref(permission_mask),
            C.c_uint32(rx_queue_size),
            C.c_uint32(interface_version),
            C.c_uint32(bus_type),
        ))
        if ret != XL_SUCCESS:
            raise RuntimeError(f"xlOpenPort failed, error code = {ret}.")
        if access_mask and (int(permission_mask.value) & int(access_mask)) != int(access_mask):
            raise RuntimeError(
                f"xlOpenPort did not grant init access. requested=0x{access_mask:X}, "
                f"permission=0x{int(permission_mask.value):X}."
            )
        return int(port_handle.value), int(permission_mask.value)

    def close_port(self, port_handle: int) -> int:
        return int(self.dll.xlClosePort(XLportHandle(port_handle)))

    def activate_channel(
        self, port_handle: int, access_mask: int, bus_type: int = XL_BUS_TYPE_CAN,
        flags: int = XL_ACTIVATE_RESET_CLOCK,
    ) -> int:
        return int(self.dll.xlActivateChannel(
            XLportHandle(port_handle),
            C.c_uint64(access_mask),
            C.c_uint32(bus_type),
            C.c_uint32(flags),
        ))

    def deactivate_channel(self, port_handle: int, access_mask: int) -> int:
        return int(self.dll.xlDeactivateChannel(
            XLportHandle(port_handle),
            C.c_uint64(access_mask),
        ))

    def can_set_channel_bitrate(
        self, port_handle: int, access_mask: int, bitrate: int,
    ) -> int:
        return int(self.dll.xlCanSetChannelBitrate(
            XLportHandle(port_handle),
            C.c_uint64(access_mask),
            C.c_uint32(bitrate),
        ))

    def can_fd_set_configuration(
        self, port_handle: int, access_mask: int, can_fd_conf: XLcanFdConf,
    ) -> int:
        return int(self.dll.xlCanFdSetConfiguration(
            XLportHandle(port_handle),
            C.c_uint64(access_mask),
            C.byref(can_fd_conf),
        ))

    def can_transmit(
        self, port_handle: int, access_mask: int, events: List[XLevent],
    ) -> int:
        count = C.c_uint32(len(events))
        if not events:
            return XL_SUCCESS
        ret = int(self.dll.xlCanTransmit(
            XLportHandle(port_handle),
            C.c_uint64(access_mask),
            C.byref(count),
            C.byref(events[0]),
        ))
        return ret

    def can_receive(self, port_handle: int) -> Tuple[int, XLcanRxEvent | None]:
        """Receive a single event. Returns (returnCode, event)."""
        event = XLcanRxEvent()
        ret = int(self.dll.xlCanReceive(
            XLportHandle(port_handle),
            C.byref(event),
        ))
        if ret != XL_SUCCESS:
            return ret, None
        return ret, event

    def can_transmit_ex(
        self, port_handle: int, access_mask: int, event: XLcanTxEvent,
    ) -> int:
        sent_count = C.c_uint32(0)
        return int(self.dll.xlCanTransmitEx(
            XLportHandle(port_handle),
            C.c_uint64(access_mask),
            C.c_uint32(1),
            C.byref(sent_count),
            C.byref(event),
        ))

    def can_flush_transmit_queue(self, port_handle: int) -> int:
        return int(self.dll.xlCanFlushTransmitQueue(
            XLportHandle(port_handle), C.c_uint64(0)
        ))

    def get_channel_mask(self, hw_type: int, hw_index: int, hw_channel: int) -> int:
        return int(self.dll.xlGetChannelMask(
            C.c_int(hw_type), C.c_int(hw_index), C.c_int(hw_channel),
        ))

    def get_serial_number(self, channel_mask: int) -> int:
        if not hasattr(self.dll, "xlGetSerialNumber"):
            return 0
        serial = C.c_uint32(0)
        ret = int(self.dll.xlGetSerialNumber(
            C.c_uint64(channel_mask), C.byref(serial),
        ))
        if ret != XL_SUCCESS:
            return 0
        return int(serial.value)

    def get_channel_index(self, hw_type: int, hw_index: int, hw_channel: int) -> int:
        if not hasattr(self.dll, "xlGetChannelIndex"):
            return -1
        return int(self.dll.xlGetChannelIndex(
            C.c_int(hw_type), C.c_int(hw_index), C.c_int(hw_channel)
        ))


# ---------------------------------------------------------------------------
# DLL cache (singleton per libpath)
# ---------------------------------------------------------------------------

_DLL_CACHE: Dict[str, _VxlApiDll] = {}
_DLL_CACHE_LOCK = threading.Lock()


def _load_dll(libpath: str) -> _VxlApiDll:
    with _DLL_CACHE_LOCK:
        dll = _DLL_CACHE.get(libpath)
        if dll is None:
            dll = _VxlApiDll(libpath)
            _DLL_CACHE[libpath] = dll
        return dll


# ---------------------------------------------------------------------------
# Shared port registry (like _ZlgSharedRegistry)
# ---------------------------------------------------------------------------

class _VectorSharedPort:
    """Manages a single xlOpenPort with one or more activated channels."""

    def __init__(self, dll: _VxlApiDll, access_mask: int) -> None:
        self.dll = dll
        self.access_mask = int(access_mask)
        self.lock = threading.RLock()
        self.refcount = 0
        self.port_handle: int = XL_INVALID_PORTHANDLE
        self.permission_mask: int = 0
        self.activated_channels: Dict[int, bool] = {}  # channelMask -> is_fd
        self._driver_opened = False

    def _ensure_driver_open(self) -> None:
        if self._driver_opened:
            return
        ret = self.dll.open_driver()
        if ret != XL_SUCCESS:
            raise RuntimeError(f"xlOpenDriver failed, error code = {ret}.")
        self._driver_opened = True

    def acquire_channel(
        self,
        channel_mask: int,
        hw: HardwareConfig,
    ) -> None:
        with self.lock:
            opened_port_now = False

            # Ensure driver is open
            self._ensure_driver_open()

            # Open port if not yet open
            if self.port_handle == XL_INVALID_PORTHANDLE:
                self.port_handle, self.permission_mask = self.dll.open_port(
                    app_name="CANFD_Router",
                    access_mask=self.access_mask,
                    rx_queue_size=4096,
                    interface_version=XL_INTERFACE_VERSION_V4 if hw.fd else XL_INTERFACE_VERSION_V3,
                )
                opened_port_now = True

            # Check if channel already activated
            if channel_mask in self.activated_channels:
                self.refcount += 1
                return

            try:
                # Configure channel parameters
                is_fd = hw.fd
                self._configure_channel(channel_mask, hw, is_fd)

                # Activate the channel
                ret = self.dll.activate_channel(
                    self.port_handle,
                    channel_mask,
                    flags=XL_ACTIVATE_RESET_CLOCK,
                )
                if ret != XL_SUCCESS:
                    raise RuntimeError(
                        f"xlActivateChannel failed for channel mask 0x{channel_mask:X}, "
                        f"error code = {ret}."
                    )

                self.activated_channels[channel_mask] = is_fd
                self.refcount += 1

            except Exception:
                # Clean up on failure
                if channel_mask in self.activated_channels:
                    try:
                        self.dll.deactivate_channel(self.port_handle, channel_mask)
                    except Exception:
                        pass
                    del self.activated_channels[channel_mask]
                if opened_port_now and self.port_handle != XL_INVALID_PORTHANDLE:
                    try:
                        self.dll.close_port(self.port_handle)
                    except Exception:
                        pass
                    self.port_handle = XL_INVALID_PORTHANDLE
                raise

    def _configure_channel(
        self, channel_mask: int, hw: HardwareConfig, is_fd: bool,
    ) -> None:
        if is_fd:
            # CAN FD configuration
            can_fd_conf = XLcanFdConf()
            can_fd_conf.arbitrationBitRate = int(hw.bitrate)
            can_fd_conf.dataBitRate = int(hw.data_bitrate)
            # Default to an 80% sample point. The previous 70% timing can
            # produce CAN FD bit errors on networks configured like CANoe.
            extra = hw.extra or {}
            default_abr = _timing_from_sample_point(
                _extra_sample_point(
                    extra,
                    "vector_fd_sample_point_abr",
                    "vector_fd_nominal_sample_point",
                    "sample_point_abr",
                    default=80.0,
                )
            )
            default_dbr = _timing_from_sample_point(
                _extra_sample_point(
                    extra,
                    "vector_fd_sample_point_dbr",
                    "vector_fd_data_sample_point",
                    "sample_point_dbr",
                    default=80.0,
                )
            )
            can_fd_conf.sjwAbr = _extra_timing_value(
                extra, "vector_fd_sjw_abr", "sjwAbr", default=default_abr[0]
            )
            can_fd_conf.tseg1Abr = _extra_timing_value(
                extra, "vector_fd_tseg1_abr", "tseg1Abr", default=default_abr[1]
            )
            can_fd_conf.tseg2Abr = _extra_timing_value(
                extra, "vector_fd_tseg2_abr", "tseg2Abr", default=default_abr[2]
            )
            can_fd_conf.sjwDbr = _extra_timing_value(
                extra, "vector_fd_sjw_dbr", "sjwDbr", default=default_dbr[0]
            )
            can_fd_conf.tseg1Dbr = _extra_timing_value(
                extra, "vector_fd_tseg1_dbr", "tseg1Dbr", default=default_dbr[1]
            )
            can_fd_conf.tseg2Dbr = _extra_timing_value(
                extra, "vector_fd_tseg2_dbr", "tseg2Dbr", default=default_dbr[2]
            )

            ret = self.dll.can_fd_set_configuration(
                self.port_handle, channel_mask, can_fd_conf,
            )
            if ret != XL_SUCCESS:
                raise RuntimeError(
                    f"xlCanFdSetConfiguration failed for channel mask 0x{channel_mask:X}, "
                    f"error code = {ret}."
                )
        else:
            # Classic CAN configuration
            ret = self.dll.can_set_channel_bitrate(
                self.port_handle, channel_mask, int(hw.bitrate),
            )
            if ret != XL_SUCCESS:
                raise RuntimeError(
                    f"xlCanSetChannelBitrate failed for channel mask 0x{channel_mask:X}, "
                    f"error code = {ret}."
                )

    def release(self) -> None:
        with self.lock:
            self.refcount = max(0, self.refcount - 1)
            if self.refcount == 0 and self.port_handle != XL_INVALID_PORTHANDLE:
                # Deactivate all channels
                for channel_mask in list(self.activated_channels):
                    try:
                        self.dll.deactivate_channel(self.port_handle, channel_mask)
                    except Exception:
                        pass
                self.activated_channels.clear()
                # Close port
                try:
                    self.dll.close_port(self.port_handle)
                except Exception:
                    pass
                self.port_handle = XL_INVALID_PORTHANDLE
                # Close driver
                if self._driver_opened:
                    try:
                        self.dll.close_driver()
                    except Exception:
                        pass
                    self._driver_opened = False


class _VectorSharedRegistry:
    _lock = threading.Lock()
    _ports: Dict[Tuple[str, int], _VectorSharedPort] = {}

    @classmethod
    def acquire(
        cls,
        *,
        dll: _VxlApiDll,
        access_mask: int,
        channel_mask: int,
        hw: HardwareConfig,
    ) -> _VectorSharedPort:
        key = (dll.libpath, int(access_mask))
        with cls._lock:
            shared = cls._ports.get(key)
            if shared is None:
                shared = _VectorSharedPort(dll, int(access_mask))
                cls._ports[key] = shared
        try:
            shared.acquire_channel(int(channel_mask), hw)
            return shared
        except Exception:
            with cls._lock:
                if shared.refcount == 0 and shared.port_handle == XL_INVALID_PORTHANDLE:
                    stale = [item for item, value in cls._ports.items() if value is shared]
                    for item in stale:
                        cls._ports.pop(item, None)
            raise

    @classmethod
    def release(cls, shared: _VectorSharedPort) -> None:
        shared.release()
        with cls._lock:
            if shared.refcount == 0:
                stale = [key for key, value in cls._ports.items() if value is shared]
                for key in stale:
                    cls._ports.pop(key, None)


# ---------------------------------------------------------------------------
# VectorAdapter — implements BusAdapter
# ---------------------------------------------------------------------------

class VectorAdapter(BusAdapter):
    def __init__(self, config: HardwareConfig):
        self._config = config
        self._shared: _VectorSharedPort | None = None
        self._channel_mask: int = 0

    @property
    def is_open(self) -> bool:
        return self._shared is not None and self._channel_mask != 0

    def open(self) -> None:
        if self.is_open:
            return

        channel_mask = _safe_int(self._config.extra.get("channel_mask"), 0)
        access_mask = _safe_int(self._config.extra.get("access_mask"), 0)

        if access_mask == 0:
            access_mask = channel_mask

        if channel_mask == 0:
            # Try to derive channel_mask from hwType / hwIndex / hwChannel
            hw_type = _safe_int(self._config.extra.get("hw_type_val"), -1)
            hw_index = _safe_int(self._config.extra.get("hw_index"), 0)
            hw_channel = _safe_int(self._config.extra.get("hw_channel"), 0)
            if hw_type >= 0:
                channel_mask = 1 << (hw_index * 2 + hw_channel)

        if channel_mask == 0:
            raise RuntimeError(
                "Vector port is missing channel_mask. Please import it from Scan Device."
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
                shared = _VectorSharedRegistry.acquire(
                    dll=dll,
                    access_mask=access_mask,
                    channel_mask=channel_mask,
                    hw=self._config,
                )
                self._shared = shared
                self._channel_mask = channel_mask
                self._config.extra["libpath"] = libpath
                self._config.extra["channel_mask"] = str(channel_mask)
                self._config.extra["access_mask"] = str(access_mask)
                return
            except Exception as exc:
                runtime_errors.append(f"runtime '{libpath}': {exc}")

        if runtime_errors:
            raise RuntimeError(runtime_errors[0])
        if load_errors:
            raise RuntimeError(load_errors[0])
        raise RuntimeError("Failed to open Vector device.")

    def close(self) -> None:
        shared = self._shared
        self._shared = None
        self._channel_mask = 0
        if shared is not None:
            _VectorSharedRegistry.release(shared)

    def send(self, frame: Frame) -> None:
        if self._shared is None:
            raise RuntimeError("Adapter not opened.")

        event = XLcanTxEvent()
        event.tag = XL_CAN_EV_TAG_TX_MSG
        event.channelIndex = 0

        arbitration_id = int(frame.arbitration_id) & 0x1FFFFFFF

        if frame.is_extended_id:
            arbitration_id |= 0x80000000

        event.canMsg.canId = arbitration_id
        if frame.is_fd:
            event.canMsg.msgFlags = XL_CAN_TXMSG_FLAG_EDL
            if (frame.bitrate_switch if frame.bitrate_switch is not None else self._config.fd):
                event.canMsg.msgFlags |= XL_CAN_TXMSG_FLAG_BRS
            data_len = min(len(frame.data), 64)
            event.canMsg.dlc = _len_to_dlc(data_len)
            for idx in range(data_len):
                event.canMsg.data[idx] = int(frame.data[idx]) & 0xFF
        else:
            event.canMsg.msgFlags = 0
            data_len = min(len(frame.data), 8)
            event.canMsg.dlc = data_len
            for idx in range(data_len):
                event.canMsg.data[idx] = int(frame.data[idx]) & 0xFF

        ret = self._shared.dll.can_transmit_ex(
            self._shared.port_handle,
            self._channel_mask,
            event,
        )
        if ret != XL_SUCCESS:
            raise RuntimeError(
                f"Vector xlCanTransmitEx failed, error code = {ret}. "
                "Usually this means the Vector TX queue is saturated, the CAN bus has no ACK, "
                "bitrate/FD/DLC settings do not match, or termination/wiring is wrong."
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
            time.sleep(0.002)

    def _recv_once(self) -> Frame | None:
        if self._shared is None:
            return None

        ret, event = self._shared.dll.can_receive(self._shared.port_handle)
        if ret != XL_SUCCESS or event is None:
            return None
        tag = int(event.tag)
        if tag in (XL_CAN_EV_TAG_RX_ERROR, XL_CAN_EV_TAG_TX_ERROR):
            error_code = int(event.canError.errorCode)
            return Frame(
                arbitration_id=0,
                data=b"",
                timestamp=float(event.timeStampSync) / 1_000_000_000.0,
                channel=self._config.channel,
                is_error=True,
                error_source="RX_ERROR" if tag == XL_CAN_EV_TAG_RX_ERROR else "TX_ERROR",
                error_code=error_code,
                error_text=_describe_can_error_code(error_code),
                driver="vector",
            )
        if tag == XL_CAN_EV_TAG_CHIP_STATE:
            chip_state = event.canChipState
            bus_status = int(chip_state.busStatus)
            return Frame(
                arbitration_id=0,
                data=b"",
                timestamp=float(event.timeStampSync) / 1_000_000_000.0,
                channel=self._config.channel,
                is_error=True,
                error_source="CHIP_STATE",
                error_text="Chip state",
                bus_status=_describe_chip_state(bus_status),
                tx_error_count=int(chip_state.txErrorCounter),
                rx_error_count=int(chip_state.rxErrorCounter),
                driver="vector",
            )
        if tag not in (XL_CAN_EV_TAG_RX_OK, XL_CAN_EV_TAG_TX_OK):
            return None

        msg = event.canRxOkMsg if tag == XL_CAN_EV_TAG_RX_OK else event.canTxOkMsg
        can_id = int(msg.canId) & 0x1FFFFFFF
        is_extended = bool(msg.canId & 0x80000000)
        is_fd = bool(msg.msgFlags & XL_CAN_RXMSG_FLAG_EDL)
        is_rtr = bool(msg.msgFlags & XL_CAN_RXMSG_FLAG_RTR)

        if is_fd:
            data_len = _dlc_to_len(int(msg.dlc))
        else:
            data_len = min(int(msg.dlc), 8)

        # XLcanRxEvent uses synchronized nanosecond timestamps.
        timestamp = float(event.timeStampSync) / 1_000_000_000.0

        data = bytes(msg.data[:data_len])

        return Frame(
            arbitration_id=can_id,
            data=data,
            is_fd=is_fd,
            is_extended_id=is_extended,
            timestamp=timestamp,
            channel=self._config.channel,
        )


# ---------------------------------------------------------------------------
# VectorDriver — implements DriverPlugin
# ---------------------------------------------------------------------------

class VectorDriver(DriverPlugin):
    name = "vector"

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

            try:
                ret = dll.open_driver()
                if ret != XL_SUCCESS:
                    self._diagnostics.append(
                        f"xlOpenDriver failed at '{libpath}', error code = {ret}."
                    )
                    continue
            except Exception as exc:
                self._diagnostics.append(f"xlOpenDriver failed at '{libpath}': {exc}")
                continue

            try:
                config = dll.get_driver_config()
            except Exception as exc:
                self._diagnostics.append(f"xlGetDriverConfig failed: {exc}")
                try:
                    dll.close_driver()
                except Exception:
                    pass
                continue

            for idx in range(int(config.channelCount)):
                ch = config.channel[idx]
                channel_mask = int(ch.channelMask)
                if channel_mask == 0:
                    continue

                # Filter: only CAN-capable channels
                bus_caps = int(ch.channelBusCapabilities)
                if not (bus_caps & XL_BUS_ACTIVE_CAP_CAN):
                    continue

                hw_type = int(ch.hwType)
                hw_channel = int(ch.hwChannel)
                hw_index = int(ch.hwIndex)
                serial = int(ch.serialNumber)
                hw_name = _hw_type_name(hw_type)
                ch_name = _bytes_to_text(ch.name)
                transceiver_name = _bytes_to_text(ch.transceiverName)

                supports_fd = bool(
                    int(ch.channelCapabilities)
                    & (
                        XL_CHANNEL_FLAG_CANFD_BOSCH_SUPPORT
                        | XL_CHANNEL_FLAG_CANFD_ISO_SUPPORT
                    )
                )

                display_name = (
                    f"Vector {hw_name} #{hw_index}"
                    f" CH{hw_channel}"
                )
                if ch_name:
                    display_name += f" ({ch_name})"

                devices.append(
                    DeviceInfo(
                        device_id=f"vector-{serial}-{hw_index}-{hw_channel}",
                        display_name=display_name,
                        driver=self.name,
                        channel=str(hw_channel),
                        capabilities={
                            "interface": "vxlapi",
                            "vendor": "vector",
                            "registry_key": self.name,
                            "channel_mask": str(channel_mask),
                            "access_mask": str(channel_mask),
                            "hw_type_val": str(hw_type),
                            "hw_type_name": hw_name,
                            "hw_index": str(hw_index),
                            "hw_channel": str(hw_channel),
                            "channel_index": str(int(ch.channelIndex)),
                            "serial": str(serial),
                            "fd": "yes" if supports_fd else "no",
                            "libpath": libpath,
                            "ch_name": ch_name,
                            "transceiver_name": transceiver_name,
                        },
                    )
                )

            try:
                dll.close_driver()
            except Exception:
                pass

            if devices:
                return devices

        if not devices and not self._diagnostics:
            self._diagnostics.append(
                "No Vector CAN devices detected. Verify the device is connected and "
                "the Vector XL Driver Library is installed (vxlapi64.dll)."
            )
        return devices

    def create_adapter(self, config: HardwareConfig) -> BusAdapter:
        return VectorAdapter(config)
