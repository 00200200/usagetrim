"""Read-only Darwin process probes; no child status is reaped here.

ABI declarations only, from Apple's public bsd/sys/signal.h and bsd/sys/wait.h:
https://github.com/apple-oss-distributions/xnu/tree/main/bsd/sys
No Linux siginfo layout or Linux wait-option values are used here.
Group inspection uses libproc.h/sys/proc_info.h's proc_listpids/PROC_PIDT_SHORTBSDINFO.
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


class _BsdShortInfo(ctypes.Structure):
    # Public struct proc_bsdshortinfo, not the private kinfo_proc layout.
    _fields_ = [
        ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16),
        ("flags", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32),
        ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
    ]


class DarwinProcessGroup:
    """Bounded group membership/status snapshot, used only for ambiguous EPERM.

    The caller must retain the waitable leader throughout. A full buffer, missing
    leader, changing membership or denied status inspection is inconclusive.
    Only an entirely verified zombie group can be declared non-running.
    """

    _MAX_MEMBERS = 4096

    def __init__(self):
        if (
            sys.platform != "darwin"
            or ctypes.sizeof(ctypes.c_void_p) != 8
            or ctypes.sizeof(ctypes.c_int) != 4
            or ctypes.sizeof(_BsdShortInfo) != 64
            or _BsdShortInfo.status.offset != 12
            or _BsdShortInfo.pid.offset != 0
            or _BsdShortInfo.pgid.offset != 8
        ):
            raise OSError("Unsupported Darwin process-info ABI")
        try:
            self.library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            self.listpids = self.library.proc_listpids
            self.pidinfo = self.library.proc_pidinfo
        except (OSError, AttributeError):
            raise OSError("Darwin group inspection is unavailable") from None
        self.listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
        self.listpids.restype = ctypes.c_int
        self.pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self.pidinfo.restype = ctypes.c_int

    def _members(self, pgid):
        members = (ctypes.c_int * self._MAX_MEMBERS)()
        capacity = ctypes.sizeof(members)
        ctypes.set_errno(0)
        # PROC_PGRP_ONLY=2. Return value is bytes, not a count of PIDs.
        used = self.listpids(2, pgid, members, capacity)
        if used <= 0 or used >= capacity or used % ctypes.sizeof(ctypes.c_int):
            raise OSError("Darwin group snapshot is incomplete")
        pids = set(members[: used // ctypes.sizeof(ctypes.c_int)])
        if pgid not in pids or any(pid <= 0 for pid in pids):
            raise OSError("Darwin group leader could not be verified")
        return pids

    def has_live_members(self, pgid):
        if type(pgid) is not int or not 1 < pgid <= 0x7FFFFFFF:
            raise OSError("Invalid owned process group")
        pids = self._members(pgid)
        for pid in pids:
            info = _BsdShortInfo()
            ctypes.set_errno(0)
            # PROC_PIDT_SHORTBSDINFO=13; arg=1 explicitly includes zombies.
            # The short form reports PGID even after a process lost its session.
            received = self.pidinfo(pid, 13, 1, ctypes.byref(info), ctypes.sizeof(info))
            if (
                received != ctypes.sizeof(info)
                or info.pid != pid
                or info.pgid != pgid
                or info.status not in {1, 2, 3, 4, 5}
            ):
                raise OSError("Darwin group member status is unavailable")
            # Darwin SZOMB=5. Running, sleeping, stopped and initializing tasks
            # all count as live; do not infer liveness from signal permissions.
            if info.status != 5:
                return True
        # A member could have forked and exited between enumeration and status
        # inspection. After all observed members are zombies, enumerate again;
        # a newly listed child or any disappearance makes the proof inconclusive.
        if self._members(pgid) != pids:
            raise OSError("Darwin group membership changed during inspection")
        return False
