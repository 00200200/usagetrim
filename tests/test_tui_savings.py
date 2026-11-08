"""SQLite savings aggregation is independent of Textual and tokenizer assets."""

import pytest

from usagetrim.core.telemetry import TelemetryStore
from usagetrim.tui.history import HistoryFilters, read_history
from usagetrim.tui.savings import read_savings

NOW = 1_700_000_123.5


def add_event(store, event_id, timestamp, raw=100, compact=40, **overrides):
    fields = {"operation": "read", "client": "codex", "project": "/repo"}
    fields.update(overrides)
    store.record(0, event_id=event_id, raw_openai=raw, compact_openai=compact, **fields)
    with store.connect() as conn:
        conn.execute("UPDATE events SET timestamp=? WHERE event_id=?", (timestamp, event_id))


@pytest.mark.parametrize(
    "period,count,step", [("24h", 24, 3600), ("7d", 7, 86400), ("all", 30, 86400)]
)
def test_savings_bucket_boundaries_and_empty_intervals(tmp_path, period, count, step):
    store = TelemetryStore(tmp_path / "telemetry.db")
    start = NOW - count * step
    for event_id, timestamp, raw, compact in (
        ("too-old", start - 0.25, 1000, 0),
        ("start", start, 100, 40),
        ("before-edge", start + step - 0.25, 200, 80),
        ("edge", start + step, 300, 100),
        ("now", NOW, 400, 40),
        ("future", NOW + 0.25, 1000, 0),
    ):
        add_event(store, event_id, timestamp, raw, compact)
    series = read_savings(store, HistoryFilters(time_range=period), now=NOW)
    assert series.start == start and series.end == NOW
    assert series.interval_seconds == step
    assert series.event_count == 4
    expected = [0] * count
    expected[0], expected[1], expected[-1] = 180, 200, 360
    assert series.saved_openai == tuple(expected)


def test_savings_clamps_net_after_sql_sums_and_matches_gain_eligibility(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    start = NOW - 24 * 3600
    add_event(store, "saving", start, raw=100, compact=20)
    add_event(store, "expansion", start + 1, raw=10, compact=90)
    add_event(store, "negative-bucket", start + 3600, raw=10, compact=30)
    add_event(store, "positive-bucket", start + 7200, raw=50, compact=15)
    add_event(store, "null-counters", start + 7201, raw=None, compact=None)
    add_event(store, "other-engine", start, raw=9999, compact=0, engine="none")
    add_event(store, "preview", start, raw=9999, compact=0, delivery="prepared")
    series = read_savings(store, HistoryFilters(time_range="24h"), now=NOW)
    assert series.event_count == 5
    # Not SUM(max(0, raw-compact)): the first two events cancel each other.
    assert series.saved_openai[:3] == (0, 0, 35)
    assert sum(series.saved_openai) == 35


@pytest.mark.parametrize(
    "filters,expected",
    [
        (HistoryFilters(), 240),
        (HistoryFilters(operation="read"), 180),
        (HistoryFilters(client="codex"), 180),
        (HistoryFilters(project="/repo"), 180),
        (HistoryFilters(operation="read", client="codex", project="/repo"), 60),
        (HistoryFilters(operation="Read"), 0),
    ],
)
def test_savings_respects_metadata_filters(tmp_path, filters, expected):
    store = TelemetryStore(tmp_path / "telemetry.db")
    add_event(store, "match", NOW)
    add_event(store, "other-operation", NOW, operation="diff")
    add_event(store, "other-client", NOW, client="claude")
    add_event(store, "other-project", NOW, project="/other")
    series = read_savings(store, filters, now=NOW)
    assert sum(series.saved_openai) == expected


@pytest.mark.parametrize("field", ["operation", "client", "project"])
def test_savings_uses_bound_filter_values(tmp_path, field):
    store = TelemetryStore(tmp_path / "telemetry.db")
    value = "żółć' OR 1=1 --"
    add_event(store, "literal", NOW, **{field: value})
    add_event(store, "other", NOW, raw=9999, compact=0)
    series = read_savings(store, HistoryFilters(**{field: value}), now=NOW)
    assert series.event_count == 1 and sum(series.saved_openai) == 60


@pytest.mark.parametrize("period,count", [("24h", 24), ("7d", 7), ("all", 30)])
def test_savings_empty_database_produces_zero_buckets(tmp_path, period, count):
    store = TelemetryStore(tmp_path / "telemetry.db")
    series = read_savings(store, HistoryFilters(time_range=period), now=NOW)
    assert series.saved_openai == (0,) * count
    assert series.event_count == 0


def test_savings_is_sql_aggregation_not_the_limited_history(tmp_path, monkeypatch):
    store = TelemetryStore(tmp_path / "telemetry.db")
    for index in range(200):
        add_event(store, str(index), NOW)
    statements = []
    original_connect = store.connect

    def connect():
        conn = original_connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store, "connect", connect)
    series = read_savings(store, HistoryFilters(time_range="24h"), now=NOW)
    assert series.event_count == 200 and series.saved_openai[-1] == 12000
    assert len(statements) == 1
    assert "SUM(raw_openai)" in statements[0]
    assert "GROUP BY bucket" in statements[0] and "LIMIT 24" in statements[0]
    assert len(read_history(store, HistoryFilters())) == 50


def test_all_time_chart_limits_thirty_days_without_limiting_history(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    add_event(store, "recent", NOW)
    add_event(store, "old", NOW - 31 * 86400)
    filters = HistoryFilters()
    series = read_savings(store, filters, now=NOW)
    assert len(series.saved_openai) == 30 and series.event_count == 1
    assert {row["event_id"] for row in read_history(store, filters)} == {"recent", "old"}


def test_savings_window_advances_on_refresh(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    start = NOW - 24 * 3600
    add_event(store, "expired-next-hour", start + 1)
    add_event(store, "retained", NOW)
    filters = HistoryFilters(time_range="24h")
    first = read_savings(store, filters, now=NOW)
    second = read_savings(store, filters, now=NOW + 3600)
    assert first.event_count == 2 and second.event_count == 1
    assert second.start == first.start + 3600
    assert sum(first.saved_openai) == 120 and sum(second.saved_openai) == 60
