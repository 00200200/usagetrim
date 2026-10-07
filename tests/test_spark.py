"""Tests for Apache Spark & PySpark executor log compactor & task skew detector."""

from __future__ import annotations

from usagetrim.core.specialized import (
    author_spark_log_fixture,
    auto_specialize_command_output,
    filter_spark,
)
from usagetrim.filters import filter_spark as fs1
from usagetrim.filters.spark import filter_spark_log as fs2
from usagetrim.metrics.tokenizer import count_tokens


def test_filter_spark_collapses_verbose_executor_logs():
    raw = author_spark_log_fixture(has_error=False, has_skew=False, num_tasks=50)
    compacted = filter_spark(raw)

    assert "[UsageTrim:" in compacted
    assert "TaskSetManager & BlockManager events collapsed across 2 stage(s)" in compacted
    assert "=== Spark Stage Task Distribution & Skew Summary ===" in compacted

    # Verbose chatter is removed
    assert "Starting task" not in compacted
    assert "Finished task" not in compacted
    assert "HeartbeatReceiver: Received heartbeat" not in compacted
    assert "BlockManagerInfo: Added broadcast" not in compacted


def test_filter_spark_token_reduction_exceeds_90_percent():
    raw = author_spark_log_fixture(has_error=False, has_skew=False, num_tasks=100)
    compacted = filter_spark(raw)

    raw_tokens = count_tokens(raw).avg
    compacted_tokens = count_tokens(compacted).avg
    reduction_pct = (raw_tokens - compacted_tokens) / raw_tokens * 100.0

    assert reduction_pct >= 90.0, f"Expected >= 90% reduction, got {reduction_pct:.1f}%"


def test_filter_spark_detects_severe_task_skew():
    raw = author_spark_log_fixture(has_error=False, has_skew=True, num_tasks=50)
    compacted = filter_spark(raw, skew_threshold=3.0)

    assert "⚠️ Severe Skew:" in compacted
    assert "executor" in compacted
    assert "median" in compacted
    assert "TID" in compacted


def test_filter_spark_balanced_stages():
    raw = author_spark_log_fixture(has_error=False, has_skew=False, num_tasks=30)
    compacted = filter_spark(raw)

    assert "[Balanced]" in compacted
    assert "⚠️ Severe Skew:" not in compacted


def test_filter_spark_preserves_py4j_and_python_tracebacks_verbatim():
    raw = author_spark_log_fixture(has_error=True, has_skew=False, num_tasks=20)
    compacted = filter_spark(raw)

    # Collapsed banner present
    assert "[UsageTrim:" in compacted

    # 100% preservation of Py4J Java exceptions and stack traces
    assert "Py4JJavaError: An error occurred while calling o42.count." in compacted
    assert "org.apache.spark.SparkException: Job aborted due to stage failure" in compacted
    assert "java.lang.OutOfMemoryError: Java heap space" in compacted
    assert "\tat java.base/java.util.Arrays.copyOf(Arrays.java:3745)" in compacted
    assert "\t... 32 more" in compacted

    # 100% preservation of Python tracebacks
    assert "Traceback (most recent call last):" in compacted
    assert 'File "/app/script.py", line 42, in <module>' in compacted
    assert 'df.groupBy("user_id").agg(count("*")).show()' in compacted
    assert "pyspark.errors.exceptions.captured.PySparkException" in compacted


def test_auto_specialize_spark_commands():
    fixture = author_spark_log_fixture(has_error=False, has_skew=False, num_tasks=20)

    spark_commands = [
        "spark-submit --master yarn --deploy-mode client app.py",
        "pyspark",
        "spark-shell -i script.scala",
        "spark-sql -e 'SELECT 1'",
        "/opt/spark/bin/spark-submit main.py",
    ]

    for cmd in spark_commands:
        compacted = auto_specialize_command_output(cmd, fixture)
        assert compacted is not None, f"Failed to specialize command: {cmd}"
        assert "[UsageTrim:" in compacted
        assert "=== Spark Stage Task Distribution & Skew Summary ===" in compacted


def test_auto_specialize_spark_output_auto_detection():
    fixture = author_spark_log_fixture(has_error=False, has_skew=False, num_tasks=20)

    # Even with generic command like "cat spark.log" or "docker logs spark", it detects spark logs
    compacted = auto_specialize_command_output("cat /var/log/spark.log", fixture)
    assert compacted is not None
    assert "[UsageTrim:" in compacted

    # Unrelated output should not be specialized as Spark
    unrelated = "line 1\nline 2\nhello world\n"
    assert auto_specialize_command_output("spark-submit app.py", unrelated) is None
    assert auto_specialize_command_output("cat test.txt", unrelated) is None


def test_spark_filters_import_and_aliases():
    from usagetrim.core.specialized import author_spark_log_fixture as asf3
    from usagetrim.core.specialized import filter_spark as fs3
    from usagetrim.core.specialized import filter_spark_log as fsl3
    from usagetrim.filters import author_spark_log_fixture as asf1
    from usagetrim.filters import filter_spark_log as fsl1
    from usagetrim.filters.spark import author_spark_log_fixture as asf2
    from usagetrim.filters.spark import filter_spark as fs_pure

    assert fs1 is fs2
    assert fs1 is fs3
    assert fs1 is fs_pure
    assert fsl1 is fsl3
    assert asf1 is asf2
    assert asf1 is asf3
