"""Non-reaping platform probes and failure-isolated runner cleanup."""

from __future__ import annotations

import ctypes
import errno
import os
import threading
from types import SimpleNamespace

import pytest

from usagetrim.core import darwin_wait, execution_process, execution_runner
from usagetrim.core.cache import ContextCache
from usagetrim.core.execution_contract import ExecutionMode, ExecutionRequest


@pytest.mark.parametrize("missing", ["waitid", "WNOWAIT"])
def test_missing_python_wait_api_selects_darwin_adapter_without_reaping(monkeypatch, missing):
    monkeypatch.delattr(os, missing, raising=False)
    monkeypatch.setattr(execution_process, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(execution_process, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9))
    calls, signals, starts = [], [], []

    class Probe:
        def poll(self, pid):
            calls.append(pid)
            return 7

    class Process:
        def __init__(self, *args, **kwargs):
            self.pid, self.returncode = 1234567, None
            starts.append(kwargs)

        def poll(self):
            raise AssertionError("Popen.poll must not release the leader")

    monkeypatch.setattr(darwin_wait, "DarwinWaitId", Probe)
    monkeypatch.setattr(execution_process.subprocess, "Popen", Process)
    monkeypatch.setattr(os, "getpgrp", lambda: 42, raising=False)
    monkeypatch.setattr(os, "killpg", lambda *args: signals.append(args), raising=False)
    tree = execution_process.ProcessTree.__new__(execution_process.ProcessTree)
    # Choose the POSIX strategy without pretending Windows itself is macOS.
    tree.process = tree.job = tree._darwin_wait = None
    tree.released = tree.status_unavailable = False
    tree.observed_returncode = None
    tree.start(("fixture",), shell=False, cwd=None)
    assert starts[0]["start_new_session"] is True
    assert tree.poll() == 7 and tree.observed_returncode == 7
    tree.force()
    assert calls == [1234567, 1234567]
    assert signals == [(1234567, 9)]
    tree.close()


@pytest.mark.parametrize("missing", ["waitid", "WNOWAIT"])
def test_missing_wait_api_on_non_darwin_refuses_start_before_allocating_child(monkeypatch, missing):
    monkeypatch.delattr(os, missing, raising=False)
    monkeypatch.setattr(execution_process, "sys", SimpleNamespace(platform="linux"))

    def forbidden(*args, **kwargs):
        raise AssertionError("No safe non-reaping backend")

    monkeypatch.setattr(execution_process.subprocess, "Popen", forbidden)
    tree = execution_process.ProcessTree.__new__(execution_process.ProcessTree)
    tree.process = tree.job = None
    with pytest.raises(OSError, match="Non-reaping"):
        tree.start(("fixture",), shell=False, cwd=None)
    assert tree.process is None


@pytest.mark.skipif(
    ctypes.sizeof(ctypes.c_long) != 8 or ctypes.sizeof(ctypes.c_void_p) != 8,
    reason="Simulates the 64-bit Darwin ABI, never a native syscall",
)
@pytest.mark.parametrize(
    "pid,code,status,error,expected",
    [
        (0, 0, 0, 0, None),
        (123, 1, 7, 0, 7),
        (123, 2, 15, 0, -15),
        (123, 3, 6, 0, -6),
        (0, 0, 0, errno.ECHILD, ChildProcessError),
        (0, 0, 0, errno.EINTR, OSError),
        (999, 1, 0, 0, OSError),
        (123, 5, 19, 0, OSError),
    ],
)
def test_darwin_waitid_abi_options_and_status_validation(
    monkeypatch, pid, code, status, error, expected
):
    calls = []

    class NativeCall:
        def __call__(self, kind, target, pointer, options):
            calls.append((kind, target, options))
            info = ctypes.cast(pointer, ctypes.POINTER(darwin_wait._SignalInfo)).contents
            assert info.pid == 0
            info.pid, info.code, info.status = pid, code, status
            ctypes.set_errno(error)
            return -1 if error else 0

    native = NativeCall()
    monkeypatch.setattr(darwin_wait, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(waitid=native))
    probe = darwin_wait.DarwinWaitId()
    assert ctypes.sizeof(darwin_wait._SignalInfo) == 104
    if isinstance(expected, type):
        with pytest.raises(expected):
            probe.poll(123)
    else:
        assert probe.poll(123) == expected
    assert calls == [(1, 123, 37)]  # Darwin flags, never Linux flags or waitpid.


