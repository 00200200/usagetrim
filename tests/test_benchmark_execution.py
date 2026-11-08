"""Small execution resource-harness checks; large profiles are never automatic."""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def harness():
    path = Path(__file__).resolve().parents[1] / "scripts/benchmark_execution.py"
    spec = importlib.util.spec_from_file_location("execution_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("profile", [False, True])
def test_small_isolated_resource_harness_measures_processes_separately(harness, profile):
    result = harness.measure_runner(65536, timeout=60, profile=profile)
    assert result["measurement_valid"] and result["worker_returncode"] == 0
    assert result["generated_bytes"] == result["retained_bytes"] == 65536
    assert result["capture_complete"] and result["returncode"] == 0
    assert result["error_count"] == 0 and result["stored_fragments"] > 0
    assert result["runner_pid"] != result["producer_pid"]
    assert result["runner_pid"] != os.getpid()
    assert result["first_observation_seconds"] is not None
    assert result["first_observation_seconds"] <= result["wall_seconds"]
    assert result["artifact_finalization_seconds"] >= 0
    assert result["retrieval_latency_seconds"] >= 0
    # No speed thresholds or assumptions about the relative process RSS sizes.
    if sys.platform in {"linux", "darwin", "win32"}:
        assert result["runner_peak_rss_bytes"] > 0
        assert result["producer_peak_rss_bytes"] > 0
        assert result["warmup_peak_rss_bytes"] > 0
    assert not harness.rss_growth([result])["measured"]
    if profile:
        details = result["profile"]
        stages = details["stages"]
        assert stages["writer.append"]["bytes"] == 65536
        assert stages["writer.observe"]["calls"] == stages["writer.append"]["calls"]
        assert stages["sqlite.commit"]["calls"] >= (
            stages["writer.append"]["calls"] + result["stored_fragments"]
        )
        assert stages["sqlite.connect"]["calls"] >= stages["sqlite.commit"]["calls"]
        assert stages["redaction_and_assembly_residual"]["seconds"] >= 0
        assert details["queue_high_water_events"] <= result["parameters"]["limits"]["queue_chunks"]
        assert details["sqlite_settings"]["foreign_keys"] == 1
        assert result["parameters"]["profile_enabled"] is True
    else:
        assert "profile" not in result
        assert "profile_enabled" not in result["parameters"]


@pytest.mark.parametrize("sizes", [["--size-mib", "1024"], ["--sizes-mib", "16", "1024"]])
def test_gib_requires_explicit_opt_in_before_any_measurement(harness, monkeypatch, sizes):
    def forbidden(*args, **kwargs):
        raise AssertionError("A missing opt-in must never launch a benchmark")

    monkeypatch.setattr(harness, "measure_runner", forbidden)
    monkeypatch.setattr(sys, "argv", ["benchmark", "--entry-point", "runner", *sizes])
    with pytest.raises(SystemExit) as error:
        harness.main()
    assert error.value.code == 2


def test_memory_target_requires_valid_comparable_samples(harness):
    def sample(
        size, rss, *, valid=True, method="test process peak", parameters=None, environment=None
    ):
        return {
            "generated_bytes": size * harness.MIB,
            "runner_peak_rss_bytes": rss,
            "measurement_valid": valid,
            "rss_method": method,
            "parameters": parameters or {"identical": True},
            "environment": environment or {"synthetic": "same environment"},
        }

    assert harness.rss_growth([])["within_64_mib"] is None
    small = sample(16, 20 * harness.MIB)
    large = sample(1024, 24 * harness.MIB)
    target = harness.rss_growth([small, large])
    assert target["measured"] and target["delta_bytes"] == 4 * harness.MIB
    assert target["within_64_mib"]
    assert not harness.rss_growth([small, sample(1024, 100 * harness.MIB)])["within_64_mib"]
    assert not harness.rss_growth([small, sample(1024, None)])["measured"]
    assert not harness.rss_growth([small, small, large])["measured"]
    assert not harness.rss_growth([small, sample(1024, -1)])["measured"]
    assert not harness.rss_growth([small, sample(1024, 1, valid=False)])["measured"]
    assert not harness.rss_growth([small, sample(1024, 1, method="different")])["measured"]
    assert not harness.rss_growth([small, sample(1024, 1, parameters={"different": True})])[
        "measured"
    ]
    assert not harness.rss_growth([small, sample(1024, 1, environment={"different": True})])[
        "measured"
    ]


def test_bounded_report_reader_rejects_oversized_json(harness, tmp_path):
    path = tmp_path / "report.json"
    path.write_bytes(b" " * 65537)
    with pytest.raises(ValueError, match="exceeds"):
        harness._read_report(path)


@pytest.mark.parametrize("size", [0, -1, True, 2 * 1024**3])
def test_resource_harness_rejects_invalid_sizes_before_launch(harness, size):
    with pytest.raises(ValueError):
        harness.measure_runner(size)


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan")])
def test_resource_harness_rejects_unbounded_observer_timeouts(harness, timeout):
    with pytest.raises(ValueError):
        harness.measure_runner(65536, timeout=timeout)


@pytest.mark.parametrize("failure", ["notification", "force"])
def test_observer_faults_still_close_owned_tree_and_reap_worker(harness, monkeypatch, failure):
    from usagetrim.core import execution_process

    events = []

    class Pipe:
        def close(self):
            events.append("pipe_close")

    class Process:
        stdout, stderr = Pipe(), Pipe()

        def wait(self, *, timeout):
            assert timeout == 5
            events.append("wait")
            return 0

    class Tree:
        job = None

        def __init__(self):
            self.process = Process()

        def start(self, *args, **kwargs):
            return self.process

        def poll(self):
            return 0

        def force(self):
            events.append("force")
            if failure == "force":
                raise RuntimeError("Injected observer force failure")

        def close(self):
            events.append("tree_close")

    touch = Path.touch

    def failing_touch(path, *args, **kwargs):
        if failure == "notification" and path.name == "cancel":
            raise OSError("Injected notification failure")
        return touch(path, *args, **kwargs)

    monkeypatch.setattr(execution_process, "ProcessTree", Tree)
    monkeypatch.setattr(harness, "_read_report", lambda path: {"measurement_valid": True})
    monkeypatch.setattr(harness.shutil, "disk_usage", lambda path: SimpleNamespace(free=1 << 40))
    monkeypatch.setattr(Path, "touch", failing_touch)
    with pytest.raises(RuntimeError):
        harness.measure_runner(65536, timeout=60)
    assert events[-3:] == ["force", "tree_close", "wait"]


@pytest.mark.parametrize("corpus", ["ascii", "mixed"])
def test_redactor_microbenchmark_has_equivalent_incremental_hashes_without_sqlite(
    harness, monkeypatch, tmp_path, corpus
):
    from usagetrim.core.stream_redactor import StreamingRedactor

    spec = importlib.util.spec_from_file_location("micro_scalar", harness.REDACTOR_REFERENCE)
    reference = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reference)

    def forbidden(*args, **kwargs):
        raise AssertionError("Redactor microbenchmark must not open SQLite")

    monkeypatch.setattr(harness.sqlite3, "connect", forbidden)
    results = [
        harness._exercise_redactor(
            kind,
            10003,
            corpus,
            stop_file=tmp_path / "cancel",
            deadline=time.monotonic() + 60,
        )
        for kind in (reference.ScalarRedactor, StreamingRedactor)
    ]
    assert results[0] == results[1]
    assert results[0]["input_bytes"] == 10003
    assert len(results[0]["output_sha256"]) == 64
    assert sum(len(part) for part in harness._redactor_parts(10003, corpus)) == 10003
    assert max(map(len, harness._redactor_parts(10003, corpus))) <= 4096


