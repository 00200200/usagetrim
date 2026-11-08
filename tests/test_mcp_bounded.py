"""Bounded tool semantics and real, multi-message stdio sessions; offline only."""

from __future__ import annotations

import io
import json
import os
import queue
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from usagetrim.core.cache import ContextCache
from usagetrim.core.execution_contract import (
    CaptureView,
    ExecutionIssue,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    RecoveryInfo,
    StreamCapture,
    TerminationReason,
)
from usagetrim.mcp import bounded_exec, server, stdio

ROOT = Path(__file__).resolve().parents[1]
PRODUCER = ROOT / "tests/fixtures/execution/producer.py"


def call(identifier=1, **arguments):
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "method": "tools/call",
        "params": {"name": "usagetrim_exec", "arguments": {"bounded": True, **arguments}},
    }


def captured(*, code=0, reason=TerminationReason.EXITED, complete=True, issues=()):
    return ExecutionResult(
        ExecutionRequest(ExecutionMode.SHELL, shell_command="fixture"),
        reason,
        code,
        capture=CaptureView(text_scope="preview", stream_layout="separate", complete=complete),
        streams=tuple(
            StreamCapture(
                source,
                CaptureView(
                    text=(
                        "Error: literal [bold]data[/bold]\nsecond\n"
                        if source == "stdout"
                        else "diagnostic\n"
                    ),
                    text_scope="preview",
                    stream_layout="separate",
                    complete=complete,
                ),
                RecoveryInfo(ref_id="tc_" + digit * 16, available=True, complete=complete),
                preview_truncated=False,
            )
            for source, digit in (("stdout", "a"), ("stderr", "b"))
        ),
        issues=issues,
    )


def text(response):
    result = response["result"]
    assert result["structuredContent"] == {"isError": result["isError"]}
    assert len(result["content"]) == 1 and result["content"][0]["type"] == "text"
    return result["content"][0]["text"]


@pytest.mark.parametrize("profile", ["full", "coding", "minimal", "desktop"])
def test_bounded_schema_is_additive_for_every_exec_profile(profile):
    tool = next(
        tool for tool in server.tool_definitions(profile) if tool["name"] == "usagetrim_exec"
    )
    properties = tool["inputSchema"]["properties"]
    assert properties["bounded"]["default"] is False
    assert properties["timeout"]["type"] == "number"
    assert tool["inputSchema"]["required"] == ["command"]


def test_bounded_shell_request_is_opaque_offline_and_skips_legacy(monkeypatch, tmp_path):
    from usagetrim.core import execution_runner

    def forbidden(*args, **kwargs):
        raise AssertionError("bounded MCP must not use legacy capture/filters/tokenizers/telemetry")

    for name in (
        "_record",
        "record_text",
        "_compress",
        "compact_terminal_output",
        "safe_compact_output",
        "count_tokens",
        "auto_specialize_command_output",
    ):
        monkeypatch.setattr(server, name, forbidden)
    monkeypatch.setattr(server.subprocess, "run", forbidden)
    requests = []
    command = "  printf 'literal ; $(data)' && another  "

    def run(request, *, limits, cancel_event):
        requests.append((request, limits, cancel_event))
        return replace(captured(), request=request)

    monkeypatch.setattr(execution_runner, "run_execution", run)
    response = server._respond(call(command=command, cwd=str(tmp_path), timeout=12.5, max_lines=1))
    output = text(response)
    assert response["result"]["isError"] is False  # Error: in data is not a failure.
    assert "literal [bold]data[/bold]" in output and "second" not in output
    assert "preview=truncated; recovery=complete" in output
    request, limits, event = requests[0]
    assert request.mode is ExecutionMode.SHELL and request.shell_command == command
    assert request.argv is None and request.cwd == str(tmp_path)
    assert request.stdin_mode == "null" and request.timeout_seconds == 12.5
    assert limits.preview_bytes == 4096 and event is None