def test_darwin_loader_failure_cannot_launch_a_child(monkeypatch):
    monkeypatch.delattr(os, "waitid", raising=False)
    monkeypatch.setattr(execution_process, "sys", SimpleNamespace(platform="darwin"))

    def unavailable():
        raise RuntimeError("PRIVATE_NATIVE_INIT_ERROR")

    monkeypatch.setattr(darwin_wait, "DarwinWaitId", unavailable)
    tree = execution_process.ProcessTree.__new__(execution_process.ProcessTree)
    tree.process = tree.job = None
    with pytest.raises(RuntimeError):
        tree.start(("fixture",), shell=False, cwd=None)
    assert tree.process is None


def test_unsupported_darwin_abi_is_refused_before_loading_native_library(monkeypatch):
    sizeof = ctypes.sizeof

    def wrong_size(value):
        return 112 if value is darwin_wait._SignalInfo else sizeof(value)

    def forbidden(*args, **kwargs):
        raise AssertionError("Unsupported ABI must not reach a native call")

    monkeypatch.setattr(darwin_wait, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(ctypes, "sizeof", wrong_size)
    monkeypatch.setattr(ctypes, "CDLL", forbidden)
    with pytest.raises(OSError, match="Unsupported Darwin"):
        darwin_wait.DarwinWaitId()


@pytest.mark.parametrize("partial,broken_close", [(False, False), (True, True)])
def test_unexpected_status_failure_still_stops_readers_closes_tree_and_reaps(
    tmp_path, monkeypatch, partial, broken_close
):
    events, readers, writes = [], [], []
    read_started = {source: threading.Event() for source in ("stdout", "stderr")}
    original_reader = execution_runner._PipeReader

    class Reader(original_reader):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            readers.append(self)

        def _chunks(self):
            read_started[self.stream].set()
            yield from super()._chunks()

    class Process:
        pid = 1234567  # Synthetic PID. Native signaling is forbidden below.

        def __init__(self):
            for source in ("stdout", "stderr"):
                read, write = os.pipe()
                writes.append(write)
                setattr(self, source, os.fdopen(read, "rb", buffering=0))
            if partial:
                self.stderr.close()
                del self.stderr

        def wait(self, *, timeout):
            assert timeout >= 0
            # Writers are still open: EOF cannot be why these readers stopped.
            assert all(reader.finished.is_set() for reader in readers)
            assert not any(reader.thread.is_alive() for reader in readers)
            events.append("reap")
            return 7

    class Tree:
        def __init__(self):
            self.process = None
            self.job = object()  # Also exercise diagnostic failure before close.
            self.status_unavailable = False
            self.observed_returncode = 7

        def start(self, *args, **kwargs):
            self.process = Process()
            return self.process

        def poll(self):
            assert read_started["stdout"].wait(5)
            if not partial:
                assert read_started["stderr"].wait(5)
            raise RuntimeError("PRIVATE_MONITOR_ERROR")

        def gentle(self):
            events.append("gentle")
            raise RuntimeError("PRIVATE_NATIVE_ERROR")

        def force(self):
            events.append("force_refused")
            raise RuntimeError("Unverified status; no signal is sent")

        def alive(self):
            raise RuntimeError("PRIVATE_DIAGNOSTIC_ERROR")

        def close(self):
            events.append("close")
            if broken_close:
                raise RuntimeError("PRIVATE_CLOSE_ERROR")

    def forbidden(*args):
        raise AssertionError("A synthetic/unverified PID must never be signaled")

    monkeypatch.setattr(execution_runner, "_PipeReader", Reader)
    monkeypatch.setattr(execution_runner, "ProcessTree", Tree)
    monkeypatch.setattr(os, "killpg", forbidden, raising=False)
    try:
        request = ExecutionRequest(ExecutionMode.ARGV, argv=("synthetic",))
        with pytest.raises(execution_runner.ExecutionRunnerError) as error:
            execution_runner.run_execution(
                request,
                cache=ContextCache(tmp_path / "cache.db"),
                shutdown=execution_runner.ShutdownPolicy(force_seconds=1, reader_join_seconds=2),
            )
        assert events.index("close") < events.index("reap")
        assert "force_refused" in events
        assert read_started["stdout"].is_set()
        if not partial:
            assert read_started["stderr"].is_set()
        assert not any(reader.thread.is_alive() for reader in readers)
        categories = {issue.error_type for issue in error.value.issues}
        assert "forced_termination_failed" in categories
        assert "tree_state_unavailable" in categories
        if partial:
            assert "stderr_pipe_unavailable" in categories
        if broken_close:
            assert "tree_close_failed" in categories
        assert "PRIVATE" not in str(error.value)
    finally:
        # Test safety only; assertions above must pass before this fallback.
        for reader in readers:
            reader.stop.set()
            reader.ready.set()
        for write in writes:
            os.close(write)
        for reader in readers:
            if reader.thread.ident is not None:
                reader.thread.join(timeout=5)


def test_native_initialization_exception_releases_ready_gated_readers(tmp_path, monkeypatch):
    def unavailable():
        raise RuntimeError("PRIVATE_NATIVE_INIT_ERROR")

    monkeypatch.setattr(execution_runner, "ProcessTree", unavailable)
    request = ExecutionRequest(ExecutionMode.ARGV, argv=("synthetic",))
    with pytest.raises(execution_runner.ExecutionRunnerError) as error:
        execution_runner.run_execution(request, cache=ContextCache(tmp_path / "cache.db"))
    assert not any(
        thread.is_alive() and thread.name.startswith("usagetrim-capture-")
        for thread in threading.enumerate()
    )
    assert "PRIVATE" not in str(error.value)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group signaling")
def test_unexpected_probe_failure_does_not_authorize_a_group_signal(monkeypatch):
    tree = execution_process.ProcessTree()
    tree.process = SimpleNamespace(pid=1234567)
    signals = []

    def broken():
        raise RuntimeError("PRIVATE_MONITOR_ERROR")

    monkeypatch.setattr(tree, "poll", broken)
    monkeypatch.setattr(os, "killpg", lambda *args: signals.append(args))
    with pytest.raises(RuntimeError):
        tree.force()
    assert signals == []


def test_lost_waitability_preserves_known_status_but_never_reauthorizes_signals(monkeypatch):
    calls, signals = [], []

    class Probe:
        def poll(self, pid):
            calls.append(pid)
            if len(calls) == 1:
                return 7
            raise ChildProcessError

    tree = execution_process.ProcessTree.__new__(execution_process.ProcessTree)
    tree.process = SimpleNamespace(pid=1234567)
    tree.job = None
    tree.released = tree.status_unavailable = False
    tree.observed_returncode = None
    tree._darwin_wait = Probe()
    monkeypatch.setattr(execution_process, "signal", SimpleNamespace(SIGKILL=9))
    monkeypatch.setattr(os, "getpgrp", lambda: 42, raising=False)
    monkeypatch.setattr(os, "killpg", lambda *args: signals.append(args), raising=False)
    assert tree.poll() == 7
    with pytest.raises(OSError):
        tree.force()
    with pytest.raises(OSError):
        tree.force()
    assert tree.status_unavailable and tree.observed_returncode == 7
    assert calls == [1234567, 1234567] and signals == []