def test_microbenchmark_comparison_requires_output_and_completeness_parity(harness):
    # Synthetic numbers test report logic only, not measured speed or RSS.
    before = {
        "implementation": "scalar",
        "measurement_valid": True,
        "input_bytes": 100,
        "input_sha256": "synthetic input",
        "corpus": "ascii",
        "feed_bytes": 4096,
        "warmup_bytes": 65536,
        "rss_method": "synthetic",
        "worker_peak_rss_bytes": 1000,
        "output_bytes": 100,
        "output_sha256": "synthetic output",
        "output_records": 3,
        "redaction_incomplete": False,
        "wall_seconds": 2,
        "scalar_reference_sha256": "fixture",
        "redactor_source_sha256": "source",
        "environment": {"synthetic": True},
    }
    after = before | {"implementation": "ascii", "wall_seconds": 1}
    comparison = harness.redactor_comparison([before, after])
    assert comparison["equivalent_output"] is True
    assert comparison["scalar_over_ascii_wall_ratio"] == 2
    assert harness.redactor_comparison([before])["equivalent_output"] is None
    mismatch = after | {"output_sha256": "different"}
    assert harness.redactor_comparison([before, mismatch])["equivalent_output"] is False
    mismatch = after | {"redaction_incomplete": True}
    assert harness.redactor_comparison([before, mismatch])["scalar_over_ascii_wall_ratio"] is None


def test_small_filter_resource_profile_reports_no_invented_progress(harness):
    result = harness.measure_runner(65536, timeout=60, filters=True)
    assert result["measurement_valid"] and result["capture_complete"]
    assert result["runner_pid"] != result["producer_pid"]
    assert result["parameters"]["filters_enabled"] is True
    assert result["filtering_seconds"] >= 0
    assert result["first_bounded_progress_seconds"] is None
    assert result["retained_disk_bytes"] >= result["retained_bytes"]
    assert result["display_body_bytes"] <= 2 * 16384
    assert all(view["note"] == "unsupported_format" for view in result["filter_views"])
    assert not harness.rss_growth([result])["measured"]
