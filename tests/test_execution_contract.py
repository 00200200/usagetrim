from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

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


def request():
    return ExecutionRequest(ExecutionMode.ARGV, argv=("tool", "", "space name", "*.py"))


def test_command_representations_preserve_input_and_hide_payloads():
    assert request().argv == ("tool", "", "space name", "*.py")
    command = "  tool 'space name' && other  "
    shell = ExecutionRequest(ExecutionMode.SHELL, shell_command=command, timeout_seconds=120)
    assert shell.shell_command == command and shell.argv is None
    assert command not in repr(shell) and "space name" not in repr(request())
    with pytest.raises(FrozenInstanceError):
        shell.shell_command = "changed"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": ExecutionMode.ARGV},
        {"mode": ExecutionMode.ARGV, "argv": ()},
        {"mode": ExecutionMode.ARGV, "argv": ("",)},
        {"mode": ExecutionMode.ARGV, "argv": ("tool",), "shell_command": "other"},
        {"mode": ExecutionMode.SHELL, "shell_command": " "},
        {"mode": ExecutionMode.SHELL, "shell_command": "tool", "argv": ("tool",)},
    ],
)
def test_invalid_command_representations(kwargs):
    with pytest.raises(ValueError):
        ExecutionRequest(**kwargs)


def test_failed_start_and_exited_are_distinct():
    failed = ExecutionResult(request(), TerminationReason.FAILED_TO_START, None)
    assert failed.returncode is None
    with pytest.raises(ValueError):
        ExecutionResult(request(), TerminationReason.FAILED_TO_START, 127)
    with pytest.raises(ValueError):
        ExecutionResult(request(), TerminationReason.EXITED, None)
    exited = ExecutionResult(request(), TerminationReason.EXITED, 127)
    assert exited.termination_reason is TerminationReason.EXITED


@pytest.mark.parametrize("reason", [TerminationReason.TIMED_OUT, TerminationReason.CANCELLED])
def test_timeout_and_future_cancel_do_not_invent_returncodes(reason):
    assert ExecutionResult(request(), reason, None).returncode is None
    assert ExecutionResult(request(), reason, -9).returncode == -9


def test_signal_returncode_is_observed_not_normalized():
    assert ExecutionResult(request(), TerminationReason.SIGNALED, -15).returncode == -15
    assert ExecutionResult(request(), TerminationReason.SIGNALED, 143).returncode == 143
    assert ExecutionResult(request(), TerminationReason.SIGNALED, None).returncode is None
    with pytest.raises(ValueError):
        ExecutionResult(request(), TerminationReason.SIGNALED, 0)


@pytest.mark.parametrize("mode", [ExecutionMode.ARGV, ExecutionMode.SHELL])
def test_nested_result_repr_hides_sensitive_fields(mode):
    secret = "private-secret-payload"
    command = ExecutionRequest(
        mode,
        argv=(secret,) if mode is ExecutionMode.ARGV else None,
        shell_command=secret if mode is ExecutionMode.SHELL else None,
        cwd=secret,
    )
    capture = CaptureView(secret)
    recovery = RecoveryInfo(ref_id=secret, spill_path=Path(secret))
    issue = ExecutionIssue("storage", secret, secret)
    result = ExecutionResult(
        command, TerminationReason.EXITED, 0, capture=capture, recovery=recovery, issues=(issue,)
    )
    for value in (command, capture, recovery, issue, result):
        assert secret not in repr(value)


@pytest.mark.parametrize("phase", ["capture", "storage"])
def test_child_success_and_usagetrim_failure_are_independent(phase):
    issue = ExecutionIssue(phase, "OSError", "private diagnostic")
    result = ExecutionResult(
        request(),
        TerminationReason.EXITED,
        0,
        capture=CaptureView("private output", text_scope="partial", complete=False),
        issues=(issue,),
    )
    assert result.returncode == 0 and result.issues == (issue,)
    assert "private diagnostic" not in repr(result)
    assert "private output" not in repr(result)


def test_unknown_capture_and_recovery_do_not_claim_completeness():
    capture = CaptureView("combined output")
    assert capture.stream_layout == "merged"
    assert not hasattr(capture, "stdout") and not hasattr(capture, "stderr")
    assert capture.complete is None and capture.replacement_occurred is None
    recovery = RecoveryInfo(ref_id="tc_example")
    assert recovery.available is None and recovery.complete is None
    assert RecoveryInfo(ref_id="tc_example", available=False).available is False
    with pytest.raises(ValueError):
        RecoveryInfo(available=True)
    with pytest.raises(ValueError):
        RecoveryInfo(ref_id="tc_example", complete=True)
    with pytest.raises(ValueError):
        CaptureView(text_scope="partial", complete=True)
    assert RecoveryInfo(ref_id="tc_example", available=True, complete=True).complete
    assert CaptureView(text_scope="preview", complete=True).complete


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True])
def test_invalid_durations(value):
    with pytest.raises(ValueError):
        ExecutionResult(request(), TerminationReason.EXITED, 0, elapsed_seconds=value)
    with pytest.raises(ValueError):
        ExecutionRequest(ExecutionMode.ARGV, argv=("tool",), timeout_seconds=value)


def test_separate_streams_are_additive_and_nested_repr_remains_safe():
    secret = "private-stream-payload"
    stream = StreamCapture(
        "stdout",
        CaptureView(secret, text_scope="preview", stream_layout="separate", complete=True),
        RecoveryInfo(ref_id=secret, available=True, complete=True),
        preview_truncated=True,
    )
    result = ExecutionResult(
        request(),
        TerminationReason.EXITED,
        0,
        execution_id=secret,
        streams=(stream,),
    )
    assert result.streams[0].capture.complete
    assert result.streams[0].preview_truncated
    assert secret not in repr(result) and secret not in repr(stream)
    assert StreamCapture("stderr").capture.stream_layout == "separate"
    legacy = ExecutionResult(request(), TerminationReason.EXITED, 0)
    assert legacy.streams == () and legacy.execution_id is None
    assert legacy.capture.stream_layout == "merged"
    with pytest.raises(ValueError, match="unique"):
        ExecutionResult(request(), TerminationReason.EXITED, 0, streams=(stream, stream))
    with pytest.raises(ValueError, match="unknown capture stream"):
        StreamCapture("merged")
