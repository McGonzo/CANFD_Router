"""Use the real Python process when invoked through a Python manager alias."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import sys


def _native_process_image() -> Path:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel.GetModuleFileNameW
    function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    if not function(None, buffer, len(buffer)):
        raise ctypes.WinError(ctypes.get_last_error())
    return Path(buffer.value)


def relaunch_python_manager_alias() -> None:
    """Preserve command arguments and exit status while avoiding the alias host.

    Python manager's bin/python.exe can embed the selected runtime while
    reporting its real path via sys.executable. NI-XNET enumeration fails in
    that host even when the same runtime and DLL work in a real Python process.
    Apply only to the manager's bin/python.exe and pythonw.exe aliases,
    before GUI imports.
    """
    if sys.platform != "win32" or getattr(sys, "frozen", False):
        return
    image = _native_process_image()
    target = Path(sys.executable)
    if os.path.normcase(str(image)) == os.path.normcase(str(target)):
        return
    if image.parent.name.lower() != "bin" or image.name.lower() not in {"python.exe", "pythonw.exe"}:
        return
    if not target.is_file():
        return
    raise SystemExit(subprocess.call([str(target), *sys.orig_argv[1:]]))
