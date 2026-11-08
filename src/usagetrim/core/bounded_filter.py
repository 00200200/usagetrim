"""Bounded post-capture filtering of verified, redacted CCR fragments.

This is not a second capture path. No raw pipe bytes, full-log tokenizer or
whole-text public filter is used. Existing safe-filter recognizers define pytest
records; other formats retain the runner's preview/recovery contract.
"""

from __future__ import annotations

import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field, replace

from usagetrim.core import safe_filter
from usagetrim.core.cache import ContextCache
from usagetrim.core.execution_artifacts import ArtifactError
from usagetrim.core.execution_contract import ExecutionIssue, ExecutionResult, RecoveryInfo
from usagetrim.core.redactor import redact_secrets
from usagetrim.core.stream_redactor import LINE_LIMIT

SCAN_BYTES = 8 * 1024 * 1024
SCAN_LINES = 131072
SMALL_TEXT_BYTES = 64 * 1024
OUTPUT_BYTES = 16 * 1024
SCAN_SECONDS = 2.0


@dataclass(frozen=True)
class FilterView:
    text: str = field(repr=False)
    state: str = "preview"
    coverage: str = "preview"
    note: str = "unsupported_format"


@dataclass(frozen=True)
class FilteredExecution:
    result: ExecutionResult = field(repr=False)
    views: tuple[FilterView, ...] = ()


def _excerpt(line):
    value = safe_filter._truncate_diag_line(line)
    encoded = value.encode("utf-8")
    if len(encoded) > 256:
        value = encoded[:256].decode("utf-8", errors="ignore") + " [line excerpt]\n"
    return value if value.endswith("\n") else value + "\n"


