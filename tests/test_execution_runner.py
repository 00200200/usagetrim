"""Real command capture in a disposable, deadline-guarded test harness.

The harness sends only bounded result summaries, never child logs in full. Its
termination fallback protects CI from regressions; it is not runner cancellation.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from usagetrim.core.execution_contract import ExecutionMode, ExecutionRequest
from usagetrim.core.execution_runner import CaptureLimits, run_execution

PRODUCER = Path(__file__).parent / "fixtures" / "execution" / "producer.py"
_SECRET = "ghp_" + "A" * 36
_MARKERS = tuple(
    value.encode()
    for value in (
        "A" * 36,
        "B" * 48,
        "C" * 48,
        "D" * 48,
        "E" * 48,
        "G" * 48,
        "AKIA" + "F" * 16,
        "eyJhbGciOiJIUzI1NiJ9",
    )
)


def _has_secret(parts):
    tail = b""
    for part in parts:
        window = tail + part
        if any(marker in window for marker in _MARKERS):
            return True
        tail = window[-max(map(len, _MARKERS)) :]
    return False


def _request(*arguments):
    return ExecutionRequest(ExecutionMode.ARGV, argv=(sys.executable, str(PRODUCER), *arguments))


def _probe(connection, request, db_path, limit_options, fault):
    """Isolate real pipes/threads and bound diagnostics even if a regression hangs."""
    from usagetrim.core import execution_runner as runner
    from usagetrim.core.cache import ContextCache

    release, blocked, full = threading.Event(), threading.Event(), threading.Event()
    controller = None
    try:
        os.environ["USAGETRIM_CACHE_MAX_BYTES"] = str(64 * 1024 * 1024)
        limits = runner.CaptureLimits(**limit_options)
        cache = ContextCache(Path(db_path))
        original_queue = runner.queue.Queue
        original_popen = runner.subprocess.Popen
        holder = {}
        read_requests = []
        active_readers = []
        dequeued = hashlib.sha256()
        received = {"stdout": 0, "stderr": 0}

        class ObservedQueue(original_queue):
            def __init__(self, maxsize):
                super().__init__(maxsize=maxsize)
                holder["queue"] = self
                self.peak = self.maximum_payload = 0

            def _put(self, event):
                super()._put(event)
                self.peak = max(self.peak, self._qsize())
                self.maximum_payload = max(self.maximum_payload, len(event.data))
                if blocked.is_set() and self._qsize() == self.maxsize:
                    full.set()

            def _get(self):
                event = super()._get()
                if not event.terminal:
                    received[event.stream] += len(event.data)
                    dequeued.update(f"{event.stream}:{len(event.data)};".encode())
                return event

        class LimitedPipe:
            def __init__(self, raw):
                self.raw = raw

            def read(self, size):
                assert type(size) is int and 1 <= size <= limits.chunk_bytes
                # Fixed-size counters rather than an accumulated list of reads.
                if not read_requests:
                    read_requests.append(size)
                else:
                    read_requests[0] = max(read_requests[0], size)
                return self.raw.read(size)

            def close(self):
                self.raw.close()

            def fileno(self):
                return self.raw.fileno()

        def popen(*args, **kwargs):
            assert kwargs["stdout"] == subprocess.PIPE
            assert kwargs["stderr"] == subprocess.PIPE
            assert kwargs["bufsize"] == 0 and "env" not in kwargs and "stdin" not in kwargs
            process = original_popen(*args, **kwargs)
            process.stdout = LimitedPipe(process.stdout)
            process.stderr = LimitedPipe(process.stderr)
            return process

        def forbidden(*args, **kwargs):
            raise AssertionError("Whole-output capture or network access attempted")

        import socket

        socket.create_connection = forbidden
        socket.socket.connect = forbidden
        runner.queue.Queue = ObservedQueue
        runner.subprocess.Popen = popen
        original_popen.communicate = forbidden
        runner.subprocess.run = forbidden
        cache.retrieve = forbidden
        cache.store = forbidden
        original_begin = cache.begin_artifact
        from usagetrim.core import execution_artifacts as artifacts

        original_connect = artifacts._connect
        inject_write = injected = False

        @contextmanager
        def failing_connect(*args, **kwargs):
            nonlocal injected
            if inject_write and not injected and kwargs.get("write"):
                injected = True
                messages = {
                    "sqlite_busy": "database is busy",
                    "sqlite_locked": "database is locked",
                    "sqlite_io": "disk I/O error",
                    "disk_full": "database or disk is full",
                    "finish": "disk I/O error",
                }
                raise sqlite3.OperationalError(messages.get(fault, "PRIVATE_STORAGE_ERROR"))
            with original_connect(*args, **kwargs) as connection:
                yield connection

        artifacts._connect = failing_connect
        if fault == "reader":
            original_chunks = runner._PipeReader._chunks

            def broken_chunks(reader):
                for data in original_chunks(reader):
                    yield data
                    if reader.stream == "stdout":
                        raise OSError("PRIVATE_READER_ERROR")

            runner._PipeReader._chunks = broken_chunks

        class Writer:
            def __init__(self, raw):
                self.raw, self.calls = raw, 0

            def __getattr__(self, name):
                return getattr(self.raw, name)

            def append(self, data):
                nonlocal inject_write
                self.calls += 1
                if fault == "slow" and self.raw.stream.value == "stdout" and self.calls == 1:
                    blocked.set()
                    if not release.wait(timeout=10):
                        raise RuntimeError("test storage gate was not released")
                if self.raw.stream.value == "stdout" and self.calls == 2:
                    if fault in {"sqlite_busy", "sqlite_locked", "sqlite_io", "disk_full"}:
                        inject_write = True
                    if fault == "sqlite":
                        raise sqlite3.OperationalError("PRIVATE_STORAGE_ERROR")
                    if fault == "permission":
                        raise PermissionError("PRIVATE_STORAGE_ERROR")
                return self.raw.append(data)

            def finish(self):
                nonlocal inject_write
                if fault == "finish" and self.raw.stream.value == "stdout":
                    inject_write = True
                return self.raw.finish()

        def begin(*args, **kwargs):
            if fault == "startup_storage":
                raise PermissionError("PRIVATE_STORAGE_ERROR")
            return Writer(original_begin(*args, **kwargs))

        cache.begin_artifact = begin
        if fault == "missing_recovery":
            original_recovery = cache.execution_recovery

            def recovery(execution_id):
                cache.clear()
                return original_recovery(execution_id)

            cache.execution_recovery = recovery
        if fault == "slow":

            def unblock():
                try:
                    if blocked.wait(timeout=10):
                        observed = holder["queue"]
                        # Close the race where the queue filled just before the
                        # writer announced that it was blocked.
                        with observed.mutex:
                            if observed._qsize() == observed.maxsize:
                                full.set()
                        full.wait(timeout=10)
                finally:
                    release.set()

            controller = threading.Thread(target=unblock)
            controller.start()
        result = runner.run_execution(request, cache=cache, limits=limits)
        if fault == "evict_after":
            cache.clear()
        elif fault == "corrupt_after":
            with sqlite3.connect(cache.db_path) as corrupt_connection:
                corrupt_connection.execute(
                    "UPDATE execution_chunks SET data=? WHERE (ref_id,sequence)=("
                    "SELECT ref_id,sequence FROM execution_chunks WHERE ref_id IN ("
                    "SELECT ref_id FROM execution_artifacts WHERE stream='stdout') "
                    "ORDER BY sequence DESC LIMIT 1)",
                    (b"tampered\n",),
                )
        release.set()
        if controller is not None:
            controller.join()
        active_readers = [
            thread.name
            for thread in threading.enumerate()
            if thread.name.startswith("usagetrim-capture-") and thread.is_alive()
        ]
        views = {}
        for stream in result.streams:
            text = stream.capture.text
            over_limit = len(text) > limits.preview_bytes
            byte_length = None if over_limit else len(text.encode("utf-8"))
            views[stream.stream] = {
                "text": "" if over_limit else text,
                "preview_over_limit": over_limit,
                "preview_bytes": byte_length,
                "complete": stream.capture.complete,
                "scope": stream.capture.text_scope,
                "truncated": stream.preview_truncated,
                "ref": stream.recovery.ref_id if stream.recovery else None,
                "available": stream.recovery.available if stream.recovery else None,
                "recovery_complete": stream.recovery.complete if stream.recovery else None,
            }
        stored = {}
        secrets_found = False
        persisted = hashlib.sha256()
        for item in cache.iter_execution_observations(result.execution_id):
            persisted.update(f"{item.stream.value}:{item.input_bytes};".encode())
        report = cache.execution_recovery(result.execution_id)
        for item in report.streams:
            digest, byte_count, fragments = hashlib.sha256(), 0, 0
            origin_matches = True
            iter_error = None
            tail = b""
            try:
                for fragment in cache.iter_artifact(item.ref_id):
                    assert fragment.sequence == fragments
                    assert len(fragment.data) <= 4096
                    origin_matches &= (
                        fragment.stream == item.stream
                        and fragment.execution_id == result.execution_id
                    )
                    digest.update(fragment.data)
                    secrets_found |= _has_secret((tail, fragment.data))
                    tail = fragment.data[-max(map(len, _MARKERS)) :]
                    byte_count += len(fragment.data)
                    fragments += 1
            except artifacts.ArtifactError as error:
                iter_error = error.code
            stored[item.stream.value] = {
                "bytes": item.stored_bytes,
                "complete": item.complete,
                "sha256": digest.hexdigest(),
                "iterated_bytes": byte_count,
                "fragments": fragments,
                "origin_matches": origin_matches,
                "iter_error": iter_error,
                "integrity_verified": bool(
                    item.available
                    and iter_error is None
                    and byte_count == item.stored_bytes
                    and fragments == item.chunk_count
                    and digest.hexdigest() == item.content_hash
                ),
            }
        for path in Path(db_path).parent.glob("cache.db*"):
            try:
                with path.open("rb") as database:
                    secrets_found |= _has_secret(iter(lambda: database.read(8192), b""))
            except FileNotFoundError:
                pass  # SQLite may remove a journal between enumeration and open.
        summary = {
            "reason": result.termination_reason.value,
            "returncode": result.returncode,
            "complete": result.capture.complete,
            "layout": result.capture.stream_layout,
            "root_text": result.capture.text,
            "views": views,
            "stored": stored,
            "secrets_absent_from_storage": not secrets_found,
            "current_recovery_complete": bool(
                report.recorded
                and len(report.streams) == 2
                and all(item.available and item.complete for item in report.streams)
                and all(item["integrity_verified"] for item in stored.values())
            ),
            "issues": [
                {"phase": i.phase, "type": i.error_type, "message": i.message}
                for i in result.issues
            ],
            "queue_peak": holder["queue"].peak,
            "maximum_payload": holder["queue"].maximum_payload,
            "maximum_read": read_requests[0] if read_requests else 0,
            "observed_bytes": received,
            "order_preserved": persisted.digest() == dequeued.digest(),
            "identities_match": (
                len(report.streams) == 2
                and len({item.ref_id for item in report.streams}) == 2
                and all(item.execution_id == result.execution_id for item in report.streams)
                and {item.stream.value for item in report.streams} == {"stdout", "stderr"}
            ),
            "backpressure": full.is_set(),
            "live_readers": len(active_readers),
            "repr_redacted": _SECRET not in repr(result),
            "elapsed": result.elapsed_seconds,
        }
        encoded = json.dumps(summary, ensure_ascii=False).encode("utf-8")
        assert len(encoded) <= 256 * 1024
        connection.send_bytes(encoded)
    except BaseException as error:
        # Do not expose argv, exception bodies, raw output or tracebacks.
        connection.send_bytes(json.dumps({"probe_failed": type(error).__name__}).encode())
    finally:
        release.set()
        if controller is not None:
            controller.join()
        connection.close()


def capture(request, tmp_path, *, fault=None, **limits):
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    harness = context.Process(
        target=_probe,
        args=(sending, request, str(tmp_path / "cache.db"), limits, fault),
    )
    try:
        harness.start()
        sending.close()
        assert receiving.poll(60), "bounded capture harness did not finish"
        summary = json.loads(receiving.recv_bytes(maxlength=256 * 1024))
        harness.join(timeout=10)
        assert not harness.is_alive(), "capture harness retained a thread or process"
        assert harness.exitcode == 0 and not summary.get("probe_failed"), summary
        assert summary["live_readers"] == 0
        return summary
    finally:
        receiving.close()
        sending.close()
        if harness.pid is not None and harness.is_alive():
            # Test harness safety only; the implementation never kills a process.
            harness.terminate()
            harness.join(timeout=5)
            if harness.is_alive():
                harness.kill()
                harness.join(timeout=5)
        if harness.pid is not None and not harness.is_alive():
            harness.close()


@pytest.mark.parametrize(
    "mode,stdout,stderr",
    [
        ("empty", "", ""),
        ("stdout", "payload\n", ""),
        ("stderr", "", "payload\n"),
        ("mixed", "out1\nout2\n", "err1\n"),
        ("invalid-utf8", "bad:��\n", ""),
        ("ansi", "red\n", ""),
        ("crlf", "one\ntwo\n", ""),
    ],
)
def test_real_capture_modes(mode, stdout, stderr, tmp_path):
    result = capture(_request("--mode", mode), tmp_path, chunk_bytes=7, queue_chunks=2)
    assert result["reason"] == "exited" and result["returncode"] == 0
    assert result["views"]["stdout"]["text"] == stdout
    assert result["views"]["stderr"]["text"] == stderr
    assert result["complete"] and result["order_preserved"]
    assert result["identities_match"]
    assert result["layout"] == "separate" and result["root_text"] == ""
    assert not result["issues"]
    assert all(view["recovery_complete"] for view in result["views"].values())
    for source, expected in (("stdout", stdout), ("stderr", stderr)):
        stored = result["stored"][source]
        assert stored["sha256"] == hashlib.sha256(expected.encode()).hexdigest()
        assert stored["origin_matches"] and stored["iterated_bytes"] == len(expected.encode())
        assert stored["integrity_verified"]


def test_no_newline_nonzero_and_literal_error_text(tmp_path):
    result = capture(
        _request("--text", "Error: ordinary text", "--no-newline", "--exit-code", "7"),
        tmp_path,
    )
    assert result["returncode"] == 7 and result["reason"] == "exited"
    assert result["views"]["stdout"]["text"] == "Error: ordinary text"
    assert result["complete"] and not result["issues"]
    assert (
        result["stored"]["stdout"]["sha256"] == hashlib.sha256(b"Error: ordinary text").hexdigest()
    )


def test_argv_is_literal_including_empty_arguments_and_shell_metacharacters(tmp_path):
    arguments = [
        "space name",
        "",
        "$(touch PWNED)",
        "; touch PWNED",
        "*",
        "-leading",
        "\n",
        'quote"\\',
        "Zażółć",
    ]
    request = ExecutionRequest(
        ExecutionMode.ARGV,
        argv=_request("--mode", "argv", "--", *arguments).argv,
        cwd=str(tmp_path),
    )
    result = capture(request, tmp_path)
    assert json.loads(result["views"]["stdout"]["text"]) == arguments
    assert not (tmp_path / "PWNED").exists()


def test_shell_command_keeps_native_shell_operators(tmp_path):
    request = ExecutionRequest(ExecutionMode.SHELL, shell_command="echo first&&echo second")
    result = capture(request, tmp_path)
    assert result["returncode"] == 0
    assert result["views"]["stdout"]["text"] == "first\nsecond\n"


def test_cwd_and_environment_are_inherited(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGETRIM_EXECUTION_FIXTURE", "inherited-value")
    code = (
        "import json,os; print(json.dumps([os.getcwd(),os.environ['USAGETRIM_EXECUTION_FIXTURE']]))"
    )
    request = ExecutionRequest(
        ExecutionMode.ARGV, argv=(sys.executable, "-c", code), cwd=str(tmp_path)
    )
    result = capture(request, tmp_path)
    cwd, value = json.loads(result["views"]["stdout"]["text"])
    assert Path(cwd) == tmp_path and value == "inherited-value"


@pytest.mark.parametrize("bad_cwd", [False, True])
def test_failed_start_has_no_child_exit_code_and_no_exception_body(tmp_path, bad_cwd):
    if bad_cwd:
        request = ExecutionRequest(
            ExecutionMode.ARGV,
            argv=(sys.executable, "-c", "pass"),
            cwd=str(tmp_path / "PRIVATE_START_ERROR"),
        )
    else:
        request = ExecutionRequest(
            ExecutionMode.ARGV, argv=(str(tmp_path / "PRIVATE_START_ERROR"),)
        )
    result = capture(request, tmp_path)
    assert result["reason"] == "failed_to_start" and result["returncode"] is None
    assert not result["complete"]
    assert "PRIVATE_START_ERROR" not in json.dumps(result)
    assert any(item["phase"] == "start" for item in result["issues"])


def test_large_dual_stream_output_and_slow_storage_remain_bounded(tmp_path):
    total = 2 * 1024 * 1024
    result = capture(
        _request("--mode", "records", "--bytes", str(total)),
        tmp_path,
        fault="slow",
        chunk_bytes=4096,
        queue_chunks=2,
        preview_bytes=512,
    )
    assert result["returncode"] == 0 and result["complete"]
    assert sum(item["bytes"] for item in result["stored"].values()) == total
    assert sum(result["observed_bytes"].values()) == total
    assert result["backpressure"] and result["queue_peak"] <= 2
    assert result["maximum_read"] <= 4096 and result["maximum_payload"] <= 4096
    assert result["order_preserved"]
    assert all(
        view["preview_bytes"] <= 512 and view["truncated"] for view in result["views"].values()
    )
    expected = {source: hashlib.sha256() for source in ("stdout", "stderr")}
    for index in range(total // 64):
        line = f"{index:08d} INFO execution fixture record={index:08d}".ljust(63).encode() + b"\n"
        source = "stdout" if (index * 64 // 65536) % 2 == 0 else "stderr"
        expected[source].update(line)
    for source in expected:
        assert result["stored"][source]["sha256"] == expected[source].hexdigest()
        assert result["stored"][source]["iter_error"] is None


def test_long_line_fails_closed_but_is_fully_drained(tmp_path):
    total = 1024 * 1024
    result = capture(
        _request("--mode", "line", "--bytes", str(total), "--no-newline"),
        tmp_path,
        chunk_bytes=8192,
        queue_chunks=1,
    )
    assert result["returncode"] == 0 and not result["complete"]
    assert result["observed_bytes"]["stdout"] == total
    assert "unavailable" in result["views"]["stdout"]["text"]
    assert not result["views"]["stdout"]["recovery_complete"]
    assert result["maximum_payload"] <= 8192 and result["queue_peak"] <= 1
    notice = b"[Execution artifact: unsafe or oversized remainder unavailable]\n"
    assert result["stored"]["stdout"]["sha256"] == hashlib.sha256(notice).hexdigest()


@pytest.mark.parametrize(
    "fault",
    [
        "sqlite",
        "permission",
        "startup_storage",
        "missing_recovery",
        "sqlite_busy",
        "sqlite_locked",
        "sqlite_io",
        "disk_full",
        "finish",
    ],
)
def test_storage_failures_do_not_deadlock_or_replace_child_exit_status(tmp_path, fault):
    result = capture(
        _request("--mode", "records", "--bytes", str(256 * 1024), "--exit-code", "7"),
        tmp_path,
        fault=fault,
        queue_chunks=1,
        preview_bytes=256,
    )
    assert result["returncode"] == 7 and result["reason"] == "exited"
    assert not result["complete"]
    assert any(item["phase"] == "storage" for item in result["issues"])
    assert "PRIVATE_STORAGE_ERROR" not in json.dumps(result)
    assert sum(result["observed_bytes"].values()) == 256 * 1024
    assert not result["views"]["stdout"]["recovery_complete"]


def test_reader_failure_keeps_actual_exit_code_and_partial_recovery(tmp_path):
    result = capture(
        _request("--mode", "records", "--bytes", "64", "--exit-code", "7"),
        tmp_path,
        fault="reader",
        queue_chunks=1,
    )
    assert result["returncode"] == 7 and not result["complete"]
    assert any(item["phase"] == "capture" for item in result["issues"])
    assert not result["views"]["stdout"]["recovery_complete"]
    assert "PRIVATE_READER_ERROR" not in json.dumps(result)


def test_quota_failure_continues_draining_both_pipes(tmp_path):
    result = capture(
        _request("--mode", "records", "--bytes", str(256 * 1024)),
        tmp_path,
        quota_bytes=8192,
        queue_chunks=1,
    )
    assert result["returncode"] == 0 and not result["complete"]
    assert any("quota" in item["type"] for item in result["issues"])
    assert sum(result["observed_bytes"].values()) == 256 * 1024
    assert not result["current_recovery_complete"]


def test_eviction_invalidates_recovery_without_fabricating_an_empty_complete_stream(tmp_path):
    result = capture(_request("--text", "previously captured"), tmp_path, fault="evict_after")
    assert result["complete"]  # Historical capture outcome, not a lease on CCR data.
    assert not result["current_recovery_complete"] and result["stored"] == {}


def test_corrupted_artifact_is_not_advertised_as_verified_full_recovery(tmp_path):
    result = capture(
        _request("--mode", "records", "--bytes", str(256 * 1024)),
        tmp_path,
        fault="corrupt_after",
    )
    assert result["complete"]  # Outcome predates the injected corruption.
    assert not result["current_recovery_complete"]
    assert result["stored"]["stdout"]["iter_error"] == "corrupt"
    assert not result["stored"]["stdout"]["integrity_verified"]


def test_redaction_precedes_preview_and_diagnostic_rendering(tmp_path):
    result = capture(_request("--text", "ghp_\x1b[31m" + "A" * 36), tmp_path, chunk_bytes=3)
    assert result["complete"] and result["repr_redacted"]
    assert _SECRET not in json.dumps(result)
    assert "A" * 36 not in result["views"]["stdout"]["text"]
    assert "REDACTED" in result["views"]["stdout"]["text"]
    assert result["secrets_absent_from_storage"]


@pytest.mark.parametrize(
    "text,lines",
    [
        ("sk-proj-" + "B" * 48, 1),
        ("sk-ant-api03-" + "C" * 48, 1),
        ("Authorization: Bearer " + "D" * 48, 1),
        ("postgres://user:" + "E" * 48 + "@localhost/db", 1),
        ("AKIA" + "F" * 16, 1),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmaXh0dXJlIn0.c2lnbmF0dXJl", 1),
        ("-----BEGIN PRIVATE KEY-----\n" + "G" * 48 + "\n-----END PRIVATE KEY-----", 3),
    ],
)
def test_split_credentials_are_redacted_in_full_recovery_and_database(tmp_path, text, lines):
    result = capture(_request("--text", text), tmp_path, chunk_bytes=3, queue_chunks=1)
    expected = ("[REDACTED_CREDENTIAL_LINE]\n" * lines).encode()
    assert result["complete"] and result["current_recovery_complete"]
    assert result["secrets_absent_from_storage"]
    assert result["stored"]["stdout"]["sha256"] == hashlib.sha256(expected).hexdigest()
    assert not _has_secret((json.dumps(result).encode(),))


def test_split_utf8_and_redaction_state_are_independent_per_stream(tmp_path):
    code = (
        "import os; "
        "os.write(1,b'ghp_'); "
        "os.write(2,'Zażółć 日本 😀\\n'.encode()); "
        "os.write(1,b'A'*36+b'\\n')"
    )
    request = ExecutionRequest(ExecutionMode.ARGV, argv=(sys.executable, "-c", code))
    result = capture(request, tmp_path, chunk_bytes=1, queue_chunks=1)
    assert result["complete"] and result["order_preserved"]
    assert "REDACTED" in result["views"]["stdout"]["text"]
    assert result["views"]["stderr"]["text"] == "Zażółć 日本 😀\n"
    expected = "Zażółć 日本 😀\n".encode()
    assert result["stored"]["stderr"]["sha256"] == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize("mode,expected", [("ansi", b"red\n"), ("crlf", b"one\ntwo\n")])
def test_terminal_sequences_split_at_every_byte_recover_normalized_text(tmp_path, mode, expected):
    result = capture(_request("--mode", mode), tmp_path, chunk_bytes=1, queue_chunks=1)
    assert result["complete"] and result["current_recovery_complete"]
    assert result["stored"]["stdout"]["sha256"] == hashlib.sha256(expected).hexdigest()


def test_split_osc_never_reaches_preview_or_recovered_artifact(tmp_path):
    text = "\x1b]title;" + _SECRET + "\x07visible"
    result = capture(_request("--text", text), tmp_path, chunk_bytes=1, queue_chunks=1)
    assert result["complete"] and result["secrets_absent_from_storage"]
    assert result["views"]["stdout"]["text"] == "visible\n"
    assert result["stored"]["stdout"]["sha256"] == hashlib.sha256(b"visible\n").hexdigest()


def test_preview_cap_can_cut_unicode_without_claiming_storage_is_incomplete(tmp_path):
    result = capture(_request("--text", "😀" * 10), tmp_path, preview_bytes=5)
    assert result["complete"]
    view = result["views"]["stdout"]
    assert view["text"] == "😀" and view["truncated"]
    assert view["preview_bytes"] == 4 and view["recovery_complete"]


def test_bare_cr_is_masked_using_existing_m2_policy(tmp_path):
    result = capture(_request("--mode", "cr"), tmp_path, chunk_bytes=1)
    assert result["complete"]
    assert "REDACTED" in result["views"]["stdout"]["text"]
    expected = b"[REDACTED_CREDENTIAL_LINE]\n"
    assert result["stored"]["stdout"]["sha256"] == hashlib.sha256(expected).hexdigest()


def test_pipe_read_failure_publishes_terminal_event_and_closes_pipe():
    from usagetrim.core import execution_runner as runner

    class BrokenPipe:
        closed = False

        def read(self, size):
            raise OSError("PRIVATE_PIPE_ERROR")

        def close(self):
            self.closed = True

    events = runner.queue.Queue(maxsize=1)
    ready = threading.Event()
    ready.set()
    reader = runner._PipeReader("stdout", events, ready, 8, "test-id")
    reader.pipe = BrokenPipe()

    def chunks():
        reader.pipe.read(8)
        return iter(())

    reader._chunks = chunks
    reader._run()
    event = events.get_nowait()
    assert event.terminal and event.failed and not event.data
    assert reader.pipe.closed
    assert "PRIVATE_PIPE_ERROR" not in repr(event)


def test_second_reader_start_failure_cannot_spawn_an_undrainable_child(monkeypatch, tmp_path):
    from usagetrim.core import execution_runner as runner
    from usagetrim.core.cache import ContextCache

    start = threading.Thread.start
    calls = []

    def failing_start(thread):
        calls.append(thread)
        if len(calls) == 2:
            raise RuntimeError("PRIVATE_THREAD_ERROR")
        return start(thread)

    def forbidden(*args, **kwargs):
        raise AssertionError("child must not start without two readers")

    monkeypatch.setattr(threading.Thread, "start", failing_start)
    monkeypatch.setattr(runner.subprocess, "Popen", forbidden)
    result = run_execution(_request(), cache=ContextCache(tmp_path / "cache.db"))
    assert result.termination_reason.value == "failed_to_start"
    assert result.returncode is None and not result.capture.complete
    assert len(calls) == 2 and not any(thread.is_alive() for thread in calls)
    assert any(issue.phase == "capture" for issue in result.issues)
    assert all(not stream.recovery.complete for stream in result.streams)
    assert "PRIVATE_THREAD_ERROR" not in repr(result)


def test_pre_cancel_is_observed_before_launch_or_cache_access(monkeypatch):
    from usagetrim.core import execution_runner as runner

    def forbidden(*args, **kwargs):
        raise AssertionError("side effect before cancellation check")

    monkeypatch.setattr(runner, "ContextCache", forbidden)
    monkeypatch.setattr(runner.subprocess, "Popen", forbidden)
    request = ExecutionRequest(ExecutionMode.ARGV, argv=(sys.executable,))
    cancelled = threading.Event()
    cancelled.set()
    result = run_execution(request, cancel_event=cancelled)
    assert result.termination_reason.value == "cancelled"
    assert result.returncode is None and result.streams == ()
    assert result.capture.complete is False


@pytest.mark.parametrize(
    "options",
    [
        {"chunk_bytes": 0},
        {"chunk_bytes": 65537},
        {"queue_chunks": 0},
        {"queue_chunks": True},
        {"queue_chunks": 65},
        {"preview_bytes": 65537},
        {"quota_bytes": 0},
    ],
)
def test_limits_reject_unbounded_or_invalid_buffers(options):
    with pytest.raises(ValueError):
        CaptureLimits(**options)


@pytest.mark.skipif(os.name != "posix", reason="Negative signal returncodes are POSIX semantics")
def test_natural_signal_exit_is_distinct_from_capture_success(tmp_path):
    request = ExecutionRequest(
        ExecutionMode.ARGV,
        argv=(
            sys.executable,
            "-c",
            "import os,signal; os.kill(os.getpid(), signal.SIGTERM)",
        ),
    )
    result = capture(request, tmp_path)
    assert result["reason"] == "signaled" and result["returncode"] == -15
