import os

import pytest

from usagetrim.core import benchmark
from usagetrim.core.benchmark import benchmark_case
from usagetrim.core.cache import ContextCache
from usagetrim.core.safe_filter import recovery_notice, safe_compact_output
from usagetrim.core.specialized import auto_specialize_command_output
from usagetrim.metrics.tokenizer import count_tokens


def test_pytest_uses_safe_filter_and_counts_recovery_notice():
    raw = "".join(f"tests/test_api.py::test_{i} PASSED [ 80%]\n" for i in range(80))
    raw += "=== FAILURES ===\nFAILED tests/test_api.py::test_failure - AssertionError\n"
    result = benchmark_case("pytest-failure", raw, "pytest -v", ["AssertionError"], exit_code=1)

    assert result.case_id == "pytest-failure"
    expected_output = safe_compact_output(raw, command="pytest -v", exit_code=1)
    assert "80 passing tests" in expected_output
    assert result.signals_preserved and result.missing_signals == ()
    assert result.exact_recovery
    assert not hasattr(result, "ref_id")
    assert not hasattr(result, "text")
    assert result.raw_tokens == count_tokens(raw).openai
    assert result.output_tokens == count_tokens(expected_output).openai
    assert (
        result.reduction_pct == 100 * (result.raw_tokens - result.output_tokens) / result.raw_tokens
    )
    assert result.reduction_pct > 0
    assert result.latency_ms >= 0


def test_cargo_uses_specialized_filter():
    raw = "running 80 tests\n"
    raw += "".join(f"test module::case_{i} ... ok\n" for i in range(80))
    raw += "test result: ok. 80 passed; 0 failed\n"
    specialized = auto_specialize_command_output("cargo test", raw)
    result = benchmark_case("cargo-pass", raw, "cargo test", ["80 passed"], exit_code=0)

    assert specialized is not None and specialized != raw
    ref_id = ContextCache().store(raw)
    expected_output = specialized + ("" if specialized.endswith(("\n", "\r")) else "\n")
    expected_output += recovery_notice(ref_id)
    assert result.output_tokens == count_tokens(expected_output).openai
    assert result.signals_preserved and result.exact_recovery


@pytest.mark.parametrize("configured_cache", [True, False])
def test_recovery_preserves_bytes_and_isolates_user_cache(monkeypatch, configured_cache):
    cache = ContextCache()
    user_ref = cache.store("existing user output")
    previous_cache = os.environ["USAGETRIM_CACHE_DIR"]
    if not configured_cache:
        monkeypatch.delenv("USAGETRIM_CACHE_DIR")
    raw = "\x1b[31mUnicode diagnostic: café λ\x1b[0m\r\ntrailing spaces  "
    result = benchmark_case("unknown", raw, "custom-tool", ["café λ", "missing diagnostic"])

    assert result.exact_recovery
    assert not result.signals_preserved
    assert result.missing_signals == ("missing diagnostic",)
    assert cache.retrieve(user_ref) == "existing user output"
    assert cache.get_stats()["count"] == 1
    assert os.environ.get("USAGETRIM_CACHE_DIR") == (previous_cache if configured_cache else None)


def test_passthrough_has_no_recovery_notice_overhead():
    result = benchmark_case("passthrough", "unchanged output\n", "custom-tool", [])

    assert result.output_tokens == result.raw_tokens
    assert result.reduction_pct == 0
    assert result.exact_recovery is True


def test_redaction_prevents_claiming_exact_raw_recovery():
    raw = "OPENAI_API_KEY=sk-proj-" + "a" * 30
    result = benchmark_case("redacted", raw, "custom-tool", [])

    assert not result.exact_recovery


def test_filter_failure_restores_cache_environment(monkeypatch):
    previous_cache = os.environ["USAGETRIM_CACHE_DIR"]

    def fail_filter(*args):
        raise RuntimeError("filter failed")

    monkeypatch.setattr(benchmark, "auto_specialize_command_output", fail_filter)
    with pytest.raises(RuntimeError, match="filter failed"):
        benchmark_case("broken", "output", "custom-tool", [])
    assert os.environ["USAGETRIM_CACHE_DIR"] == previous_cache


@pytest.mark.parametrize("use_safe", [False, True])
def test_latency_excludes_harness_notice_and_checks_exact_notice(monkeypatch, use_safe):
    raw = "original diagnostic\n"
    ref_id = ContextCache().store(raw)
    filtered = f"incidental reference: {ref_id}\n"
    clock = [0.0]

    def specialized(command, output):
        clock[0] += 0.002
        return None if use_safe else filtered

    def safe(output, *, command, exit_code):
        assert exit_code == 1
        clock[0] += 0.003
        return filtered

    def notice(reference):
        clock[0] += 1.0
        return recovery_notice(reference)

    monkeypatch.setattr(benchmark, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(benchmark, "auto_specialize_command_output", specialized)
    monkeypatch.setattr(benchmark, "safe_compact_output", safe)
    monkeypatch.setattr(benchmark, "recovery_notice", notice)
    result = benchmark_case("timed", raw, "custom-tool", ["original diagnostic"], exit_code=1)

    assert result.latency_ms == pytest.approx(5.0 if use_safe else 2.0)
    assert result.output_tokens == count_tokens(filtered + recovery_notice(ref_id)).openai
    assert result.exact_recovery and not result.signals_preserved
