"""Owned process trees and interruptible pipe polling; internal runner support.

Windows uses documented Win32 APIs only. No child code runs before assignment
to a kill-on-close Job Object. Imports do not load Windows DLLs on POSIX.
"""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys


class ProcessTree:
    def __init__(self):
        self.process = None
        self.job = None
        self.released = False
        self.status_unavailable = False
        self.observed_returncode = None
        self._darwin_wait = None
        self._darwin_group = None
        if os.name == "nt":
            self.job = _WindowsJob()

    def start(self, command, *, shell, cwd):
        if self.job is None and not (
            callable(getattr(os, "waitid", None)) and hasattr(os, "WNOWAIT")
        ):
            if sys.platform != "darwin":
                raise OSError("Non-reaping child status inspection is required")
            from usagetrim.core.darwin_wait import DarwinWaitId

            self._darwin_wait = DarwinWaitId()
        options = (
            {"creationflags": 0x00000004 | subprocess.CREATE_NEW_PROCESS_GROUP}
            if self.job is not None
            else {"start_new_session": True}
        )
        options.update(
            shell=shell, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )
        factory = subprocess.Popen
        if isinstance(factory, type):
            # Retain the object before __init__: KeyboardInterrupt can arrive
            # after a PID was allocated but before Popen's constructor returns.
            self.process = factory.__new__(factory)
            try:
                factory.__init__(self.process, command, **options)
            except BaseException:
                # Popen reaps exec/cwd failures itself. A retained, already
                # reaped PID is not permission to signal a process group.
                if (
                    getattr(self.process, "pid", None) is None
                    or getattr(self.process, "returncode", None) is not None
                ):
                    self.process = None
                raise
        else:
            # Keep ordinary Popen factory injection available to capture tests.
            self.process = factory(command, **options)
        if self.job is not None:
            # CREATE_SUSPENDED avoids the spawn-before-assignment escape race.
            self.job.assign(self.process.pid)
            self.job.resume(self.process.pid)
        return self.process

    def gentle(self):
        if self.job is not None:
            self.job.gentle(self.process.pid)
        else:
            self._signal(signal.SIGTERM)

    def force(self):
        if self.job is not None:
            # Also cover a still-suspended process if assignment failed.
            if self.process is not None and not self.job.assigned:
                self.process.kill()
            self.job.force()
        else:
            self._signal(signal.SIGKILL)

    def _signal(self, number):
        pid = self.process.pid
        if (
            self.released
            or self.status_unavailable
            or type(pid) is not int
            or pid <= 1
            or pid == os.getpgrp()
        ):
            raise OSError("Invalid owned process group")
        # Keep a waitable leader (including a zombie) until the last group
        # signal. Reaping it earlier can permit process-group ID reuse.
        self.poll()
        try:
            os.killpg(pid, number)
        except ProcessLookupError:
            pass
        except PermissionError as error:
            if error.errno != errno.EPERM or not self._darwin_group_exited():
                raise

    def _darwin_group_exited(self):
        """Only dismiss EPERM with a waitable leader and a verified dead group."""
        if (
            sys.platform != "darwin"
            or self.released
            or self.status_unavailable
            or self.process is None
        ):
            return False
        try:
            # An exited leader alone is insufficient: descendants may still run.
            if self.poll() is None:
                return False
            probe = getattr(self, "_darwin_group", None)
            if probe is None:
                from usagetrim.core.darwin_wait import DarwinProcessGroup

                self._darwin_group = probe = DarwinProcessGroup()
            if probe.has_live_members(self.process.pid):
                return False
            # Do not accept a snapshot after an external reaper released our ID.
            return self.poll() is not None
        except OSError:
            # An unavailable native probe is not evidence that EPERM is harmless.
            return False

    def poll(self):
        try:
            if self.job is not None:
                result = self.process.poll()
            elif self._darwin_wait is not None:
                result = self._darwin_wait.poll(self.process.pid)
            else:
                status = os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                result = (
                    None
                    if status is None
                    else status.si_status
                    if status.si_code == os.CLD_EXITED
                    else -status.si_status
                )
        except ChildProcessError:
            # An external SIGCHLD handler must not make us signal a reused ID
            # or accept Popen's ECHILD-derived zero as an observed exit status.
            self.status_unavailable = True
            raise OSError("Owned child status is unavailable") from None
        if result is not None:
            self.observed_returncode = result
        return result

    def alive(self):
        if self.process is None:
            return False
        if self.job is not None:
            return self.job.alive() or (not self.job.assigned and self.process.poll() is None)
        try:
            os.killpg(self.process.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError as error:
            if error.errno == errno.EPERM and self._darwin_group_exited():
                return False
            raise

    def close(self):
        self.released = True
        if self.job is not None:
            self.job.close()


def pipe_available(pipe):
    """Windows only: available bytes, or -1 for a closed pipe. Never waits."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    peek = kernel.PeekNamedPipe
    peek.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    peek.restype = wintypes.BOOL
    available = wintypes.DWORD()
    if not peek(msvcrt.get_osfhandle(pipe.fileno()), None, 0, None, ctypes.byref(available), None):
        error = ctypes.get_last_error()
        if error in {109, 232}:  # ERROR_BROKEN_PIPE / ERROR_NO_DATA
            return -1
        raise ctypes.WinError(error)
    return available.value


class _WindowsJob:
    """Small, owned Win32 handle adapter; no fallback to an uncontained child."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes as w

        self.ctypes, self.w = ctypes, w
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.handle = None
        self.assigned = False
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            "QueryInformationJobObject": (
                [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p],
                w.BOOL,
            ),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "CreateToolhelp32Snapshot": ([w.DWORD, w.DWORD], w.HANDLE),
            "Thread32First": ([w.HANDLE, ctypes.c_void_p], w.BOOL),
            "Thread32Next": ([w.HANDLE, ctypes.c_void_p], w.BOOL),
            "OpenThread": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "ResumeThread": ([w.HANDLE], w.DWORD),
            "GenerateConsoleCtrlEvent": ([w.DWORD, w.DWORD], w.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("per_process", ctypes.c_int64),
                ("per_job", ctypes.c_int64),
                ("flags", w.DWORD),
                ("minimum", ctypes.c_size_t),
                ("maximum", ctypes.c_size_t),
                ("active", w.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", w.DWORD),
                ("scheduling", w.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits),
                ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            if not self.kernel.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def assign(self, pid):
        # PROCESS_TERMINATE | PROCESS_SET_QUOTA: rights required by assignment.
        process = self.kernel.OpenProcess(0x0001 | 0x0100, False, pid)
        if not process:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, process):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            self.assigned = True
        finally:
            self._close_handle(process)

    def resume(self, pid):
        ctypes, w = self.ctypes, self.w

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("size", w.DWORD),
                ("usage", w.DWORD),
                ("thread_id", w.DWORD),
                ("owner", w.DWORD),
                ("priority", w.LONG),
                ("delta", w.LONG),
                ("flags", w.DWORD),
            ]

        snapshot = self.kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        thread_id = None
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            more = self.kernel.Thread32First(snapshot, ctypes.byref(entry))
            if not more:
                raise ctypes.WinError(ctypes.get_last_error())
            while more:
                if entry.owner == pid:
                    if thread_id is not None:
                        raise OSError("Ambiguous suspended process threads")
                    thread_id = entry.thread_id
                more = self.kernel.Thread32Next(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._close_handle(snapshot)
        if thread_id is None:
            raise OSError("Suspended process thread not found")
        thread = self.kernel.OpenThread(0x0002, False, thread_id)  # THREAD_SUSPEND_RESUME
        if not thread:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if self.kernel.ResumeThread(thread) != 1:
                raise OSError("Unexpected suspended process state")
        finally:
            self._close_handle(thread)

    def gentle(self, pid):
        # Requires a shared console. A GUI/no-console host may reject this;
        # the runner reports that failure and still performs forced job cleanup.
        if not self.kernel.GenerateConsoleCtrlEvent(1, pid):  # CTRL_BREAK_EVENT
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def force(self):
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def alive(self):
        ctypes, w = self.ctypes, self.w

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("times", ctypes.c_int64 * 4),
                ("faults", w.DWORD),
                ("total", w.DWORD),
                ("active", w.DWORD),
                ("terminated", w.DWORD),
            ]

        information = Accounting()
        if not self.kernel.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(information), ctypes.sizeof(information), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return information.active != 0

    def _close_handle(self, handle):
        if not self.kernel.CloseHandle(handle):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def close(self):
        if self.handle is not None:
            self._close_handle(self.handle)
            self.handle = None
