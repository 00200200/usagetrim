"""Offline measurements of individual command-output fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from tempfile import TemporaryDirectory
from time import perf_counter

from usagetrim.core.cache import ContextCache
from usagetrim.core.safe_filter import recovery_notice, safe_compact_output
from usagetrim.core.specialized import auto_specialize_command_output
from usagetrim.metrics.tokenizer import count_tokens


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    raw_output: str
    command: str
    required_signals: tuple[str, ...] = ()
    exit_code: int | None = None


@dataclass(frozen=True)
class BenchmarkResult:
    case_id: str
    raw_tokens: int
    output_tokens: int
    reduction_pct: float
    latency_ms: float
    signals_preserved: bool
    missing_signals: tuple[str, ...]
    exact_recovery: bool


@dataclass(frozen=True)
class BenchmarkSuiteResult:
    results: tuple[BenchmarkResult, ...]
    raw_tokens: int
    output_tokens: int
    reduction_pct: float
    latency_ms: float
    signals_preserved: bool
    exact_recovery: bool


def benchmark_case(
    case_id: str,
    raw_output: str,
    command: str,
    required_signals: Sequence[str],
    *,
    exit_code: int | None = None,
) -> BenchmarkResult:
    """Measure existing filters without running commands or recording telemetry.

    Latency measures only the existing filtering path, including any cache or
    tokenization work performed by the production filters themselves.
    exact_recovery checks recoverability of the original fixture through the
    harness's isolated ContextCache, independently of signal preservation by the
    filter. It compares UTF-8 bytes without normalization; recognized secrets
    cause this check to fail because ContextCache deliberately redacts them.
    Recovery notices are counted internally; no disposable reference is returned.
    Cache isolation temporarily changes the process environment; run sequentially.
    """
    raw_tokens = count_tokens(raw_output).openai
    previous_cache = os.environ.get("USAGETRIM_CACHE_DIR")
    with TemporaryDirectory(prefix="usagetrim-benchmark-") as directory:
        try:
            os.environ["USAGETRIM_CACHE_DIR"] = directory
            cache = ContextCache()
            ref_id = cache.store(raw_output, source=f"benchmark:{case_id}")

            start = perf_counter()
            compacted = auto_specialize_command_output(command, raw_output)
            if compacted is None:
                compacted = safe_compact_output(raw_output, command=command, exit_code=exit_code)
            latency_ms = (perf_counter() - start) * 1000
            filtered_output = compacted
            if compacted != raw_output:
                notice = recovery_notice(ref_id)
                # Match a complete notice line, not an incidental reference in a log.
                if notice not in compacted.splitlines(keepends=True):
                    separator = "" if compacted.endswith(("\n", "\r")) else "\n"
                    compacted += separator + notice
            recovered = cache.retrieve(ref_id)
        finally:
            if previous_cache is None:
                os.environ.pop("USAGETRIM_CACHE_DIR", None)
            else:
                os.environ["USAGETRIM_CACHE_DIR"] = previous_cache

    # Check diagnostics separately from the runner's appended recovery metadata.
    # A signal must have survived filtering, rather than appearing in the notice.
    missing_signals = tuple(signal for signal in required_signals if signal not in filtered_output)
    output_tokens = count_tokens(compacted).openai
    reduction_pct = 100 * (raw_tokens - output_tokens) / raw_tokens if raw_tokens else 0.0
    return BenchmarkResult(
        case_id=case_id,
        raw_tokens=raw_tokens,
        output_tokens=output_tokens,
        reduction_pct=reduction_pct,
        latency_ms=latency_ms,
        signals_preserved=not missing_signals,
        missing_signals=missing_signals,
        exact_recovery=recovered.encode("utf-8") == raw_output.encode("utf-8"),
    )


def benchmark_suite(cases: Iterable[BenchmarkCase]) -> BenchmarkSuiteResult:
    """Run cases sequentially in input order and aggregate their measurements.

    Reduction is calculated from token totals, not averaged case percentages.
    Latency is the sum of measured filter times, not the suite's wall-clock time.
    An empty suite has zero totals and reduction, and both checks pass vacuously.
    Exceptions from benchmark_case propagate to the caller.
    """
    results = tuple(
        benchmark_case(
            case.case_id,
            case.raw_output,
            case.command,
            case.required_signals,
            exit_code=case.exit_code,
        )
        for case in cases
    )
    raw_tokens = sum(result.raw_tokens for result in results)
    output_tokens = sum(result.output_tokens for result in results)
    return BenchmarkSuiteResult(
        results=results,
        raw_tokens=raw_tokens,
        output_tokens=output_tokens,
        reduction_pct=100 * (raw_tokens - output_tokens) / raw_tokens if raw_tokens else 0.0,
        latency_ms=sum(result.latency_ms for result in results),
        signals_preserved=all(result.signals_preserved for result in results),
        exact_recovery=all(result.exact_recovery for result in results),
    )


def benchmark_bounded_case(case: BenchmarkCase) -> dict:
    """Offline fixture replay, not subprocess/RSS evidence or a new baseline.

    Compare bounded display with the existing safe filter on the same normalized
    input, only below 64 KiB. The legacy-safe comparison explicitly uses generic
    estimates, not the specialized CLI/MCP pipeline or a provider tokenizer.
    """
    import hashlib

    from usagetrim.core.bounded_filter import SMALL_TEXT_BYTES, filter_execution
    from usagetrim.core.execution_contract import (
        CaptureView,
        ExecutionMode,
        ExecutionRequest,
        ExecutionResult,
        RecoveryInfo,
        StreamCapture,
        TerminationReason,
    )
    from usagetrim.core.stream_redactor import StreamingRedactor

    if len(case.raw_output) > SMALL_TEXT_BYTES:
        raise ValueError("quality replay accepts only small fixtures; use the resource harness")
    data = case.raw_output.encode("utf-8")
    if len(data) > SMALL_TEXT_BYTES:
        raise ValueError("quality fixture exceeds the 64 KiB UTF-8 input limit")
    previous_cache = os.environ.get("USAGETRIM_CACHE_DIR")
    with TemporaryDirectory(prefix="usagetrim-bounded-quality-") as directory:
        try:
            os.environ["USAGETRIM_CACHE_DIR"] = directory
            cache = ContextCache()
            writer = cache.begin_artifact(stream="stdout")
            stderr = cache.begin_artifact(stream="stderr", execution_id=writer.execution_id)
            for start in range(0, len(data), 8192):
                writer.append(data[start : start + 8192])
            writer.finish()
            stderr.finish()
            streams = []
            for item in (writer, stderr):
                view = cache.read_artifact(item.ref_id, max_bytes=4096)
                streams.append(
                    StreamCapture(
                        item.stream.value,
                        CaptureView(view.text, "preview", "separate", view.metadata.complete),
                        RecoveryInfo(
                            item.ref_id,
                            available=view.metadata.available,
                            complete=view.metadata.complete,
                        ),
                        view.response_truncated,
                    )
                )
            # Exit status is manifest metadata, never a measured child result.
            fixture_result = ExecutionResult(
                ExecutionRequest(ExecutionMode.SHELL, shell_command=case.command),
                TerminationReason.EXITED,
                case.exit_code if case.exit_code is not None else 0,
                capture=CaptureView(
                    text_scope="preview",
                    stream_layout="separate",
                    complete=all(s.capture.complete for s in streams),
                ),
                execution_id=writer.execution_id,
                streams=tuple(streams),
            )
            started = perf_counter()
            filtered = filter_execution(fixture_result, cache=cache)
            latency = (perf_counter() - started) * 1000
            display = filtered.views[0].text
            missing = [signal for signal in case.required_signals if signal not in display]
            expected, recovered = hashlib.sha256(), hashlib.sha256()
            redactor = StreamingRedactor()
            for record in redactor.feed(data):
                expected.update(record.encode("utf-8"))
            for record in redactor.finish():
                expected.update(record.encode("utf-8"))
            for fragment in cache.iter_artifact(writer.ref_id):
                recovered.update(fragment.data)
            legacy = None
            metadata = cache.artifact_metadata(writer.ref_id)
            if metadata.complete and metadata.stored_bytes <= SMALL_TEXT_BYTES:
                original = cache.read_artifact(writer.ref_id, max_bytes=SMALL_TEXT_BYTES).text
                started = perf_counter()
                safe = safe_compact_output(original, command=case.command, model="generic")
                legacy = {
                    "scope": "existing safe filter only, same redacted-normalized input; generic estimate",
                    "display_bytes_including_notice": len(safe.encode("utf-8")),
                    "latency_ms": (perf_counter() - started) * 1000,
                    "missing_signals": [
                        signal for signal in case.required_signals if signal not in safe
                    ],
                }
            return {
                "case_id": case.case_id,
                "source_sha256": hashlib.sha256(data).hexdigest(),
                "scope": "fixture replay; no child process/RSS/live progress measurement",
                "manifest_exit_code": case.exit_code,
                "raw_bytes": len(data),
                "display_body_bytes": len(display.encode("utf-8")),
                "filter_state": filtered.views[0].state,
                "coverage": filtered.views[0].coverage,
                "filter_note": filtered.views[0].note,
                "latency_ms": latency,
                "signals_preserved": not missing,
                "missing_signals": missing,
                "exact_redacted_normalized_recovery": (
                    expected.digest() == recovered.digest() and not redactor.incomplete
                ),
                "legacy_safe_filter": legacy,
            }
        finally:
            if previous_cache is None:
                os.environ.pop("USAGETRIM_CACHE_DIR", None)
            else:
                os.environ["USAGETRIM_CACHE_DIR"] = previous_cache