@pytest.mark.parametrize(
    "arguments",
    [
        {"command": "fixture", "timeout": 0},
        {"command": "fixture", "timeout": float("nan")},
        {"command": "fixture", "timeout": float("inf")},
        {"command": "fixture", "timeout": True},
        {"command": "fixture", "timeout": 3601},
        {"command": "fixture", "max_tokens": 100},
        {"command": "fixture", "budget": 100},
        {"command": "fixture", "max_lines": 0},
        {"command": "fixture", "engine": "none"},
        {"command": "fixture", "safe": True},
        {"command": "fixture", "cwd": "relative"},
        {"command": "fixture", "bounded": "true"},
        {"command": "fixture", "bounded": False, "timeout": 10},
    ],
)
def test_bounded_invalid_options_do_not_allocate_cache_or_process(monkeypatch, arguments):
    from usagetrim.core import execution_runner

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid arguments must not start capture")

    monkeypatch.setattr(execution_runner, "run_execution", forbidden)
    monkeypatch.setattr(server.subprocess, "run", forbidden)
    response = server._respond(call(**arguments))
    assert response["result"]["isError"] is True
    assert "Error:" in text(response)


@pytest.mark.parametrize("condition", ["paused", "wrapped"])
def test_bounded_refuses_implicit_passthrough(monkeypatch, condition):
    from usagetrim.core import execution_runner

    monkeypatch.setattr(bounded_exec, "paused", lambda: condition == "paused")
    monkeypatch.setattr(bounded_exec, "already_wrapped", lambda command: condition == "wrapped")

    def forbidden(*args, **kwargs):
        raise AssertionError("must reject rather than fall back to unbounded capture")

    monkeypatch.setattr(execution_runner, "run_execution", forbidden)
    monkeypatch.setattr(server.subprocess, "run", forbidden)
    response = server._respond(call(command="fixture"))
    assert response["result"]["isError"] and "passthrough" in text(response)


@pytest.mark.parametrize(
    "code,reason,complete,failed",
    [
        (0, TerminationReason.EXITED, True, False),
        (7, TerminationReason.EXITED, True, True),
        (-15, TerminationReason.SIGNALED, True, True),
        (0, TerminationReason.TIMED_OUT, False, True),
        (None, TerminationReason.FAILED_TO_START, False, True),
        (-15, TerminationReason.CANCELLED, False, True),
        (0, TerminationReason.EXITED, False, True),
    ],
)
def test_bounded_error_flag_uses_real_outcome(monkeypatch, code, reason, complete, failed):
    from usagetrim.core import execution_runner

    monkeypatch.setattr(
        execution_runner,
        "run_execution",
        lambda *a, **k: captured(code=code, reason=reason, complete=complete),
    )
    response = server._respond(call(command="fixture"))
    assert response["result"]["isError"] is failed
    assert reason.value in text(response)
    assert f"child_exit={'unknown' if code is None else code}" in text(response)


@pytest.mark.parametrize(
    "phase,category",
    [
        ("storage", "storage_error"),
        ("capture", "pipe_read_failed"),
        ("cleanup", "scope_close_failed"),
    ],
)
def test_bounded_failures_are_reported_without_exception_contents(monkeypatch, phase, category):
    from usagetrim.core import execution_runner

    value = captured(issues=(ExecutionIssue(phase, category, "PRIVATE_EXCEPTION_TEXT"),))
    monkeypatch.setattr(execution_runner, "run_execution", lambda *a, **k: value)
    response = server._respond(call(command="fixture"))
    assert response["result"]["isError"] is True
    assert f"{phase}/{category}" in text(response)
    assert "PRIVATE_EXCEPTION_TEXT" not in text(response)


@pytest.mark.parametrize("kind", ["runner", "unexpected"])
def test_bounded_runner_exceptions_do_not_escape_or_expose_raw_content(monkeypatch, kind):
    from usagetrim.core import execution_runner

    def fail(*args, **kwargs):
        if kind == "runner":
            raise execution_runner.ExecutionRunnerError(
                (ExecutionIssue("cleanup", "forced_termination_failed", "PRIVATE_EXCEPTION_TEXT"),)
            )
        raise OSError("PRIVATE_EXCEPTION_TEXT")

    monkeypatch.setattr(execution_runner, "run_execution", fail)
    response = server._respond(call(command="fixture"))
    assert response["result"]["isError"]
    assert "state unknown" in text(response) and "PRIVATE_EXCEPTION_TEXT" not in text(response)


