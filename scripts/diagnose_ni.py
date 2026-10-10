"""Read-only NI enumeration and Python launch diagnostics; never opens a CAN bus."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def package_identity() -> str:
    if sys.platform != "win32":
        return "not Windows"
    function = ctypes.WinDLL("kernel32").GetCurrentPackageFullName
    function.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_wchar_p]
    function.restype = ctypes.c_long
    length = ctypes.c_uint32(0)
    status = function(ctypes.byref(length), None)
    if status == 15700:  # APPMODEL_ERROR_NO_PACKAGE
        return "unpackaged"
    if status != 122:  # ERROR_INSUFFICIENT_BUFFER
        return f"status {status}"
    buffer = ctypes.create_unicode_buffer(length.value)
    status = function(ctypes.byref(length), buffer)
    return buffer.value if status == 0 else f"status {status}"


def main() -> None:
    if "--native-python" in sys.argv:
        from can_gateway.utils.python_launch import relaunch_python_manager_alias
        relaunch_python_manager_alias()
    import can_gateway.drivers.ni_driver as ni
    from can_gateway.services.device_registry import DeviceRegistry

    report = {
        "python": sys.executable,
        "version": sys.version,
        "prefix": sys.prefix,
        "working_directory": os.getcwd(),
        "project_main": str(PROJECT_ROOT / "main.py"),
        "ni_module": ni.__file__,
        "nixnet_package": getattr(ni.nixnet, "__file__", None),
        "package_identity": package_identity(),
        "is_admin": bool(ctypes.windll.shell32.IsUserAnAdmin()) if sys.platform == "win32" else None,
    }
    if sys.platform == "win32":
        kernel = ctypes.WinDLL("kernel32")
        kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel.GetModuleHandleW.restype = ctypes.c_void_p
        kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
        kernel.GetModuleFileNameW.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        kernel.GetModuleFileNameW(None, buffer, len(buffer))
        report["native_process_image"] = buffer.value
    report["launch_environment"] = {
        name: os.environ.get(name) for name in (
            "PYTHONHOME", "PYTHONPATH", "__PYVENV_LAUNCHER__",
            "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "USERPROFILE", "TEMP", "TMP",
        )
    }
    driver = ni.NIDriver()
    devices = driver.scan_devices()
    report["ni_devices"] = [item.channel for item in devices]
    report["ni_diagnostics"] = driver.diagnostics()
    devices, diagnostics = DeviceRegistry().scan_all_report()
    report["registry_ni_devices"] = [item.channel for item in devices if item.driver == "ni"]
    report["registry_ni_diagnostics"] = [item for item in diagnostics if item.startswith("ni:")]
    if sys.platform == "win32":
        handle = kernel.GetModuleHandleW("nixnet.dll")
        if handle:
            kernel.GetModuleFileNameW(handle, buffer, len(buffer))
            report["native_nixnet_dll"] = buffer.value
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
