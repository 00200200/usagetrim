"""Aggregate profiler correctness; no large input or performance thresholds."""

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def profiler_module():
    path = Path(__file__).resolve().parents[1] / "scripts/execution_profile.py"
    spec = importlib.util.spec_from_file_location("execution_profile_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_aggregate_storage_has_fixed_cardinality_and_never_keeps_exception_text(profiler_module):
    profiler = profiler_module.ExecutionProfile()
    for _ in range(10000):
        profiler.record("fixed_stage", 0.001, bytes_count=8)

    def broken():
        raise ValueError("PRIVATE_SECRET_DIAGNOSTIC")

    with pytest.raises(ValueError):
        profiler._timed("failure", broken)()
    result = profiler.report()
    assert len(result["stages"]) == 2
    assert result["stages"]["fixed_stage"]["calls"] == 10000
    assert result["stages"]["fixed_stage"]["bytes"] == 80000
    assert result["stages"]["failure"]["errors"] == 1
    assert "PRIVATE_SECRET_DIAGNOSTIC" not in json.dumps(result)


@pytest.mark.parametrize("fail", [False, True])
def test_profiler_restores_hooks_and_keeps_real_transactions_and_redaction(
    profiler_module, tmp_path, fail
):
    from usagetrim.core import artifact_quota, execution_artifacts, execution_runner
    from usagetrim.core.cache import ContextCache

    cache = ContextCache(tmp_path / "cache.db")
    connect = sqlite3.connect
    append = execution_artifacts.ArtifactWriter.append
    queue = execution_runner.queue.Queue
    reserve = execution_artifacts._reserve
    usage = artifact_quota.usage
    profiler = profiler_module.ExecutionProfile(cache)
    ref = None
    try:
        with profiler:
            with cache.begin_artifact(stream="stdout") as writer:
                ref = writer.ref_id
                writer.append(b"safe line\nghp_" + b"A" * 36 + b"\n")
                metadata = writer.finish()
                assert metadata.complete
            events = execution_runner.queue.Queue(maxsize=1)
            events.put(b"PRIVATE_QUEUE_PAYLOAD")
            assert events.get() == b"PRIVATE_QUEUE_PAYLOAD"
            if fail:
                raise RuntimeError("PRIVATE_EXCEPTION")
    except RuntimeError:
        assert fail
    assert sqlite3.connect is connect
    assert execution_artifacts.ArtifactWriter.append is append
    assert execution_runner.queue.Queue is queue
    assert execution_artifacts._reserve is reserve and artifact_quota.usage is usage
    text = cache.read_artifact(ref).text
    assert text == "safe line\n[REDACTED_CREDENTIAL_LINE]\n"
    result = profiler.report()
    assert result["stages"]["writer.append"]["calls"] == 1
    assert result["stages"]["sqlite.commit"]["calls"] > 0
    assert result["queue_high_water_events"] == 1
    assert result["sqlite_settings"]["foreign_keys"] == 1
    assert "PRIVATE" not in json.dumps(result)
    assert "A" * 36 not in json.dumps(result)