@pytest.mark.parametrize("availability", [False, None])
def test_bounded_missing_or_unknown_recovery_is_not_advertised(monkeypatch, availability):
    from usagetrim.core import execution_runner

    value = captured(complete=False)
    value = replace(
        value,
        streams=tuple(
            replace(
                stream,
                recovery=RecoveryInfo(ref_id=stream.recovery.ref_id, available=availability),
            )
            for stream in value.streams
        ),
    )
    monkeypatch.setattr(execution_runner, "run_execution", lambda *a, **k: value)
    response = server._respond(call(command="fixture"))
    assert response["result"]["isError"] and "ref=tc_" not in text(response)
    assert "recovery=complete" not in text(response)


def test_legacy_exec_path_and_nonzero_iserror_contract_remain_unchanged(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout="legacy\n", returncode=7)

    monkeypatch.setattr(server.subprocess, "run", run)
    monkeypatch.setattr(server, "safe_compact_output", lambda raw, **kwargs: raw)
    monkeypatch.setattr(server, "auto_specialize_command_output", lambda *args: None)
    monkeypatch.setattr(server, "_record", lambda raw, output, **kwargs: output)
    response = server._respond(call(bounded=False, command="legacy fixture"))
    assert text(response) == "legacy\n\n[exit code: 7]"
    assert response["result"]["isError"] is False
    assert calls[0][1] == {
        "shell": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "errors": "replace",
        "timeout": 120,
        "cwd": None,
    }


def shell(argv):
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def until(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    gate = threading.Event()
    while not predicate():
        assert time.monotonic() < deadline, "handshake deadline expired"
        gate.wait(0.01)


class Client:
    """Read protocol output continuously, with bounded queues and deadlines."""

    def __init__(self, directory):
        self.messages = []
        self.reader_error = None
        self.incoming = queue.Queue(maxsize=64)
        self.errors = (directory / "server.stderr").open("wb")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "usagetrim.cli", "mcp", "--profile", "full"],
            env=os.environ
            | {
                "PYTHONPATH": str(ROOT / "src"),
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
            },
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.errors,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.closed = False

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(65537)
                if not line:
                    break
                assert len(line) <= 65536 and line.endswith(b"\n"), "unbounded protocol response"
                response = json.loads(line)
                assert isinstance(response, dict) and response["jsonrpc"] == "2.0"
                assert len(self.messages) < 64, "unexpected response flood"
                self.messages.append(response)
                self.incoming.put_nowait(response)
        except BaseException as error:
            self.reader_error = error
            try:
                self.incoming.put_nowait(error)
            except queue.Full:
                pass

    def send(self, request):
        self.process.stdin.write((json.dumps(request) + "\n").encode())
        self.process.stdin.flush()

    def receive(self, seconds=15):
        item = self.incoming.get(timeout=seconds)
        if isinstance(item, BaseException):
            raise AssertionError("invalid JSON-RPC stdout") from item
        return item

    def ping(self, identifier):
        self.send({"jsonrpc": "2.0", "id": identifier, "method": "ping"})

    def cancel(self, identifier):
        self.send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": identifier},
            }
        )

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.process.stdin.close()
            self.process.wait(timeout=35)
            assert self.process.returncode == 0
            self.reader.join(timeout=5)
            assert not self.reader.is_alive()
            assert self.reader_error is None
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process.stdout.close()
            self.errors.close()


@pytest.fixture
def client(tmp_path):
    connection = Client(tmp_path)
    try:
        yield connection
    finally:
        connection.close()


def waiting(directory, name):
    """A finite child with readiness/release handshakes, never an infinite test process."""
    ready, release = directory / f"{name}.ready", directory / f"{name}.release"
    script = (
        "import os, pathlib, threading, time; "
        f"ready=pathlib.Path({str(ready)!r}); release=pathlib.Path({str(release)!r}); "
        f"os.write(1, b'{name} ready\\n'); ready.touch(); deadline=time.monotonic()+25; "
        "gate=threading.Event()\n"
        "while not release.exists() and time.monotonic()<deadline: gate.wait(0.01)\n"
    )
    return shell([sys.executable, "-c", script]), ready, release


