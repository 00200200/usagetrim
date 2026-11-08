"""Bounded redacted-artifact filtering, independent of process timing/network."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from usagetrim.core import bounded_filter as filters
from usagetrim.core.benchmark import benchmark_bounded_case
from usagetrim.core.benchmark_corpus import load_benchmark_cases
from usagetrim.core.cache import ContextCache
from usagetrim.core.execution_contract import (
    CaptureView,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    RecoveryInfo,
    StreamCapture,
    TerminationReason,
)

MANIFEST = Path(__file__).parent / "fixtures/benchmarks/manifest.json"


@pytest.fixture(autouse=True)
def deterministic_filter_clock(monkeypatch):
    # Data-correctness tests must not become performance gates on a busy CI host.
    monkeypatch.setattr(filters, "time", SimpleNamespace(monotonic=lambda: 0.0))
    import tiktoken.load

    def forbidden(*args, **kwargs):
        raise AssertionError("network/vocabulary access is forbidden")

    monkeypatch.setattr(tiktoken.load, "read_file", forbidden)


def capture(cache, data, *, command="pytest -v", stderr=b"", chunk=8192, code=1):
    stdout = cache.begin_artifact(stream="stdout")
    error = cache.begin_artifact(stream="stderr", execution_id=stdout.execution_id)
    streams = []
    for writer, raw in ((stdout, data), (error, stderr)):
        for start in range(0, len(raw), chunk):
            writer.append(raw[start : start + chunk])
        writer.finish()
        view = cache.read_artifact(writer.ref_id, max_bytes=4096)
        streams.append(
            StreamCapture(
                writer.stream.value,
                CaptureView(view.text, "preview", "separate", view.metadata.complete),
                RecoveryInfo(
                    writer.ref_id,
                    available=view.metadata.available,
                    complete=view.metadata.complete,
                ),
                view.response_truncated,
            )
        )
    return ExecutionResult(
        ExecutionRequest(ExecutionMode.SHELL, shell_command=command),
        TerminationReason.EXITED,
        code,
        capture=CaptureView(
            text_scope="preview",
            stream_layout="separate",
            complete=all(s.capture.complete for s in streams),
        ),
        streams=tuple(streams),
        execution_id=stdout.execution_id,
    )


def recovered_hash(cache, ref):
    digest = hashlib.sha256()
    for fragment in cache.iter_artifact(ref):
        digest.update(fragment.data)
    return digest.hexdigest()


CASES = [
    case
    for case in load_benchmark_cases(MANIFEST)
    if filters.safe_filter._pytest_command(case.command)
]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.case_id)
def test_pytest_corpus_preserves_required_diagnostics_and_exact_recovery(case):
    report = benchmark_bounded_case(case)
    assert report["filter_state"] == "pytest", report
    assert report["signals_preserved"], report["missing_signals"]
    assert report["exact_redacted_normalized_recovery"]
    assert report["legacy_safe_filter"] is not None
    assert report["legacy_safe_filter"]["missing_signals"] == []


def test_samples_preserve_early_middle_late_diagnostics_with_context(tmp_path):
    cache = ContextCache(tmp_path / "cache.db")
    lines = [f"unique ordinary record {index}\n" for index in range(2400)]
    for position, label in ((20, "early"), (1200, "middle"), (2370, "late")):
        lines[position - 1] = f"context before {label}\n"
        lines[position] = f"E AssertionError: {label} diagnostic\n"
        lines[position + 1] = f"context after {label}\n"
    data = "".join(lines).encode()
    result = capture(cache, data)
    before = recovered_hash(cache, result.streams[0].recovery.ref_id)
    filtered = filters.filter_execution(result, cache=cache)
    view = filtered.views[0]
    assert view.state == "pytest" and view.coverage == "sampled"
    for label in ("early", "middle", "late"):
        assert all(
            value in view.text
            for value in (
                f"context before {label}",
                f"{label} diagnostic",
                f"context after {label}",
            )
        )
    assert "omitted text remains in CCR" in view.text
    assert len(view.text.encode()) <= filters.OUTPUT_BYTES
    assert (
        recovered_hash(cache, result.streams[0].recovery.ref_id)
        == before
        == hashlib.sha256(data).hexdigest()
    )
    assert filtered.result.returncode == 1 and filtered.result.capture.complete


@pytest.mark.parametrize("chunk", [1, 3, 4096])
def test_normalization_and_split_secrets_are_safe_before_filtering(tmp_path, chunk):
    cache = ContextCache(tmp_path / "cache.db")
    raw = b"hello\r\nbad \xff\nsecret gh\x1b[31mp_" + b"A" * 36 + b"\nnext\nlast without newline"
    result = capture(cache, raw, chunk=chunk, stderr="ż🙂\r\n".encode())
    filtered = filters.filter_execution(result, cache=cache)
    combined = "".join(view.text for view in filtered.views)
    assert "A" * 36 not in combined and "\x1b" not in combined and "\r" not in combined
    assert "hello\nbad �" in filtered.views[0].text
    assert "last without newline" in filtered.views[0].text
    assert "ż🙂" in filtered.views[1].text
    assert all(stream.recovery.complete for stream in filtered.result.streams)
    assert b"A" * 36 not in cache.db_path.read_bytes()


def test_oversized_line_never_becomes_complete_filtered_recovery(tmp_path):
    cache = ContextCache(tmp_path / "cache.db")
    result = capture(cache, b"x" * (20 * 1024))
    filtered = filters.filter_execution(result, cache=cache)
    assert filtered.views[0].note == "incomplete_capture"
    assert not filtered.result.capture.complete
    assert not filtered.result.streams[0].recovery.complete


def test_unknown_formats_do_not_read_or_rewrite_cache(tmp_path, monkeypatch):
    cache = ContextCache(tmp_path / "cache.db")
    result = capture(cache, b"original\n", command="custom-tool")

    def forbidden(*args, **kwargs):
        raise AssertionError("unsupported formats must use original preview")

    monkeypatch.setattr(cache, "iter_artifact", forbidden)
    monkeypatch.setattr(cache, "artifact_metadata", forbidden)
    filtered = filters.filter_execution(result, cache=cache)
    assert filtered.views[0].text == result.streams[0].capture.text
    assert filtered.views[0].note == "unsupported_format"


def test_large_inputs_and_scan_caps_fall_back_truthfully(tmp_path, monkeypatch):
    cache = ContextCache(tmp_path / "cache.db")
    result = capture(cache, b"safe line\n" * 100)
    monkeypatch.setattr(filters, "SCAN_BYTES", 64)
    filtered = filters.filter_execution(result, cache=cache)
    assert filtered.views[0].note == "input_limit"
    assert filtered.result.streams[0].recovery.complete
    assert filtered.views[0].text == result.streams[0].capture.text


@pytest.mark.parametrize(
    "failure", ["filter", "recognizer", "tokenizer", "output", "deadline", "cancel"]
)
@pytest.mark.parametrize("code", [0, 7])
def test_filter_failures_do_not_replace_exit_or_recovery(tmp_path, monkeypatch, failure, code):
    cache = ContextCache(tmp_path / "cache.db")
    result = capture(cache, b"E AssertionError: diagnostic\n", code=code)
    cancel = threading.Event()

    def failed(*args, **kwargs):
        raise RuntimeError("PRIVATE_FILTER_OR_TOKENIZER_ERROR")

    if failure == "filter":
        monkeypatch.setattr(filters.safe_filter, "_compact_pytest_diagnostic", failed)
    elif failure == "recognizer":
        monkeypatch.setattr(filters.safe_filter, "_pytest_command", failed)
    elif failure == "tokenizer":
        monkeypatch.setattr(filters.safe_filter, "count_tokens", failed)
        monkeypatch.setattr(filters.safe_filter, "count_model_tokens", failed)
    elif failure == "output":
        monkeypatch.setattr(filters, "OUTPUT_BYTES", 8)
    elif failure == "deadline":
        clock = iter([0.0, 100.0])
        monkeypatch.setattr(filters, "time", SimpleNamespace(monotonic=lambda: next(clock, 100.0)))
    else:
        cancel.set()
    filtered = filters.filter_execution(result, cache=cache, cancel_event=cancel)
    assert (
        filtered.result.returncode == code
        and filtered.result.termination_reason is TerminationReason.EXITED
    )
    assert filtered.result.streams[0].recovery.complete
    assert "PRIVATE_FILTER_OR_TOKENIZER_ERROR" not in repr(filtered)
    if failure == "tokenizer":
        assert filtered.views[0].state == "pytest"  # No tokenizer is required at all.
    else:
        assert filtered.views[0].text == result.streams[0].capture.text
        assert filtered.views[0].note != "redacted_ccr"


@pytest.mark.parametrize("failure", ["busy", "io", "full", "permission", "corrupt", "missing"])
def test_filter_storage_failures_invalidate_stale_recovery_without_hiding_exit(
    tmp_path, monkeypatch, failure
):
    cache = ContextCache(tmp_path / "cache.db")
    result = capture(cache, b"E AssertionError\n")
    original = cache.iter_artifact

    def broken(ref):
        if failure == "permission":
            raise PermissionError("PRIVATE_PERMISSION_ERROR")
        if failure == "missing":
            raise filters.ArtifactError("missing")
        if failure == "corrupt":
            # Fail after a safe prefix: no candidate may be published before
            # aggregate integrity verification finishes.
            yield next(original(ref))
            raise filters.ArtifactError("corrupt")
        raise sqlite3.OperationalError(
            {
                "busy": "database is locked",
                "io": "disk I/O error",
                "full": "database or disk is full",
            }[failure]
        )

    monkeypatch.setattr(cache, "iter_artifact", broken)
    filtered = filters.filter_execution(result, cache=cache)
    assert (
        filtered.result.returncode == 1
        and filtered.result.termination_reason is TerminationReason.EXITED
    )
    assert not filtered.result.capture.complete
    assert filtered.result.streams[0].recovery.available is not True
    assert filtered.result.streams[0].recovery.complete is not True
    assert filtered.result.issues[0].phase == "storage"
    assert filtered.views[0].note == "recovery_unavailable"


def test_full_text_helper_is_never_called_above_its_input_cap(tmp_path, monkeypatch):
    cache = ContextCache(tmp_path / "cache.db")
    data = b"tests/test_ok.py::test_ok PASSED\n" * 2500 + b"E AssertionError: end\n"
    result = capture(cache, data)

    def forbidden(*args):
        raise AssertionError("whole-text helper is forbidden for this input size")

    monkeypatch.setattr(filters.safe_filter, "_compact_pytest_diagnostic", forbidden)
    filtered = filters.filter_execution(result, cache=cache)
    assert filtered.views[0].state == "pytest" and "end" in filtered.views[0].text
    assert "passing tests" in filtered.views[0].text


def test_cancel_during_artifact_scan_discards_candidate(tmp_path, monkeypatch):
    cache = ContextCache(tmp_path / "cache.db")
    result = capture(cache, b"safe line\n" * 1000)
    original = cache.iter_artifact
    cancel = threading.Event()

    def read(ref):
        for fragment in original(ref):
            cancel.set()
            yield fragment

    monkeypatch.setattr(cache, "iter_artifact", read)
    filtered = filters.filter_execution(result, cache=cache, cancel_event=cancel)
    assert filtered.views[0].note == "cancelled"
    assert filtered.result.streams[0].recovery.complete  # Completed before cancellation.


@pytest.mark.parametrize("reason", [TerminationReason.TIMED_OUT, TerminationReason.CANCELLED])
def test_filters_do_not_touch_incomplete_capture_or_mask_cleanup_issues(
    tmp_path, monkeypatch, reason
):
    from usagetrim.core.execution_contract import ExecutionIssue

    cache = ContextCache(tmp_path / "cache.db")
    result = capture(cache, b"ready\n", code=0)
    result = replace(
        result,
        termination_reason=reason,
        capture=replace(result.capture, complete=False),
        streams=tuple(
            replace(
                stream,
                capture=replace(stream.capture, complete=False),
                recovery=replace(stream.recovery, complete=False),
            )
            for stream in result.streams
        ),
        issues=(ExecutionIssue("cleanup", "forced_termination_failed", "safe category"),),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("incomplete capture must not be reinterpreted by a filter")

    monkeypatch.setattr(cache, "iter_artifact", forbidden)
    filtered = filters.filter_execution(result, cache=cache)
    assert filtered.result.termination_reason is reason and filtered.result.returncode == 0
    assert filtered.result.issues == result.issues
    assert all(view.note == "incomplete_capture" for view in filtered.views)


def test_filtering_does_not_duplicate_ccr_or_require_tokenizers(tmp_path, monkeypatch):
    cache = ContextCache(tmp_path / "cache.db")
    result = capture(cache, b"tests/test_ok.py::test_ok PASSED\n" * 20)

    def forbidden(*args, **kwargs):
        raise AssertionError("display filtering must neither store snapshots nor tokenize")

    monkeypatch.setattr(cache, "store", forbidden)
    monkeypatch.setattr(filters.safe_filter, "count_tokens", forbidden)
    monkeypatch.setattr(filters.safe_filter, "count_model_tokens", forbidden)
    with sqlite3.connect(cache.db_path) as conn:
        before = conn.execute("SELECT ref_id, source FROM output_cache ORDER BY ref_id").fetchall()
    filtered = filters.filter_execution(result, cache=cache)
    assert filtered.views[0].state == "pytest"
    with sqlite3.connect(cache.db_path) as conn:
        after = conn.execute("SELECT ref_id, source FROM output_cache ORDER BY ref_id").fetchall()
    assert after == before
    assert len(before) == 2
    assert all(source == "execution-artifact" for _, source in before)
