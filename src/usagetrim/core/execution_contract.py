"""Internal value types only; neither current execution entry point uses these yet.

Unknown metadata stays unknown. In particular, a decoded string cannot establish
whether replacement occurred, and a published ref does not prove availability.
These types do not run processes, persist output, or define public rendering.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal


class ExecutionMode(Enum):
    ARGV = "argv"
    SHELL = "shell"


class TerminationReason(Enum):
    EXITED = "exited"
    SIGNALED = "signaled"
    TIMED_OUT = "timed_out"
    FAILED_TO_START = "failed_to_start"
    CANCELLED = "cancelled"  # Reserved for a future runner, not implemented here.


def _duration(value: float | None, *, positive: bool = False) -> None:
    if value is not None and (
        type(value) not in {int, float}
        or not math.isfinite(value)
        or value < 0
        or (positive and value == 0)
    ):
        raise ValueError(
            "duration must be finite and positive"
            if positive
            else "duration must be finite and nonnegative"
        )


@dataclass(frozen=True)
class ExecutionRequest:
    mode: ExecutionMode
    argv: tuple[str, ...] | None = field(default=None, repr=False)
    shell_command: str | None = field(default=None, repr=False)
    cwd: str | None = field(default=None, repr=False)
    timeout_seconds: float | None = None
    # Current paths inherit env and stdin. No environment snapshot is retained.
    # A shell string stays opaque: it must never be reconstructed from argv.

    def __post_init__(self) -> None:
        if self.mode is ExecutionMode.ARGV:
            if (
                type(self.argv) is not tuple
                or not self.argv
                or not all(isinstance(arg, str) for arg in self.argv)
                or not self.argv[0]
                or self.shell_command is not None
            ):
                raise ValueError("argv mode requires an executable tuple and no shell command")
        elif self.mode is ExecutionMode.SHELL:
            if (
                not isinstance(self.shell_command, str)
                or not self.shell_command.strip()
                or self.argv is not None
            ):
                raise ValueError("shell mode requires an opaque command and no argv")
        else:
            raise ValueError("unknown execution mode")
        _duration(self.timeout_seconds, positive=True)


@dataclass(frozen=True)
class CaptureView:
    text: str = field(default="", repr=False)
    text_scope: Literal["full", "preview", "partial"] = "full"
    stream_layout: Literal["merged", "unknown"] = "merged"
    complete: bool | None = None
    encoding: str | None = None
    replacement_occurred: bool | None = None

    # There are deliberately no inferred stdout/stderr fields for merged text.
    # A future complete capture may expose just a preview and a recovery ref.

    def __post_init__(self) -> None:
        if self.text_scope not in {"full", "preview", "partial"} or self.stream_layout not in {
            "merged",
            "unknown",
        }:
            raise ValueError("unknown capture representation")
        if self.text_scope == "partial" and self.complete is True:
            raise ValueError("partial capture cannot be complete")


@dataclass(frozen=True)
class RecoveryInfo:
    ref_id: str | None = field(default=None, repr=False)
    spill_path: Path | None = field(default=None, repr=False)
    available: bool | None = None
    complete: bool | None = None
    # Completeness concerns the redacted, decoded view, never original bytes.

    def __post_init__(self) -> None:
        if self.available is True and not self.ref_id and self.spill_path is None:
            raise ValueError("available recovery requires a location")
        if self.complete is True and self.available is not True:
            raise ValueError("complete recovery requires known availability")


@dataclass(frozen=True)
class ExecutionIssue:
    phase: Literal["start", "capture", "processing", "storage"]
    error_type: str = field(repr=False)
    message: str = field(repr=False)


@dataclass(frozen=True)
class ExecutionResult:
    request: ExecutionRequest
    termination_reason: TerminationReason
    returncode: int | None
    capture: CaptureView = field(default_factory=CaptureView)
    elapsed_seconds: float | None = None  # Execution/capture time, not rendering time.
    recovery: RecoveryInfo | None = None
    issues: tuple[ExecutionIssue, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.termination_reason, TerminationReason):
            raise ValueError("unknown termination reason")
        if self.returncode is not None and type(self.returncode) is not int:
            raise ValueError("returncode must be an observed integer or None")
        if (
            self.termination_reason is TerminationReason.FAILED_TO_START
            and self.returncode is not None
        ):
            raise ValueError("failed start cannot have a child returncode")
        if self.termination_reason is TerminationReason.EXITED and self.returncode is None:
            raise ValueError("exited child requires its returncode")
        if self.termination_reason is TerminationReason.SIGNALED and self.returncode == 0:
            raise ValueError("signal termination cannot be exit zero")
        _duration(self.elapsed_seconds)