def refs(output):
    return re.findall(r"ref=(tc_[0-9a-f]{16})", output)


def test_real_stdio_large_bounded_output_and_recovery(client):
    command = shell(
        [sys.executable, str(PRODUCER), "--mode", "records", "--bytes", str(1024 * 1024)]
    )
    client.send(call(command=command, max_lines=2))
    response = client.receive(seconds=25)
    output = text(response)
    assert not response["result"]["isError"] and len(output.encode()) < 12000
    references = refs(output)
    assert len(set(references)) == 2 and output.count("preview=truncated; recovery=complete") == 2
    cache = ContextCache()
    views = [cache.read_artifact(ref, max_bytes=64) for ref in references]
    assert sum(view.metadata.stored_bytes for view in views) == 1024 * 1024
    assert all(view.metadata.complete and view.response_truncated for view in views)
    assert not (cache.db_path.parent / "telemetry.db").exists()


def test_real_stdio_shell_cwd_and_child_stdin_cannot_consume_protocol(client, tmp_path):
    script = "import json, os, sys; print(json.dumps([os.getcwd(), sys.stdin.read()]))"
    command = (
        shell([sys.executable, "-c", script])
        + " && "
        + shell([sys.executable, "-c", "print('second command')"])
    )
    client.send(call(command=command, cwd=str(tmp_path)))
    client.ping(2)
    responses = {response["id"]: response for response in (client.receive(), client.receive())}
    assert responses[2]["result"] == {}
    output = text(responses[1])
    assert json.dumps([str(tmp_path), ""]) in output and "second command" in output
    assert not responses[1]["result"]["isError"]


def test_real_stdio_ping_list_and_individual_cancellation(client, tmp_path):
    command, ready, _ = waiting(tmp_path, "cancelled")
    client.send(call("long", command=command))
    until(ready.exists)
    client.ping("ping")
    client.send({"jsonrpc": "2.0", "id": "list", "method": "tools/list"})
    assert {client.receive()["id"], client.receive()["id"]} == {"ping", "list"}
    client.cancel("unknown")
    client.cancel("long")
    client.ping("barrier")
    assert client.receive()["id"] == "barrier"
    client.close()  # Joins execution cleanup; delayed cancelled replies cannot escape the assertion.
    assert all(message["id"] != "long" for message in client.messages)
    cache = ContextCache()
    with sqlite3.connect(cache.db_path) as conn:
        references = [row[0] for row in conn.execute("SELECT ref_id FROM execution_artifacts")]
    assert len(references) == 2
    views = [cache.read_artifact(ref, max_bytes=128) for ref in references]
    assert all(not view.metadata.complete for view in views)
    assert any("cancelled ready" in view.text for view in views)


def test_real_stdio_parallel_execution_does_not_mix_ids_or_cancel_other_work(client, tmp_path):
    one, ready_one, _ = waiting(tmp_path, "one")
    two, ready_two, release_two = waiting(tmp_path, "two")
    client.send(call(1, command=one))
    client.send(call("1", command=two))
    until(lambda: ready_one.exists() and ready_two.exists())
    client.cancel(1)
    release_two.touch()
    response = client.receive()
    assert response["id"] == "1" and not response["result"]["isError"]
    assert "two ready" in text(response) and "one ready" not in text(response)
    client.close()
    assert all(message["id"] != 1 for message in client.messages)


