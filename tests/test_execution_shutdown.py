"""Offline shutdown tests, isolated behind a deadline-guarded process harness."""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

from usagetrim.core.execution_contract import ExecutionMode, ExecutionRequest
from usagetrim.core.execution_runner import ShutdownPolicy

FIXTURE = Path(__file__).parent / "fixtures" / "execution" / "lifecycle.py"


def _until(predicate, *, seconds=20):
    """Poll a cross-process handshake, never use elapsed time as readiness proof."""
    deadline = time.monotonic() + seconds
    gate = threading.Event()
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("fixture handshake deadline expired")
        gate.wait(0.01)


def _is_running(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes as w

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        kernel.WaitForSingleObject.restype = w.DWORD
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.CloseHandle.restype = w.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: gone
                return False
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    if sys.platform == "linux":
        try:
            # A reparented zombie is terminated, even if container PID 1 has
            # not reaped it yet. It cannot execute or hold any pipe descriptors.
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _shutdown_probe(connection, base, scenario):
    from usagetrim.core import execution_runner as runner
    from usagetrim.core.cache import ContextCache
    from usagetrim.core.execution_runner import CaptureLimits, ExecutionInterrupted

    directory = Path(base)
    if scenario == "parallel":
        _parallel_probe(connection, directory)
        return
    if scenario == "darwin_no_waitid":
        if hasattr(os, "waitid"):
            del os.waitid
    elif scenario == "darwin_no_wnowait":
        if hasattr(os, "WNOWAIT"):
            del os.WNOWAIT
    cancel = threading.Event()
    controller = None
    controller_errors = []
    cache = ContextCache(directory / "cache.db")
    original_begin = cache.begin_artifact
    original_tree = runner.ProcessTree
    trees = []
    assignment_pids = []
    if scenario == "assignment_failure":
        from usagetrim.core.execution_process import _WindowsJob

        def refused(self, pid):
            assignment_pids.append(pid)
            raise OSError("PRIVATE_JOB_ERROR")

        def must_not_resume(self, pid):
            raise AssertionError("Uncontained user code must not execute")

        _WindowsJob.assign = refused
        _WindowsJob.resume = must_not_resume

    class ObservedTree(original_tree):
        def __init__(self):
            super().__init__()
            self.resumed_after_assignment = False
            if self.job is not None:
                resume = self.job.resume

                def checked_resume(pid):
                    assert self.job.assigned
                    self.resumed_after_assignment = True
                    return resume(pid)

                self.job.resume = checked_resume
            trees.append(self)

    runner.ProcessTree = ObservedTree
    queue_full, storage_blocked, release_storage = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    mode = {
        "ignore": "ignore-term",
        "orphan": "orphan",
        "tree": "parent",
        "saturation": "flood",
        "full_queue": "flood",
        "stubborn_tree": "stubborn-parent",
        "flood_timeout": "flood",
    }.get(scenario, "wait")
    kwargs = {"timeout_seconds": 5} if scenario in {"timeout", "orphan", "flood_timeout"} else {}
    request = ExecutionRequest(
        ExecutionMode.ARGV,
        argv=(str(directory / "PRIVATE_EXEC_FAILURE"),)
        if scenario == "exec_failure"
        else (sys.executable, str(FIXTURE), mode, str(directory)),
        cwd=str(directory / "PRIVATE_CWD_FAILURE") if scenario == "cwd_failure" else None,
        **kwargs,
    )
    queue_type = runner.queue.Queue

    class ObservedQueue(queue_type):
        def _put(self, event):
            super()._put(event)
            if storage_blocked.is_set() and self._qsize() == self.maxsize:
                queue_full.set()

    class Writer:
        def __init__(self, raw):
            self.raw = raw

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def append(self, data):
            if scenario == "full_queue" and not storage_blocked.is_set():
                storage_blocked.set()
                observed = queues[0]
                with observed.mutex:
                    if observed._qsize() == observed.maxsize:
                        queue_full.set()
                assert release_storage.wait(20)
            return self.raw.append(data)

        def abort(self):
            if scenario == "sqlite_cleanup":
                raise sqlite3.OperationalError("PRIVATE_CLEANUP_ERROR")
            return self.raw.abort()

    queues = []

    def queue_factory(maxsize):
        result = ObservedQueue(maxsize)
        queues.append(result)
        return result

    def begin(*args, **kwargs):
        return Writer(original_begin(*args, **kwargs))

    cache.begin_artifact = begin
    runner.queue.Queue = queue_factory
    original_pump = runner._pump
    interrupt_sent = False

    def interrupt_pump(*args, **kwargs):
        nonlocal interrupt_sent
        if scenario == "interrupt" and not interrupt_sent:
            _until(lambda: (directory / "root.ready").exists())
            interrupt_sent = True
            raise KeyboardInterrupt
        return original_pump(*args, **kwargs)

    runner._pump = interrupt_pump

    def control():
        try:
            _until(lambda: (directory / "root.ready").exists())
            if scenario in {"tree", "orphan", "stubborn_tree"}:
                _until(lambda: (directory / "leaf.ready").exists())
            if scenario == "full_queue":
                assert storage_blocked.wait(20) and queue_full.wait(20)
            if scenario in {"normal", "darwin_no_waitid", "darwin_no_wnowait"}:
                (directory / "release").touch()
            elif scenario == "sigint":
                os.kill(os.getpid(), signal.SIGINT)
            elif scenario not in {"timeout", "orphan", "interrupt", "flood_timeout"}:
                cancel.set()
        except BaseException as error:
            controller_errors.append(type(error).__name__)
            cancel.set()
        finally:
            release_storage.set()

    try:
        if scenario not in {"assignment_failure", "exec_failure", "cwd_failure"}:
            controller = threading.Thread(target=control)
            controller.start()
        previous_handler = signal.getsignal(signal.SIGINT)
        was_interrupted = False
        try:
            result = runner.run_execution(
                request,
                cache=cache,
                cancel_event=cancel,
                limits=CaptureLimits(chunk_bytes=4096, queue_chunks=1),
                shutdown=ShutdownPolicy(graceful_seconds=0.5, force_seconds=2),
            )
        except ExecutionInterrupted as error:
            result = error.result
            was_interrupted = True
        if controller is not None:
            controller.join(timeout=25)
            assert not controller.is_alive()
        assert not controller_errors
        assert signal.getsignal(signal.SIGINT) is previous_handler
        assert not any(
            thread.is_alive() and thread.name.startswith("usagetrim-capture-")
            for thread in threading.enumerate()
        )
        for path in directory.glob("*.pid"):
            pid = int(path.read_text())
            _until(lambda: not _is_running(pid))
        summary = {
            "reason": result.termination_reason.value,
            "returncode": result.returncode,
            "complete": result.capture.complete,
            "interrupted": was_interrupted,
            "windows_job_assigned": bool(trees and trees[0].job and trees[0].job.assigned),
            "resumed_after_assignment": bool(trees and trees[0].resumed_after_assignment),
            "darwin_native_wait": bool(trees and trees[0]._darwin_wait is not None),
            "assignment_refused_safely": (
                len(assignment_pids) == 1
                and not _is_running(assignment_pids[0])
                and not (directory / "root.ready").exists()
            ),
            "full_queue": queue_full.is_set(),
            "issues": [issue.error_type for issue in result.issues],
            "streams": [
                {
                    "source": stream.stream,
                    "text": stream.capture.text,
                    "complete": stream.capture.complete,
                    "available": stream.recovery.available,
                    "recovery_complete": stream.recovery.complete,
                }
                for stream in result.streams
            ],
        }
        connection.send_bytes(json.dumps(summary).encode())
    except BaseException as error:
        connection.send_bytes(json.dumps({"failed": type(error).__name__}).encode())
    finally:
        cancel.set()
        release_storage.set()
        (directory / "release").touch()
        if controller is not None:
            controller.join(timeout=25)
        connection.close()


def _parallel_probe(connection, directory):
    from usagetrim.core.cache import ContextCache
    from usagetrim.core.execution_runner import run_execution

    cache = ContextCache(directory / "cache.db")
    cancels = [threading.Event(), threading.Event()]
    finished = [threading.Event(), threading.Event()]
    results, errors, threads = {}, [], []

    def execute(index):
        folder = directory / str(index)
        folder.mkdir()
        request = ExecutionRequest(
            ExecutionMode.ARGV,
            argv=(sys.executable, str(FIXTURE), "wait", str(folder)),
            timeout_seconds=30,
        )
        try:
            results[index] = run_execution(
                request,
                cache=cache,
                cancel_event=cancels[index],
                shutdown=ShutdownPolicy(graceful_seconds=0.5, force_seconds=2),
            )
        except BaseException as error:
            errors.append(type(error).__name__)
        finally:
            finished[index].set()

    try:
        for index in range(2):
            thread = threading.Thread(target=execute, args=(index,))
            threads.append(thread)
            thread.start()
        for index in range(2):
            _until(lambda: (directory / str(index) / "root.ready").exists())
        cancels[0].set()
        assert finished[0].wait(20)
        assert not finished[1].is_set()
        assert _is_running(int((directory / "1" / "root.pid").read_text()))
        (directory / "1" / "release").touch()
        assert finished[1].wait(20) and not errors
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
        assert not any(
            thread.is_alive() and thread.name.startswith("usagetrim-capture-")
            for thread in threading.enumerate()
        )
        refs = [stream.recovery.ref_id for result in results.values() for stream in result.streams]
        connection.send_bytes(
            json.dumps(
                {
                    "first": results[0].termination_reason.value,
                    "second": results[1].termination_reason.value,
                    "second_complete": results[1].capture.complete,
                    "distinct": len(set(refs)) == 4
                    and results[0].execution_id != results[1].execution_id,
                }
            ).encode()
        )
    except BaseException as error:
        connection.send_bytes(json.dumps({"failed": type(error).__name__}).encode())
    finally:
        for index in range(2):
            cancels[index].set()
            folder = directory / str(index)
            folder.mkdir(exist_ok=True)
            (folder / "release").touch()
        for thread in threads:
            thread.join(timeout=10)
        connection.close()


def shutdown_capture(tmp_path, scenario):
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(target=_shutdown_probe, args=(sending, str(tmp_path), scenario))
    finished_cleanly = False
    try:
        process.start()
        sending.close()
        assert receiving.poll(65), "shutdown harness exceeded its deadline"
        result = json.loads(receiving.recv_bytes(maxlength=128 * 1024))
        process.join(timeout=10)
        assert not process.is_alive() and process.exitcode == 0
        assert "failed" not in result, result
        finished_cleanly = True
        return result
    finally:
        (tmp_path / "release").touch()
        if process.pid is not None and process.is_alive():
            process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
        # Emergency test cleanup only. Each root owns a fresh isolated group;
        # Windows Job Objects are closed when the harness terminates.
        if not finished_cleanly and os.name == "posix":
            for path in tmp_path.rglob("root.pid"):
                try:
                    value = path.read_text()
                    if value.isdigit() and int(value) > 1 and int(value) != os.getpgrp():
                        os.killpg(int(value), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        receiving.close()
        sending.close()
        if process.pid is not None and not process.is_alive():
            process.close()


@pytest.mark.parametrize("scenario", ["timeout", "orphan"])
def test_timeout_includes_capture_and_descendant_held_stdout(tmp_path, scenario):
    result = shutdown_capture(tmp_path, scenario)
    assert result["reason"] == "timed_out" and not result["complete"]
    if scenario == "orphan":
        assert result["returncode"] == 0  # Direct child exited before its leaf.
    assert all(not stream["recovery_complete"] for stream in result["streams"])
    assert "ready stdout" in result["streams"][0]["text"]


def test_timeout_with_dual_stream_saturation_retains_only_incomplete_redacted_recovery(tmp_path):
    result = shutdown_capture(tmp_path, "flood_timeout")
    assert result["reason"] == "timed_out" and result["returncode"] is not None
    assert not result["complete"]
    assert all(not stream["recovery_complete"] for stream in result["streams"])
    assert "A" * 36 not in json.dumps(result)


@pytest.mark.parametrize("scenario", ["cancel", "tree", "saturation", "full_queue"])
def test_cancellation_drains_and_reaps_owned_tree_without_reader_leaks(tmp_path, scenario):
    result = shutdown_capture(tmp_path, scenario)
    assert result["reason"] == "cancelled" and not result["complete"]
    assert result["returncode"] is not None
    assert all(stream["available"] and not stream["complete"] for stream in result["streams"])
    assert "ready stdout" in result["streams"][0]["text"]
    assert "A" * 36 not in json.dumps(result)
    if scenario == "full_queue":
        assert result["full_queue"]


@pytest.mark.skipif(os.name != "posix", reason="SIGTERM escalation is POSIX-specific")
def test_sigterm_ignoring_process_is_forced_to_exit(tmp_path):
    result = shutdown_capture(tmp_path, "ignore")
    assert result["reason"] == "cancelled" and result["returncode"] == -signal.SIGKILL


@pytest.mark.skipif(os.name != "posix", reason="Descendant ignores POSIX SIGTERM")
def test_sigterm_ignoring_descendant_is_killed_even_after_leader_exits(tmp_path):
    result = shutdown_capture(tmp_path, "stubborn_tree")
    assert result["reason"] == "cancelled" and result["returncode"] == -signal.SIGTERM
    assert not result["complete"]


def test_keyboard_interrupt_is_reraised_after_cleanup_with_partial_result(tmp_path):
    result = shutdown_capture(tmp_path, "interrupt")
    assert result["interrupted"] and result["reason"] == "cancelled"
    assert not result["complete"]


@pytest.mark.skipif(os.name != "posix", reason="Actual SIGINT delivery on POSIX")
def test_actual_sigint_cleans_up_without_replacing_the_signal_handler(tmp_path):
    result = shutdown_capture(tmp_path, "sigint")
    assert result["interrupted"] and result["reason"] == "cancelled"
    assert not result["complete"]


def test_sqlite_failure_during_cleanup_does_not_leave_process_or_readers(tmp_path):
    result = shutdown_capture(tmp_path, "sqlite_cleanup")
    assert result["reason"] == "cancelled" and not result["complete"]
    assert any("abort_unavailable" in issue for issue in result["issues"])
    assert "PRIVATE_CLEANUP_ERROR" not in json.dumps(result)


def test_normal_release_remains_exited_with_complete_capture(tmp_path):
    result = shutdown_capture(tmp_path, "normal")
    assert result["reason"] == "exited" and result["returncode"] == 0
    assert result["complete"] and not result["issues"]


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires actual Darwin libSystem waitid")
@pytest.mark.parametrize("scenario", ["darwin_no_waitid", "darwin_no_wnowait"])
def test_darwin_native_adapter_runs_without_python_waitid_api(tmp_path, scenario):
    result = shutdown_capture(tmp_path, scenario)
    assert result["darwin_native_wait"]
    assert result["reason"] == "exited" and result["returncode"] == 0
    assert result["complete"] and not result["issues"]


@pytest.mark.skipif(
    sys.platform != "darwin", reason="Requires native Darwin process-group semantics"
)
@pytest.mark.parametrize("scenario", ["normal", "darwin_no_waitid"])
def test_darwin_waitable_zombie_leader_does_not_break_successful_capture(tmp_path, scenario):
    result = shutdown_capture(tmp_path, scenario)
    assert result["reason"] == "exited" and result["returncode"] == 0
    assert result["complete"] and result["issues"] == []
    assert all(
        stream["complete"] and stream["available"] and stream["recovery_complete"]
        for stream in result["streams"]
    )
    assert "ready stdout" in result["streams"][0]["text"]


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires native Darwin descendant cleanup")
@pytest.mark.parametrize("scenario", ["orphan", "stubborn_tree"])
def test_darwin_zombie_exception_does_not_skip_live_descendant_cleanup(tmp_path, scenario):
    # The existing harness waits on readiness files and verifies every recorded
    # child PID and reader is gone before returning, behind an outer deadline.
    result = shutdown_capture(tmp_path, scenario)
    assert result["reason"] == ("timed_out" if scenario == "orphan" else "cancelled")
    assert result["returncode"] == (0 if scenario == "orphan" else -signal.SIGTERM)
    assert not result["complete"]
    assert all(not stream["recovery_complete"] for stream in result["streams"])
    assert not {
        "scope_close_failed",
        "forced_termination_failed",
        "tree_exit_unverified",
        "tree_state_unavailable",
        "child_not_reaped",
    }.intersection(result["issues"])


@pytest.mark.parametrize("scenario", ["exec_failure", "cwd_failure"])
def test_real_constructor_failed_start_has_no_child_returncode_or_stale_group(tmp_path, scenario):
    result = shutdown_capture(tmp_path, scenario)
    assert result["reason"] == "failed_to_start" and result["returncode"] is None
    assert not result["complete"]
    assert "PRIVATE_EXEC_FAILURE" not in json.dumps(result)
    assert "PRIVATE_CWD_FAILURE" not in json.dumps(result)


def test_only_the_requested_parallel_execution_is_cancelled(tmp_path):
    result = shutdown_capture(tmp_path, "parallel")
    assert result["first"] == "cancelled" and result["second"] == "exited"
    assert result["second_complete"] and result["distinct"]


@pytest.mark.skipif(os.name != "nt", reason="Requires actual Windows Job Object APIs")
def test_windows_job_owns_descendants_and_is_assigned_before_resume(tmp_path):
    result = shutdown_capture(tmp_path, "tree")
    assert result["windows_job_assigned"] and result["reason"] == "cancelled"
    assert result["resumed_after_assignment"]


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows suspended process creation")
def test_windows_assignment_failure_never_runs_user_code(tmp_path):
    result = shutdown_capture(tmp_path, "assignment_failure")
    assert result["reason"] == "failed_to_start" and result["returncode"] is None
    assert result["assignment_refused_safely"]
    assert "PRIVATE_JOB_ERROR" not in json.dumps(result)


def test_shutdown_escalation_uses_monotonic_phases_and_retains_first_cause(monkeypatch):
    from usagetrim.core import execution_runner as runner

    now, signals = [10.0], []
    monkeypatch.setattr(runner.time, "monotonic", lambda: now[0])

    class Tree:
        process = object()

        def gentle(self):
            signals.append("graceful")

        def force(self):
            signals.append("forced")

    cancel = threading.Event()
    control = runner._Control(Tree(), 12.0, cancel, ShutdownPolicy(), runner._Issues())
    control.tick()
    assert signals == []
    now[0] = 12.0
    control.tick()
    assert signals == ["graceful"] and control.reason.value == "timed_out"
    cancel.set()
    now[0] = 14.0
    control.tick()
    control.tick()
    assert signals == ["graceful", "forced"]
    assert control.reason.value == "timed_out" and control.end_at == 16.0


def test_process_management_failures_are_not_silently_ignored(monkeypatch):
    from usagetrim.core import execution_runner as runner

    now = [0.0]
    monkeypatch.setattr(runner.time, "monotonic", lambda: now[0])

    class RefusedTree:
        process = object()

        def gentle(self):
            raise PermissionError("PRIVATE_CLEANUP_ERROR")

        force = gentle
        alive = gentle

    issues = runner._Issues()
    control = runner._Control(RefusedTree(), None, None, ShutdownPolicy(), issues)
    control.begin(runner.TerminationReason.CANCELLED)
    now[0] = 2.0
    control.tick()
    assert control.alive()
    assert {issue.error_type for issue in issues.items.values()} == {
        "graceful_termination_failed",
        "forced_termination_failed",
        "tree_state_unavailable",
    }
    assert "PRIVATE_CLEANUP_ERROR" not in repr(tuple(issues.items.values()))


@pytest.mark.skipif(os.name != "posix", reason="POSIX waitid/PID ownership semantics")
def test_group_signals_are_refused_if_the_child_is_no_longer_waitable(monkeypatch):
    from usagetrim.core.execution_process import ProcessTree

    tree = ProcessTree()
    tree.process = type("Process", (), {"pid": os.getpid() + 1000000})()
    signals = []

    def no_child(*args):
        raise ChildProcessError

    if callable(getattr(os, "waitid", None)) and hasattr(os, "WNOWAIT"):
        monkeypatch.setattr(os, "waitid", no_child)
    else:
        # Older macOS Python builds use the native adapter, not os.waitid.
        tree._darwin_wait = type("Probe", (), {"poll": staticmethod(no_child)})()
    monkeypatch.setattr(os, "killpg", lambda *args: signals.append(args))
    with pytest.raises(OSError, match="status is unavailable"):
        tree.force()
    assert tree.status_unavailable and signals == []


@pytest.mark.skipif(os.name != "posix", reason="Uses the POSIX launch branch")
def test_interrupted_constructor_retains_allocated_process_ownership(monkeypatch):
    from usagetrim.core import execution_process

    class InterruptedProcess:
        def __init__(self, *args, **kwargs):
            self.pid = 1234567  # Synthetic; this test never signals or launches a child.
            raise KeyboardInterrupt

    monkeypatch.setattr(execution_process.subprocess, "Popen", InterruptedProcess)
    tree = execution_process.ProcessTree()
    with pytest.raises(KeyboardInterrupt):
        tree.start(("tool",), shell=False, cwd=None)
    assert isinstance(tree.process, InterruptedProcess) and tree.process.pid == 1234567
    tree.close()


@pytest.mark.skipif(os.name != "posix", reason="Uses the POSIX launch branch")
def test_reaped_exec_failure_does_not_retain_a_pid_for_group_signaling(monkeypatch):
    from usagetrim.core import execution_process

    class ReapedFailure:
        def __init__(self, *args, **kwargs):
            self.pid = 1234567
            self.returncode = 255
            raise FileNotFoundError("PRIVATE_EXEC_ERROR")

    monkeypatch.setattr(execution_process.subprocess, "Popen", ReapedFailure)
    tree = execution_process.ProcessTree()
    with pytest.raises(FileNotFoundError):
        tree.start(("tool",), shell=False, cwd=None)
    assert tree.process is None
    tree.close()


@pytest.mark.parametrize("cause", ["cancel", "deadline"])
def test_request_stopped_during_storage_setup_never_launches_child(tmp_path, monkeypatch, cause):
    from usagetrim.core import execution_runner as runner
    from usagetrim.core.cache import ContextCache

    cache = ContextCache(tmp_path / "cache.db")
    begin = cache.begin_artifact
    now = [0.0]
    cancel = threading.Event()
    monkeypatch.setattr(runner.time, "monotonic", lambda: now[0])

    def begin_and_stop(*args, **kwargs):
        writer = begin(*args, **kwargs)
        if cause == "cancel":
            cancel.set()
        else:
            now[0] = 10.0
        return writer

    def forbidden(*args, **kwargs):
        raise AssertionError("Stopped request must not launch a child")

    monkeypatch.setattr(cache, "begin_artifact", begin_and_stop)
    monkeypatch.setattr(runner.subprocess, "Popen", forbidden)
    request = ExecutionRequest(ExecutionMode.ARGV, argv=(sys.executable,), timeout_seconds=5)
    result = runner.run_execution(request, cache=cache, cancel_event=cancel)
    assert result.termination_reason.value == ("cancelled" if cause == "cancel" else "timed_out")
    assert result.returncode is None and not result.capture.complete
    assert all(not stream.recovery.complete for stream in result.streams)


@pytest.mark.parametrize("duration", [0, -1, True, float("inf"), float("nan"), 61])
def test_shutdown_policy_requires_finite_bounded_phases(duration):
    with pytest.raises(ValueError):
        ShutdownPolicy(graceful_seconds=duration)
