"""M4 CLI routing and bounded rendering; no full-output tokenization is needed."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from usagetrim import cli
from usagetrim.core import execution_runner
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

ROOT = Path(__file__).resolve().parents[1]
PRODUCER = ROOT / "tests/fixtures/execution/producer.py"
runner = CliRunner()


def invoke(*options, command=("fixture",)):
    return runner.invoke(cli.app, ["run", "--bounded", "--compact", *options, "--", *command])


def result(*, code=0, reason=TerminationReason.EXITED, complete=True, issues=()):
    streams = tuple(
        StreamCapture(
            source,
            CaptureView(
                text="[bold]literal[/bold]\nsecond\n" if source == "stdout" else "diagnostic\n",
                text_scope="preview",
                stream_layout="separate",
                complete=complete,
            ),
            RecoveryInfo(ref_id="tc_" + digit * 16, available=True, complete=complete),
            preview_truncated=False,
        )
        for source, digit in (("stdout", "a"), ("stderr", "b"))
    )
    return ExecutionResult(
        ExecutionRequest(ExecutionMode.ARGV, argv=("fixture",)),
        reason,
        code,
        capture=CaptureView(text_scope="preview", stream_layout="separate", complete=complete),
        streams=streams,
        issues=issues,
        elapsed_seconds=1.5,
    )


@pytest.fixture
def forbid_legacy(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Bounded CLI must not run legacy capture, filters or text telemetry")

    for name in (
        "record_text",
        "compute_metrics",
        "count_tokens",
        "compress_to_budget",
        "safe_compact_output",
        "compact_terminal_output",
        "slim_json",
        "spill_large_output",
        "auto_specialize_command_output",
        "filter_gh_command_output",
        "ContextCache",
        "TelemetryStore",
    ):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(cli.subprocess, "run", forbidden)


def test_legacy_run_keeps_merged_capture_exit_and_telemetry(monkeypatch):
    calls, recorded = [], []

    def legacy(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout="native payload\n", returncode=7)

    def forbidden(*args, **kwargs):
        raise AssertionError("Legacy run must not invoke bounded execution")

    monkeypatch.setattr(cli.subprocess, "run", legacy)
    monkeypatch.setattr(execution_runner, "run_execution", forbidden)
    monkeypatch.setattr(cli, "record_text", lambda *args, **kwargs: recorded.append((args, kwargs)))
    argv = ["fixture", "space name", "", "$(literal)"]
    response = runner.invoke(cli.app, ["run", "--engine", "none", "--", *argv])
    assert response.exit_code == 7 and response.stdout == "native payload\n"
    assert calls == [
        (
            argv,
            {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "errors": "replace",
            },
        )
    ]
    assert recorded[0][0] == ("native payload\n", "native payload\n")
    assert recorded[0][1]["engine"] == "none"


def test_bounded_routes_literal_argv_cwd_and_timeout_without_legacy(
    monkeypatch, tmp_path, forbid_legacy
):
    monkeypatch.chdir(tmp_path)
    argv = ("fixture", "space name", "", "a\nb", "'\"\\", "$(touch PWNED)", ";", "*.py", "-x")
    calls = []

    def execute(request, *, limits):
        calls.append((request, limits))
        return replace(result(), request=request)

    monkeypatch.setattr(execution_runner, "run_execution", execute)
    response = invoke("--timeout", "12.5", command=argv)
    assert response.exit_code == 0, response.output
    request, limits = calls[0]
    assert request.mode is ExecutionMode.ARGV and request.argv == argv
    assert request.shell_command is None and request.cwd == str(tmp_path)
    assert request.timeout_seconds == 12.5 and limits.preview_bytes == 4096
    assert response.stdout == "[bold]literal[/bold]\nsecond\n"
    assert "diagnostic" in response.stderr and "[bold]literal" not in response.stderr
    assert not (tmp_path / "PWNED").exists()


@pytest.mark.parametrize(
    "options,message",
    [
        (["--bounded"], "requires --compact"),
        (["--bounded", "--safe"], "requires --compact"),
        (["--bounded", "--compact", "--budget", "100"], "not supported with --bounded"),
        (["--bounded", "--compact", "--engine", "none"], "cannot preserve passthrough"),
        (["--bounded", "--compact", "--max-lines", "0"], "must be positive"),
        (["--timeout", "5"], "requires --bounded"),
        *[
            (["--bounded", "--compact", "--timeout", value], "finite and positive")
            for value in ("0", "-1", "nan", "inf")
        ],
    ],
)
def test_invalid_bounded_options_fail_before_execution(monkeypatch, options, message):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid options must not start execution or touch cache")

    monkeypatch.setattr(execution_runner, "run_execution", forbidden)
    monkeypatch.setattr(cli.subprocess, "run", forbidden)
    monkeypatch.setattr(cli, "ContextCache", forbidden)
    response = runner.invoke(cli.app, ["run", *options, "--", "fixture"])
    assert response.exit_code == 2 and "Traceback" not in response.output
    assert message in re.sub(r"\s+", " ", response.output)


@pytest.mark.parametrize("condition", ["paused", "wrapped"])
def test_bounded_refuses_implicit_passthrough(monkeypatch, condition):
    if condition == "paused":
        from usagetrim.core import companion_state

        companion_state.update_settings(paused=True)
    else:
        monkeypatch.setattr(cli, "already_wrapped", lambda command: True)

    def forbidden(*args, **kwargs):
        raise AssertionError("Passthrough must not enter the bounded renderer")

    monkeypatch.setattr(execution_runner, "run_execution", forbidden)
    response = invoke()
    assert response.exit_code == 2 and "passthrough" in response.output


@pytest.mark.parametrize("quiet", [False, True])
def test_bounded_line_limit_and_quiet_preserve_required_recovery_metadata(monkeypatch, quiet):
    monkeypatch.setattr(execution_runner, "run_execution", lambda *args, **kwargs: result())
    response = invoke("--max-lines", "1", *(["--quiet"] if quiet else []))
    assert response.exit_code == 0 and response.stdout == "[bold]literal[/bold]\n"
    assert "stdout: capture=complete; preview=truncated; recovery=complete" in response.stderr
    assert "ref=tc_" + "a" * 16 in response.stderr
    assert "ref=tc_" + "b" * 16 in response.stderr
    assert "child_exit=0" in response.stderr
    assert ("bounded summary" in response.stderr) is (not quiet)


@pytest.mark.parametrize(
    "code,reason,expected",
    [
        (0, TerminationReason.EXITED, 0),
        (7, TerminationReason.EXITED, 7),
        (-15, TerminationReason.SIGNALED, 143),
        (0, TerminationReason.TIMED_OUT, 124),
        (-15, TerminationReason.CANCELLED, 130),
        (None, TerminationReason.FAILED_TO_START, 126),
    ],
)
def test_bounded_exit_status_is_separate_from_child_status(monkeypatch, code, reason, expected):
    value = result(code=code, reason=reason, complete=reason is TerminationReason.EXITED)
    monkeypatch.setattr(execution_runner, "run_execution", lambda *args, **kwargs: value)
    response = invoke("--quiet")
    assert response.exit_code == expected
    assert reason.value in response.stderr
    assert f"child_exit={'unknown' if code is None else code}" in response.stderr


@pytest.mark.parametrize(
    "available,complete", [(True, False), (True, None), (False, False), (None, None)]
)
def test_bounded_never_promises_full_recovery_for_lost_or_unknown_data(
    monkeypatch, available, complete
):
    value = result(complete=False)
    streams = tuple(
        replace(
            stream,
            recovery=RecoveryInfo(
                ref_id=stream.recovery.ref_id, available=available, complete=complete
            ),
        )
        for stream in value.streams
    )
    monkeypatch.setattr(
        execution_runner, "run_execution", lambda *args, **kwargs: replace(value, streams=streams)
    )
    response = invoke("--quiet")
    assert response.exit_code == 1
    assert "recovery=complete" not in response.stderr
    assert "capture=incomplete" in response.stderr
    assert ("ref=tc_" in response.stderr) is (available is True)


@pytest.mark.parametrize("child_code,expected", [(0, 1), (7, 7)])
def test_bounded_cleanup_failure_is_visible_even_when_quiet(monkeypatch, child_code, expected):
    issue = ExecutionIssue("cleanup", "scope_close_failed", "PRIVATE_EXCEPTION_CONTENT")
    monkeypatch.setattr(
        execution_runner,
        "run_execution",
        lambda *args, **kwargs: result(code=child_code, issues=(issue,)),
    )
    response = invoke("--quiet")
    assert response.exit_code == expected
    assert "cleanup/scope_close_failed" in response.stderr
    assert "PRIVATE_EXCEPTION_CONTENT" not in response.output


@pytest.mark.parametrize("kind", ["interrupt", "plain_interrupt", "runner", "unexpected"])
def test_bounded_failures_are_controlled_and_do_not_expose_exception_text(monkeypatch, kind):
    def execute(*args, **kwargs):
        if kind == "interrupt":
            raise execution_runner.ExecutionInterrupted(
                result(code=-15, reason=TerminationReason.CANCELLED, complete=False)
            )
        if kind == "plain_interrupt":
            raise KeyboardInterrupt
        if kind == "runner":
            raise execution_runner.ExecutionRunnerError(
                (
                    ExecutionIssue(
                        "cleanup", "forced_termination_failed", "PRIVATE_EXCEPTION_CONTENT"
                    ),
                )
            )
        raise OSError("PRIVATE_EXCEPTION_CONTENT")

    monkeypatch.setattr(execution_runner, "run_execution", execute)
    response = invoke()
    assert response.exit_code == (130 if kind in {"interrupt", "plain_interrupt"} else 1)
    assert "PRIVATE_EXCEPTION_CONTENT" not in response.output and "Traceback" not in response.output
    if kind == "interrupt":
        assert "recovery=incomplete; ref=tc_" in response.stderr
    elif kind == "runner":
        assert "forced_termination_failed" in response.stderr


@pytest.mark.parametrize(
    "script,expected", [("print('actual child')", "actual child\n"), ("pass", "")]
)
def test_real_bounded_cli_uses_only_small_reads_and_no_legacy_filters(
    monkeypatch, forbid_legacy, script, expected
):
    reads = []
    read = ContextCache.read_artifact

    def limited(cache, ref_id, **kwargs):
        assert 0 < kwargs["max_bytes"] <= 4096
        reads.append(kwargs["max_bytes"])
        return read(cache, ref_id, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("CLI may not materialize an entire artifact")

    monkeypatch.setattr(ContextCache, "read_artifact", limited)
    monkeypatch.setattr(ContextCache, "retrieve", forbidden)
    monkeypatch.setattr(ContextCache, "iter_artifact", forbidden)
    response = invoke("--timeout", "10", command=(sys.executable, "-c", script))
    assert response.exit_code == 0, response.output
    assert response.stdout == expected
    assert response.stderr.count("recovery=complete; ref=tc_") == 2
    assert reads == [4096, 4096]


@pytest.mark.parametrize(
    "phase,category", [("storage", "quota_exceeded"), ("capture", "pipe_read_failed")]
)
def test_bounded_capture_and_storage_failures_are_not_success(monkeypatch, phase, category):
    value = result(complete=False, issues=(ExecutionIssue(phase, category, "PRIVATE_ERROR"),))
    monkeypatch.setattr(execution_runner, "run_execution", lambda *args, **kwargs: value)
    response = invoke("--quiet")
    assert response.exit_code == 1 and "child_exit=0" in response.stderr
    assert f"{phase}/{category}" in response.stderr
    assert "recovery=complete" not in response.stderr and "PRIVATE_ERROR" not in response.output


def test_bounded_interrupt_during_render_is_not_success(monkeypatch):
    monkeypatch.setattr(execution_runner, "run_execution", lambda *args, **kwargs: result())

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_render_bounded_result", interrupted)
    assert invoke().exit_code == 130


def cli_process(command, *, options=(), stdin=None, cwd=None):
    """Capture only the bounded CLI response, with an independent test deadline.

    Test children are finite (the timeout fixture exits by itself after 20 s).
    Never use this helper for an unbounded/hostile child or arbitrary user code.
    """
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "usagetrim.cli",
            "run",
            "--bounded",
            "--compact",
            *options,
            "--",
            *command,
        ],
        cwd=cwd,
        env=os.environ
        | {
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        },
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = process.communicate(stdin, timeout=30)
        return process.returncode, stdout.decode("utf-8"), stderr.decode("utf-8")
    finally:
        try:
            if process.returncode is None:
                process.kill()
                process.communicate(timeout=5)
        finally:
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()


def references(stderr):
    return dict(re.findall(r"bounded (stdout|stderr):[^\n]*ref=(tc_[0-9a-f]{16})", stderr))


@pytest.mark.parametrize("child_code", [0, 7])
def test_bounded_real_separate_streams_and_recovery(child_code):
    code, stdout, stderr = cli_process(
        [sys.executable, str(PRODUCER), "--mode", "mixed", "--exit-code", str(child_code)]
    )
    assert code == child_code and stdout.replace("\r\n", "\n") == "out1\nout2\n"
    assert "err1" in stderr
    refs = references(stderr)
    assert set(refs) == {"stdout", "stderr"} and len(set(refs.values())) == 2
    cache = ContextCache()
    assert cache.read_artifact(refs["stdout"], max_bytes=128).text == "out1\nout2\n"
    assert cache.read_artifact(refs["stderr"], max_bytes=128).text == "err1\n"
    assert not (cache.db_path.parent / "telemetry.db").exists()


def test_bounded_large_output_limits_preview_without_materializing_recovery():
    size = 1024 * 1024
    code, stdout, stderr = cli_process(
        [sys.executable, str(PRODUCER), "--mode", "records", "--bytes", str(size)],
        options=("--max-lines", "2"),
    )
    assert code == 0 and len(stdout.splitlines()) == 2
    assert len(stdout.encode()) <= 4096 and len(stderr.encode()) < 8192
    refs = references(stderr)
    assert len(refs) == 2 and stderr.count("preview=truncated; recovery=complete") == 2
    cache = ContextCache()
    views = [cache.read_artifact(ref, max_bytes=64) for ref in refs.values()]
    assert sum(view.metadata.stored_bytes for view in views) == size
    assert all(view.metadata.complete and view.response_truncated for view in views)
    assert all(len(view.text.encode()) <= 64 for view in views)


def test_bounded_real_stdin_environment_cwd_and_literal_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDED_CLI_TEST", "inherited")
    args = ["two words", "", "*.py", "$(literal)", ";", "-option"]
    script = (
        "import json, os, sys; "
        "print(json.dumps([os.getcwd(), os.environ['BOUNDED_CLI_TEST'], sys.argv[1:], sys.stdin.read()]))"
    )
    code, stdout, stderr = cli_process(
        [sys.executable, "-c", script, *args], cwd=tmp_path, stdin=b"pipeline payload\n"
    )
    assert code == 0, stderr
    assert json.loads(stdout) == [str(tmp_path), "inherited", args, "pipeline payload\n"]


def test_bounded_real_timeout_keeps_partial_recovery(tmp_path):
    natural_exit = tmp_path / "natural_exit"
    script = (
        "import os, pathlib, threading; os.write(1, b'ready\\n'); "
        f"threading.Event().wait(20); pathlib.Path({str(natural_exit)!r}).touch()"
    )
    code, stdout, stderr = cli_process(
        [sys.executable, "-c", script],
        options=("--timeout", "5"),
    )
    assert code == 124 and "ready" in stdout
    assert "execution: timed_out" in stderr and "capture=incomplete" in stderr
    assert not natural_exit.exists(), "timeout must stop the child, not wait for natural completion"
    assert "recovery=complete" not in stderr
    refs = references(stderr)
    assert "stdout" in refs
    view = ContextCache().read_artifact(refs["stdout"], max_bytes=128)
    assert "ready" in view.text and view.metadata.available and not view.metadata.complete


def test_bounded_real_failed_start_does_not_claim_success_or_leak_path(tmp_path):
    code, stdout, stderr = cli_process([str(tmp_path / "PRIVATE_MISSING_EXECUTABLE")])
    assert code == 126 and stdout == ""
    assert "execution: failed_to_start" in stderr and "child_exit=unknown" in stderr
    assert "PRIVATE_MISSING_EXECUTABLE" not in stderr