def test_real_stdio_concurrency_limit_and_duplicate_ids_do_not_start_extra_children(
    client, tmp_path
):
    one, ready_one, release_one = waiting(tmp_path, "one")
    two, ready_two, release_two = waiting(tmp_path, "two")
    client.send(call(1, command=one))
    client.send(call(2, command=two))
    until(lambda: ready_one.exists() and ready_two.exists())
    extra, ready_extra, _ = waiting(tmp_path, "extra")
    client.send(call(3, command=extra))
    assert client.receive()["error"]["code"] == -32000
    client.send(call(1, command=extra))
    duplicate = client.receive()
    assert duplicate["id"] is None and duplicate["error"]["code"] == -32600
    assert not ready_extra.exists()
    release_one.touch()
    release_two.touch()
    responses = [client.receive(), client.receive()]
    assert {response["id"] for response in responses} == {1, 2}
    assert all(not response["result"]["isError"] for response in responses)
    client.close()
    assert not ready_extra.exists()


@pytest.mark.parametrize("code", [0, 7])
def test_real_stdio_nonzero_exit_and_legacy_contract(client, code):
    command = shell([sys.executable, str(PRODUCER), "--mode", "mixed", "--exit-code", str(code)])
    client.send(call(command=command))
    response = client.receive()
    assert response["result"]["isError"] is (code != 0)
    assert f"child_exit={code}" in text(response)
    assert all(value in text(response) for value in ("out1", "out2", "err1"))
    # Explicit already-wrapped legacy passthrough avoids token assets in this
    # offline integration test while retaining the original nonzero/isError rule.
    legacy = shell(
        [sys.executable, "-c", "print('legacy marker'); raise SystemExit(7)", "usagetrim"]
    )
    client.send(call(2, bounded=False, command=legacy))
    response = client.receive()
    assert not response["result"]["isError"]
    assert text(response).replace("\r\n", "\n") == "legacy marker\n\n[exit code: 7]"


def test_real_stdio_timeout_partial_recovery_and_eof_cleanup(client, tmp_path):
    command, _, _ = waiting(tmp_path, "timeout")
    client.send(call(command=command, timeout=5))
    response = client.receive()
    output = text(response)
    assert response["result"]["isError"] and "execution: timed_out" in output
    assert "timeout ready" in output and "recovery=complete" not in output
    assert any(
        "timeout ready" in ContextCache().read_artifact(ref, max_bytes=128).text
        for ref in refs(output)
    )
    other, other_ready, _ = waiting(tmp_path, "eof")
    client.send(call(2, command=other))
    until(other_ready.exists)
    client.close()
    assert not any(message["id"] == 2 for message in client.messages)
    cache = ContextCache()
    with sqlite3.connect(cache.db_path) as conn:
        references = [row[0] for row in conn.execute("SELECT ref_id FROM execution_artifacts")]
    assert len(references) == 4
    assert all(not cache.artifact_metadata(ref).complete for ref in references)


def test_real_stdio_failed_start_is_an_error_and_server_remains_responsive(client, tmp_path):
    missing = tmp_path / "PRIVATE_MISSING_DIRECTORY"
    client.send(call(command="echo never runs", cwd=str(missing)))
    response = client.receive()
    assert response["result"]["isError"]
    assert "execution: failed_to_start" in text(response)
    assert "PRIVATE_MISSING_DIRECTORY" not in text(response)
    client.ping(2)
    assert client.receive() == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_dispatcher_retains_cancelled_slot_until_cleanup_and_uses_fixed_workers():
    started = {identifier: threading.Event() for identifier in (1, 2)}
    release = {identifier: threading.Event() for identifier in (1, 2)}
    events = {}
    published = threading.Event()

    class Output(io.StringIO):
        def write(self, value):
            written = super().write(value)
            if json.loads(value).get("id") == 2:
                published.set()
            return written

    def respond(request, cancel=None):
        identifier = request["id"]
        events[identifier] = cancel
        started[identifier].set()
        assert release[identifier].wait(5), "test cleanup gate expired"
        return {"jsonrpc": "2.0", "id": identifier, "result": {}}

    output, errors = Output(), io.StringIO()
    dispatcher = stdio._Dispatcher(output, errors, respond)
    try:
        dispatcher.submit(call(1, command="fixture"))
        dispatcher.submit(call(2, command="fixture"))
        assert started[1].wait(5) and started[2].wait(5)
        dispatcher.cancel(1)
        dispatcher.cancel("unknown")
        dispatcher.cancel(True)  # bool must not alias integer request ID 1.
        assert events[1].is_set() and not events[2].is_set()
        assert events[1] is not events[2]
        dispatcher.submit(call(3, command="must not execute"))
        dispatcher.submit(call(1, command="duplicate must not execute"))
        assert len(dispatcher.workers) == 2 and dispatcher.jobs.maxsize == 2
        assert len(dispatcher.active) == 2
        release[2].set()
        assert published.wait(5)
    finally:
        for gate in release.values():
            gate.set()
        dispatcher.close()
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [message.get("error", {}).get("code") for message in messages] == [-32000, -32600, None]
    assert messages[-1]["id"] == 2 and not any(message.get("id") == 1 for message in messages)
    assert not any(worker.is_alive() for worker in dispatcher.workers)
    assert not dispatcher.active and errors.getvalue() == ""


