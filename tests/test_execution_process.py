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


@pytest.fixture
def darwin_eperm_tree(monkeypatch):
    """Synthetic ownership only: never call a native API with this group's ID."""
    tree = execution_process.ProcessTree.__new__(execution_process.ProcessTree)
    tree.process = SimpleNamespace(pid=1234567, returncode=None)
    tree.job = None
    tree.released = tree.status_unavailable = False
    tree.observed_returncode = None
    tree._darwin_wait = None
    tree._darwin_group = SimpleNamespace(has_live_members=lambda pid: False)
    monkeypatch.setattr(execution_process, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(execution_process, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9))
    monkeypatch.setattr(os, "getpgrp", lambda: 42, raising=False)

    def poll():
        tree.observed_returncode = 7
        return 7

    def denied(pid, number):
        assert pid == 1234567
        raise PermissionError(errno.EPERM, "Group signal denied")

    monkeypatch.setattr(tree, "poll", poll)
    monkeypatch.setattr(os, "killpg", denied, raising=False)
    return tree


@pytest.mark.parametrize("action", ["gentle", "force", "alive"])
def test_darwin_eperm_for_verified_zombie_group_is_not_a_cleanup_failure(darwin_eperm_tree, action):
    tree = darwin_eperm_tree
    result = getattr(tree, action)()
    assert result is (False if action == "alive" else None)
    assert tree.process.returncode is None  # No Popen.poll/wait has reaped it.
    assert tree.observed_returncode == 7 and not tree.released


@pytest.mark.parametrize("action", ["force", "alive"])
@pytest.mark.parametrize("state", ["live_child", "unknown", "running_leader", "released", "lost"])
def test_darwin_eperm_is_preserved_without_dead_group_proof(
    darwin_eperm_tree, monkeypatch, action, state
):
    tree = darwin_eperm_tree
    calls = []

    def probe(pid):
        calls.append(pid)
        if state == "unknown":
            raise OSError("Group inspection denied")
        return state == "live_child"

    tree._darwin_group = SimpleNamespace(has_live_members=probe)
    if state == "running_leader":
        monkeypatch.setattr(tree, "poll", lambda: None)
    elif state == "released":
        tree.released = True
    elif state == "lost":
        tree.status_unavailable = True
    # Ownership guards refuse signals before killpg; alive's read-only EPERM
    # must also remain an error after ownership has been released/lost.
    with pytest.raises(OSError) as failure:
        getattr(tree, action)()
    if state not in {"released", "lost"} or action == "alive":
        assert isinstance(failure.value, PermissionError)
        assert failure.value.errno == errno.EPERM
    assert tree.process.returncode is None
    if state in {"released", "lost", "running_leader"}:
        assert calls == []
    else:
        assert calls == [1234567]


@pytest.mark.parametrize("platform,error", [("linux", errno.EPERM), ("darwin", errno.EACCES)])
def test_permission_failures_outside_darwin_eperm_never_use_group_exception(
    darwin_eperm_tree, monkeypatch, platform, error
):
    tree = darwin_eperm_tree
    monkeypatch.setattr(execution_process, "sys", SimpleNamespace(platform=platform))

    def forbidden(pid):
        raise AssertionError("No zombie exception is allowed here")

    def denied(*args):
        raise PermissionError(error, "Signal denied")

    tree._darwin_group = SimpleNamespace(has_live_members=forbidden)
    monkeypatch.setattr(os, "killpg", denied)
    with pytest.raises(PermissionError) as failure:
        tree.force()
    assert failure.value.errno == error


def test_darwin_lost_waitability_after_group_snapshot_cannot_hide_eperm(
    darwin_eperm_tree, monkeypatch
):
    tree = darwin_eperm_tree
    calls = []

    def poll():
        calls.append(True)
        if len(calls) == 3:  # _signal, before inspection, after inspection.
            raise OSError("Waitable ownership was lost")
        return 7

    monkeypatch.setattr(tree, "poll", poll)
    with pytest.raises(PermissionError) as failure:
        tree.force()
    assert failure.value.errno == errno.EPERM and len(calls) == 3


def test_darwin_group_probe_initialization_failure_preserves_eperm(darwin_eperm_tree, monkeypatch):
    tree = darwin_eperm_tree
    tree._darwin_group = None

    def unavailable():
        raise OSError("Darwin group inspection is unavailable")

    monkeypatch.setattr(darwin_wait, "DarwinProcessGroup", unavailable)
    with pytest.raises(PermissionError) as failure:
        tree.force()
    assert failure.value.errno == errno.EPERM
    assert tree.process.returncode is None


@pytest.fixture
def darwin_group_probe(monkeypatch):
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("Darwin process-info adapter requires a 64-bit ABI")

    def create(snapshots, statuses, *, returned_size=None):
        remaining = iter(snapshots)
        calls = []

        def listpids(kind, pgid, buffer, capacity):
            assert (kind, pgid) == (2, 123)
            calls.append("list")
            members = next(remaining)
            if members == "full":
                return capacity
            for index, pid in enumerate(members):
                buffer[index] = pid
            return len(members) * ctypes.sizeof(ctypes.c_int)

        def pidinfo(pid, flavor, argument, pointer, size):
            assert (flavor, argument, size) == (13, 1, 64)
            calls.append(pid)
            status = statuses[pid]
            if status == "denied":
                ctypes.set_errno(errno.EPERM)
                return 0
            info = ctypes.cast(pointer, ctypes.POINTER(darwin_wait._BsdShortInfo)).contents
            info.pid, info.pgid = pid, 123
            if status == "wrong_group":
                info.pgid, info.status = 999, 5
            else:
                info.status = status
            return size if returned_size is None else returned_size

        monkeypatch.setattr(darwin_wait, "sys", SimpleNamespace(platform="darwin"))
        monkeypatch.setattr(
            ctypes,
            "CDLL",
            lambda *args, **kwargs: SimpleNamespace(proc_listpids=listpids, proc_pidinfo=pidinfo),
        )
        return darwin_wait.DarwinProcessGroup(), calls

    return create


def test_darwin_group_probe_confirms_all_zombies_and_rechecks_membership(darwin_group_probe):
    probe, calls = darwin_group_probe([[123, 456], [123, 456]], {123: 5, 456: 5})
    assert probe.has_live_members(123) is False
    assert calls[0] == calls[-1] == "list" and set(calls[1:-1]) == {123, 456}


@pytest.mark.parametrize("status", [1, 2, 3, 4])
def test_darwin_group_probe_does_not_confuse_live_descendant_with_zombie_leader(
    darwin_group_probe, status
):
    probe, _ = darwin_group_probe([[123, 456]], {123: 5, 456: status})
    assert probe.has_live_members(123) is True


@pytest.mark.parametrize(
    "snapshots,statuses,size",
    [
        ([[123], [123, 456]], {123: 5}, None),
        ([[123, 456], [123]], {123: 5, 456: 5}, None),
        ([[123]], {123: "denied"}, None),
        ([[123, 456]], {123: 5, 456: "denied"}, None),
        ([[123]], {123: "wrong_group"}, None),
        ([[123]], {123: 0}, None),
        ([[123]], {123: 5}, 63),
        ([[456]], {}, None),
        ([[]], {}, None),
        (["full"], {}, None),
    ],
)
def test_darwin_group_probe_refuses_incomplete_denied_or_changing_snapshots(
    darwin_group_probe, snapshots, statuses, size
):
    probe, _ = darwin_group_probe(snapshots, statuses, returned_size=size)
    with pytest.raises(OSError):
        probe.has_live_members(123)
