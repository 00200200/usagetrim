"""Non-reaping Darwin waitid adapter for Python versions without os.waitid.

ABI declarations only, from Apple's public bsd/sys/signal.h and bsd/sys/wait.h:
https://github.com/apple-oss-distributions/xnu/tree/main/bsd/sys
No Linux siginfo layout or Linux wait-option values are used here.
"""

from __future__ import annotations

import ctypes
import errno
import sys


class _SignalValue(ctypes.Union):
    _fields_ = [("integer", ctypes.c_int), ("pointer", ctypes.c_void_p)]


class _SignalInfo(ctypes.Structure):
    _fields_ = [
        ("signo", ctypes.c_int),
        ("error", ctypes.c_int),
        ("code", ctypes.c_int),
        ("pid", ctypes.c_int),
        ("uid", ctypes.c_uint),
        ("status", ctypes.c_int),
        ("address", ctypes.c_void_p),
        ("value", _SignalValue),
        ("band", ctypes.c_long),
        ("reserved", ctypes.c_ulong * 7),
    ]


class DarwinWaitId:
    """Call libSystem waitid with WNOHANG | WEXITED | WNOWAIT, never waitpid."""

    def __init__(self):
        # Supported macOS Python 3.11+ builds use the 64-bit Darwin ABI.
        # Refuse a different ABI before allocating any child process.
        if (
            sys.platform != "darwin"
            or ctypes.sizeof(ctypes.c_void_p) != 8
            or ctypes.sizeof(ctypes.c_long) != 8
            or ctypes.sizeof(_SignalInfo) != 104
            or _SignalInfo.pid.offset != 12
            or _SignalInfo.status.offset != 20
        ):
            raise OSError("Unsupported Darwin waitid ABI")
        try:
            self.library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            self.waitid = self.library.waitid
        except (OSError, AttributeError):
            raise OSError("Darwin non-reaping waitid is unavailable") from None
        self.waitid.argtypes = [
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.POINTER(_SignalInfo),
            ctypes.c_int,
        ]
        self.waitid.restype = ctypes.c_int

    def poll(self, pid):
        if type(pid) is not int or pid <= 0:
            raise OSError("Invalid owned child ID")
        info = _SignalInfo()  # WNOHANG with no change leaves a zero PID.
        ctypes.set_errno(0)
        # Darwin: P_PID=1, WNOHANG=1, WEXITED=4, WNOWAIT=32.
        result = self.waitid(1, pid, ctypes.byref(info), 0x01 | 0x04 | 0x20)
        if result != 0:
            error = ctypes.get_errno()
            if error == errno.ECHILD:
                raise ChildProcessError(error, "Owned child is not waitable")
            raise OSError(error, "Darwin child status inspection failed")
        if info.pid == 0:
            return None
        if info.pid != pid or info.code not in {1, 2, 3}:
            raise OSError("Invalid Darwin child status")
        # Darwin CLD_EXITED=1; CLD_KILLED=2 and CLD_DUMPED=3.
        if info.code == 1 and 0 <= info.status <= 255:
            return info.status
        if info.code in {2, 3} and 0 < info.status < 32:
            return -info.status
        raise OSError("Invalid Darwin child exit value")