def test_dispatcher_unexpected_handler_failure_releases_slot_and_returns_safe_error():
    published = threading.Event()

    class Output(io.StringIO):
        def write(self, value):
            count = super().write(value)
            published.set()
            return count

    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE_EXCEPTION_TEXT")

    output = Output()
    dispatcher = stdio._Dispatcher(output, io.StringIO(), fail)
    try:
        dispatcher.submit(call(command="fixture"))
        assert published.wait(5)
    finally:
        dispatcher.close()
    response = json.loads(output.getvalue())
    assert response["result"]["isError"] and "PRIVATE_EXCEPTION_TEXT" not in text(response)
    assert not dispatcher.active and not any(worker.is_alive() for worker in dispatcher.workers)


def test_shutdown_overrun_is_explicit_and_all_request_events_are_cancelled():
    joins = []

    class Worker:
        def join(self, *, timeout):
            joins.append(timeout)

        def is_alive(self):
            return True

    output, errors = io.StringIO(), io.StringIO()
    dispatcher = stdio._Dispatcher(output, errors, lambda *args: None)
    job = stdio._Job(call(command="fixture"))
    dispatcher.active[stdio._key(1)] = job
    dispatcher.workers = [Worker()]
    assert dispatcher.close() is False
    assert job.cancel.is_set() and dispatcher.closed.is_set()
    assert 0 <= joins[0] <= stdio.SHUTDOWN_SECONDS
    assert "shutdown incomplete" in errors.getvalue() and output.getvalue() == ""


@pytest.mark.parametrize("partial", [b"", b'{"jsonrpc":'])
def test_real_stdio_broken_stdout_exits_with_stdin_still_open(tmp_path, partial):
    """A worker's failed write must wake a reader awaiting another JSON line."""
    pending, pending_ready, _ = waiting(tmp_path, "pending")
    finishing, finishing_ready, release = waiting(tmp_path, "finishing")
    error_path = tmp_path / "disconnected.stderr"
    with error_path.open("wb") as errors:
        process = subprocess.Popen(
            [sys.executable, "-m", "usagetrim.cli", "mcp", "--profile", "full"],
            env=os.environ
            | {
                "PYTHONPATH": str(ROOT / "src"),
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
            },
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
        )
        try:
            frames = [call(1, command=pending), call(2, command=finishing)]
            process.stdin.write(
                b"".join((json.dumps(frame) + "\n").encode() for frame in frames) + partial
            )
            process.stdin.flush()
            until(lambda: pending_ready.exists() and finishing_ready.exists())
            # There is deliberately no parent reader thread retaining this pipe.
            # Closing only stdout leaves the server's stdin waiting indefinitely
            # in the old implementation, even if closed was already set.
            process.stdout.close()
            release.touch()
            process.wait(timeout=30)
            assert not process.stdin.closed, "stdin EOF must not be what stops the server"
            # CPython may retry the failed TextIO stdout flush at interpreter
            # finalization, yielding 120. This is not a runner cleanup failure.
            assert process.returncode in {0, 120}
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()
    diagnostics = error_path.read_text(encoding="utf-8")
    assert "shutdown incomplete" not in diagnostics
    if process.returncode == 120:
        assert "BrokenPipeError" in diagnostics or "OSError" in diagnostics
    cache = ContextCache()
    with sqlite3.connect(cache.db_path) as conn:
        references = [row[0] for row in conn.execute("SELECT ref_id FROM execution_artifacts")]
    views = [cache.read_artifact(ref, max_bytes=128) for ref in references]
    assert len(views) == 4
    assert sum(view.metadata.complete for view in views) == 2
    assert any(
        "pending ready" in view.text and view.metadata.available and not view.metadata.complete
        for view in views
    )


