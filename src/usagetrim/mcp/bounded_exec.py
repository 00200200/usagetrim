"""Opt-in bounded MCP execution; no full-output filters or savings accounting."""

from __future__ import annotations

import math
import re
from pathlib import Path

from usagetrim.core.companion_state import already_wrapped, paused
from usagetrim.core.execution_contract import ExecutionMode, ExecutionRequest, TerminationReason

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 3600
PREVIEW_BYTES = 4096


def validate(arguments):
    allowed = {"command", "cwd", "bounded", "timeout", "max_lines"}
    if set(arguments) - allowed:
        raise ValueError("bounded execution supports only command, cwd, timeout and max_lines")
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a nonempty string")
    timeout = arguments.get("timeout", DEFAULT_TIMEOUT)
    if (
        type(timeout) not in {int, float}
        or not 0 < timeout <= MAX_TIMEOUT
        or not math.isfinite(timeout)
    ):
        raise ValueError("bounded timeout must be finite, positive and at most 3600 seconds")
    lines = arguments.get("max_lines", 80)
    if type(lines) is not int or not 1 <= lines <= 10000:
        raise ValueError("max_lines must be an integer between 1 and 10000")
    cwd = arguments.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not Path(cwd).is_absolute()):
        raise ValueError("cwd must be an absolute directory path")
    if paused() or already_wrapped(command):
        raise ValueError("bounded execution cannot preserve paused or already-wrapped passthrough")
    return (
        ExecutionRequest(
            ExecutionMode.SHELL,
            shell_command=command,
            cwd=cwd,
            timeout_seconds=timeout,
            stdin_mode="null",  # A child must never consume JSON-RPC input.
        ),
        lines,
    )


def _state(value):
    return "complete" if value is True else "incomplete" if value is False else "unknown"


def issue_lines(issues):
    for issue in issues:
        phase = (
            issue.phase
            if issue.phase in {"start", "capture", "processing", "storage", "cleanup"}
            else "unknown"
        )
        category = (
            issue.error_type
            if re.fullmatch(r"[a-z0-9_]{1,80}", issue.error_type)
            else "unclassified"
        )
        yield f"[bounded issue: {phase}/{category}]\n"


def render(result, max_lines):
    child = "unknown" if result.returncode is None else str(result.returncode)
    parts = [
        f"[bounded execution: {result.termination_reason.value}; child_exit={child}; "
        f"capture={_state(result.capture.complete)}]\n"
    ]
    for stream in result.streams:
        lines = stream.capture.text.splitlines(keepends=True)
        text = "".join(lines[:max_lines])
        truncated = len(lines) > max_lines or stream.preview_truncated is True
        preview = (
            "truncated"
            if truncated
            else "complete"
            if stream.preview_truncated is False
            else "unknown"
        )
        info = stream.recovery
        availability = (
            "unavailable"
            if info is None or info.available is False
            else "available"
            if info.available is True
            else "unknown"
        )
        recovery = _state(info.complete) if availability == "available" else availability
        ref = f"; ref={info.ref_id}" if availability == "available" and info.ref_id else ""
        parts.append(
            f"[{stream.stream} preview; capture={_state(stream.capture.complete)}; "
            f"preview={preview}; recovery={recovery}{ref}; availability={availability}]\n"
        )
        parts.append(text)
        if text and not text.endswith("\n"):
            parts.append("\n")
    parts.extend(issue_lines(result.issues))
    failed = (
        result.termination_reason is not TerminationReason.EXITED
        or result.returncode != 0
        or result.capture.complete is not True
        or bool(result.issues)
        or {stream.stream for stream in result.streams} != {"stdout", "stderr"}
        or any(
            stream.capture.complete is not True
            or stream.recovery is None
            or stream.recovery.available is not True
            or stream.recovery.complete is not True
            for stream in result.streams
        )
    )
    return "".join(parts), failed


def execute(arguments, *, cancel_event=None):
    from usagetrim.core.execution_runner import (
        CaptureLimits,
        ExecutionInterrupted,
        ExecutionRunnerError,
        run_execution,
    )

    request, lines = validate(arguments)  # Reject before cache/process allocation.
    try:
        result = run_execution(
            request, limits=CaptureLimits(preview_bytes=PREVIEW_BYTES), cancel_event=cancel_event
        )
    except ExecutionInterrupted as error:
        text, _ = render(error.result, lines)
        return text, True
    except ExecutionRunnerError as error:
        return (
            "[bounded runner failed; capture/recovery state unknown]\n"
            + "".join(issue_lines(error.issues)),
            True,
        )
    except Exception:
        return "[bounded runner failed; capture/recovery state unknown]\n", True
    return render(result, lines)