class _DiagnosticSamples:
    """First/latest five-line context windows in each third of the input."""

    def __init__(self, total_lines):
        self.total = max(1, total_lines)
        self.head = []
        self.tail = deque(maxlen=6)
        self.before = deque(maxlen=2)
        self.first, self.last = {}, {}
        self.active = None

    def _save(self):
        if self.active is not None:
            region, records = self.active
            window = tuple(records)
            self.first.setdefault(region, window)
            self.last[region] = window
            self.active = None

    def feed(self, number, line):
        record = number, _excerpt(line)
        if len(self.head) < 6:
            self.head.append(record)
        self.tail.append(record)
        diagnostic = safe_filter._DIAGNOSTIC.search(line) is not None
        if self.active is not None:
            self.active[1].append(record)
            if len(self.active[1]) >= 5:
                self._save()
        elif diagnostic:
            region = min(2, (number - 1) * 3 // self.total)
            self.active = region, [*self.before, record]
        self.before.append(record)

    def finish(self):
        self._save()
        windows = [tuple(self.head), *self.first.values(), *self.last.values(), tuple(self.tail)]
        windows = sorted(set(window for window in windows if window), key=lambda item: item[0][0])
        parts = ["[UsageTrim: sampled diagnostic contexts; omitted text remains in CCR]\n"]
        for window in windows:
            parts.append(f"[lines {window[0][0]}-{window[-1][0]}; excerpts]\n")
            parts.extend(text for _, text in window)
        return "".join(parts)


class _PytestReducer:
    """Only routine pre-diagnostic pass/xdist records are folded incrementally."""

    def __init__(self, total_lines):
        self.data = bytearray()
        self.overflow = False
        self.diagnostics = False
        self.passed = self.progress = self.workers = 0
        self.samples = _DiagnosticSamples(total_lines)

    def _emit(self, text):
        encoded = text.encode("utf-8")
        if not self.overflow and len(self.data) + len(encoded) <= OUTPUT_BYTES:
            self.data.extend(encoded)
        else:
            self.overflow = True
            self.data.clear()

    def _flush(self):
        if self.progress:
            self._emit(
                f"[UsageTrim: {self.passed} passing tests, {self.progress} progress records]\n"
            )
        if self.workers:
            self._emit(f"[UsageTrim: {self.workers} xdist worker records]\n")
        self.passed = self.progress = self.workers = 0

    def feed(self, number, line):
        self.samples.feed(number, line)
        plain = safe_filter._plain(line)
        if safe_filter._DIAGNOSTIC.search(plain):
            self.diagnostics = True
        if not self.diagnostics:
            passed = safe_filter._passed_count(line)
            if passed:
                if self.workers:
                    self._flush()
                self.passed += passed
                self.progress += 1
                return
            if safe_filter._XDIST_NODE.match(plain.strip()):
                if self.progress:
                    self._flush()
                self.workers += 1
                return
        self._flush()
        self._emit(line)

    def finish(self, input_bytes):
        self._flush()
        if self.overflow:
            return self.samples.finish(), "sampled"
        text = self.data.decode("utf-8")
        if self.diagnostics and input_bytes <= SMALL_TEXT_BYTES:
            # Pure existing helper only; no tokenizer, duplicate CCR store or
            # public whole-text filter. Its entire input is explicitly bounded.
            lines, _ = safe_filter._compact_pytest_diagnostic(text.splitlines(keepends=True))
            text = "".join(lines)
        return text, "filtered"


def _lines(fragments, cancel_event, deadline):
    pending = ""
    consumed = number = 0
    for fragment in fragments:
        if cancel_event is not None and cancel_event.is_set():
            raise _Limit("cancelled")
        if time.monotonic() >= deadline:
            raise _Limit("scan_deadline")
        consumed += len(fragment.data)
        if consumed > SCAN_BYTES:
            raise _Limit("input_limit")
        pieces = (pending + fragment.data.decode("utf-8")).split("\n")
        pending = pieces.pop()
        for piece in pieces:
            number += 1
            if number > SCAN_LINES or len(piece.encode("utf-8")) > LINE_LIMIT:
                raise _Limit("line_limit")
            yield number, piece + "\n"
        if len(pending.encode("utf-8")) > LINE_LIMIT:
            raise _Limit("line_limit")
    if pending:
        number += 1
        if number > SCAN_LINES:
            raise _Limit("line_limit")
        yield number, pending


class _Limit(ValueError):
    pass


def _supported(cache, metadata, preview, command):
    if safe_filter._pytest_command(command):
        return True
    # For shell pipelines/unknown executables require the existing full-session
    # signature: header/collection plus actual final summary, not a header alone.
    head = preview.splitlines(keepends=True)[:8]
    if not head or "test session starts" not in head[0]:
        return False
    tail = cache.read_artifact(
        metadata.ref_id, first_line=max(1, metadata.line_count - 4), max_bytes=4096
    )
    if not tail.metadata.available:
        raise ArtifactError("missing")
    if not tail.metadata.complete or tail.metadata.content_hash != metadata.content_hash:
        raise ArtifactError("corrupt")
    if tail.response_truncated:
        return False
    return safe_filter._pytest_session([*head, *tail.text.splitlines(keepends=True)])


def _filter_execution(result, *, cache=None, cancel_event=None):
    """Return bounded display views; keep execution status independent of filters.

    Filter failures use M3 previews. A storage/integrity failure invalidates stale
    recovery metadata instead of continuing to advertise a complete artifact.
    """
    import shlex

    command = result.request.shell_command
    if command is None:
        command = shlex.join(result.request.argv or ())  # Classification only.
    streams, views, issues = [], [], list(result.issues)
    deadline = time.monotonic() + SCAN_SECONDS
    for stream in result.streams:
        fallback = FilterView(stream.capture.text)
        info = stream.recovery
        try:
            if stream.capture.complete is not True or info is None or info.complete is not True:
                view = replace(fallback, note="incomplete_capture")
            elif cancel_event is not None and cancel_event.is_set():
                view = replace(fallback, note="cancelled")
            elif (
                not safe_filter._pytest_command(command)
                and "test session starts" not in stream.capture.text[:200]
            ):
                view = fallback  # No cache read or construction for unknown formats.
            elif time.monotonic() >= deadline:
                view = replace(fallback, note="scan_deadline")
            else:
                if cache is None:
                    cache = ContextCache()
                if time.monotonic() >= deadline:
                    raise _Limit("scan_deadline")
                metadata = cache.artifact_metadata(info.ref_id)
                if not metadata.available:
                    raise ArtifactError("missing")
                if (
                    not metadata.complete
                    or metadata.execution_id != result.execution_id
                    or metadata.stream.value != stream.stream
                ):
                    raise ArtifactError("corrupt")
                if metadata.stored_bytes > SCAN_BYTES or metadata.line_count > SCAN_LINES:
                    raise _Limit("input_limit")
                if not _supported(cache, metadata, fallback.text, command):
                    view = fallback
                else:
                    reducer = _PytestReducer(metadata.line_count)
                    fragments = cache.iter_artifact(info.ref_id)
                    try:
                        for number, line in _lines(fragments, cancel_event, deadline):
                            reducer.feed(number, line)
                    finally:
                        fragments.close()
                    if cancel_event is not None and cancel_event.is_set():
                        raise _Limit("cancelled")
                    if time.monotonic() >= deadline:
                        raise _Limit("scan_deadline")
                    text, coverage = reducer.finish(metadata.stored_bytes)
                    text = redact_secrets(text)
                    if len(text.encode("utf-8")) > OUTPUT_BYTES:
                        raise _Limit("output_limit")
                    view = FilterView(text, "pytest", coverage, "redacted_ccr")
        except _Limit as error:
            view = replace(fallback, note=str(error))
        except (ArtifactError, OSError, sqlite3.Error) as error:
            known_lost = isinstance(error, ArtifactError) and error.code in {"missing", "corrupt"}
            stream = replace(
                stream,
                capture=replace(stream.capture, complete=False),
                recovery=RecoveryInfo(ref_id=info.ref_id, available=False if known_lost else None),
            )
            issues.append(
                ExecutionIssue(
                    "storage",
                    f"{stream.stream}_filter_recovery_unavailable",
                    "Filter recovery lookup failed.",
                )
            )
            view = replace(fallback, state="failed", note="recovery_unavailable")
        except Exception:
            view = replace(fallback, state="failed", note="filter_failed")
        streams.append(stream)
        views.append(view)
    result = replace(
        result,
        streams=tuple(streams),
        issues=tuple(issues),
        capture=(
            replace(result.capture, complete=False)
            if len(issues) > len(result.issues)
            else result.capture
        ),
    )
    return FilteredExecution(result, tuple(views))


def filter_execution(result, *, cache=None, cancel_event=None):
    """Never let a display-only filter failure replace the execution outcome."""
    try:
        return _filter_execution(result, cache=cache, cancel_event=cancel_event)
    except Exception:
        return FilteredExecution(
            result,
            tuple(
                FilterView(stream.capture.text, "failed", "preview", "filter_failed")
                for stream in result.streams
            ),
        )