def test_interruptible_input_preserves_utf8_newlines_and_final_partial_line(tmp_path, monkeypatch):
    path = tmp_path / "frames"
    path.write_bytes("ż🙂\r\nnext\rlast".encode())
    monkeypatch.setattr(stdio, "_INPUT_CHUNK_BYTES", 1)
    with path.open(encoding="utf-8") as source:
        assert list(stdio._input_lines(source, threading.Event())) == ["ż🙂\n", "next\n", "last"]
    # Fileless embedding/test streams keep the existing iterator contract.
    assert list(stdio._input_lines(io.StringIO("one\ntwo"), threading.Event())) == ["one\n", "two"]


@pytest.mark.parametrize("stop_when_idle", [False, True])
def test_windows_input_peeks_before_reading_and_can_stop_without_stdin_eof(
    monkeypatch, stop_when_idle
):
    from usagetrim.core import execution_process

    source = SimpleNamespace(fileno=lambda: 123, encoding="utf-8", errors="strict")
    calls = []
    availability = iter([0] if stop_when_idle else [1, 0, -1])

    class Closed:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, timeout):
            assert timeout == stdio._INPUT_POLL_SECONDS
            calls.append("idle")
            self.stopped = stop_when_idle

    def read(descriptor, size):
        assert descriptor == 123 and size == 1
        calls.append("read")
        return b"x"

    monkeypatch.setattr(
        stdio,
        "os",
        SimpleNamespace(
            name="nt", fstat=lambda fd: SimpleNamespace(st_mode=stat.S_IFIFO), read=read
        ),
    )
    monkeypatch.setattr(execution_process, "pipe_available", lambda pipe: next(availability))
    assert list(stdio._input_lines(source, Closed())) == ([] if stop_when_idle else ["x"])
    assert calls == (["idle"] if stop_when_idle else ["read", "idle"])


def test_disconnected_dispatcher_does_not_admit_more_work():
    output = io.StringIO()
    dispatcher = stdio._Dispatcher(output, io.StringIO(), lambda *args: None)
    assert dispatcher.close()
    dispatcher.submit(call(command="must not run"))
    dispatcher.write({"jsonrpc": "2.0", "id": 1, "result": {}})
    assert not dispatcher.workers and not dispatcher.active and dispatcher.jobs.empty()
    assert output.getvalue() == ""


def test_real_stdio_pytest_filter_preserves_diagnostic_tail_and_protocol_status(client, tmp_path):
    raw = (
        "================ test session starts ================\ncollected 501 items\n"
        + "tests/test_ok.py::test_ok PASSED\n" * 500
        + "E AssertionError: middle diagnostic\nlast actionable context\n"
        + "================ 1 failed, 500 passed in 0.10s ================\n"
    )
    path = tmp_path / "pytest.log"
    path.write_text(raw, encoding="utf-8")
    script = (
        "import shutil,sys; f=open(sys.argv[1],encoding='utf-8'); "
        "shutil.copyfileobj(f,sys.stdout,4096); print('stderr separate',file=sys.stderr); "
        "raise SystemExit(7)"
    )
    client.send(call(command=shell([sys.executable, "-c", script, str(path)])))
    response = client.receive()
    output = text(response)
    assert response["result"]["isError"]
    assert "middle diagnostic" in output and "last actionable context" in output
    assert "filter: pytest; coverage=filtered" in output and "child_exit=7" in output
    assert "stderr separate" in output
    reference = refs(output)[0]
    view = ContextCache().read_artifact(reference, max_bytes=32768)
    assert view.text == raw and view.metadata.complete
