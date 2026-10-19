"""Run the configured project shim in external Python, outside Cura's DLL context.

Invoked with Python -I. This script changes only the child process; Cura's
process-global DLL search path and environment are never changed by the adapter.
"""
from __future__ import annotations

import os
import runpy
import sys


def main() -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.SetDllDirectoryW.argtypes = (wintypes.LPCWSTR,)
        kernel.SetDllDirectoryW.restype = wintypes.BOOL
        if not kernel.SetDllDirectoryW(None):
            raise ctypes.WinError(ctypes.get_last_error())
    if len(sys.argv) < 2:
        raise SystemExit("A configured project launcher is required.")
    sys.argv = sys.argv[1:]
    runpy.run_path(sys.argv[0], run_name="__main__")


if __name__ == "__main__":
    main()
