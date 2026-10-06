"""Offline measurements of individual command-output fixtures."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from tempfile import TemporaryDirectory
from time import perf_counter

from usagetrim.core.cache import ContextCache
from usagetrim.core.safe_filter import recovery_notice, safe_compact_output
from usagetrim.core.specialized import auto_specialize_command_output
from usagetrim.metrics.tokenizer import count_tokens


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
