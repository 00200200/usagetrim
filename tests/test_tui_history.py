"""Dashboard history queries use SQLite filtering, not in-memory sampling."""

import pytest

from usagetrim.core.telemetry import EVENT_COLUMNS, TelemetryStore
from usagetrim.tui.history import HistoryFilters, read_history

NOW = 1_700_000_000


def add_event(store, event_id, timestamp=NOW, **overrides):
    fields = {"operation": "read", "client": "codex", "project": "/repo"}
    fields.update(overrides)
    store.record(0, event_id=event_id, raw_openai=100, compact_openai=40, **fields)
    with store.connect() as conn:
        conn.execute("UPDATE events SET timestamp=? WHERE event_id=?", (timestamp, event_id))


@pytest.mark.parametrize(
    "filters,expected",
    [
        (HistoryFilters(operation="read"), ["a", "c"]),
        (HistoryFilters(client="claude"), ["b", "c"]),
        (HistoryFilters(project="/app"), ["a", "b"]),
        (HistoryFilters(operation="read", client="claude", project="/other"), ["c"]),
        (HistoryFilters(operation="Read"), []),
        (HistoryFilters(operation="read", client="claude", project="/app"), []),
    ],
)
def test_history_exact_filters_and_combinations(tmp_path, filters, expected):
    store = TelemetryStore(tmp_path / "telemetry.db")
    add_event(store, "a", NOW - 10, operation="read", client="codex", project="/app")
    add_event(store, "b", NOW - 20, operation="diff", client="claude", project="/app")
    add_event(store, "c", NOW - 30, operation="read", client="claude", project="/other")
    assert [row["event_id"] for row in read_history(store, filters)] == expected


@pytest.mark.parametrize(
    "period,expected",
    [
        ("24h", ["now", "day-edge"]),
        ("7d", ["now", "day-edge", "day-old", "week-edge"]),
        ("all", ["future", "now", "day-edge", "day-old", "week-edge", "week-old"]),
    ],
)
def test_history_time_windows_are_inclusive(tmp_path, period, expected):
    store = TelemetryStore(tmp_path / "telemetry.db")
    for event_id, timestamp in [
        ("future", NOW + 1),
        ("now", NOW),
        ("day-edge", NOW - 86400),
        ("day-old", NOW - 86401),
        ("week-edge", NOW - 7 * 86400),
        ("week-old", NOW - 7 * 86400 - 1),
    ]:
        add_event(store, event_id, timestamp)
    rows = read_history(store, HistoryFilters(time_range=period), now=NOW)
    assert [row["event_id"] for row in rows] == expected


def test_history_limit_applies_after_filtering_and_ties_are_stable(tmp_path, monkeypatch):
    store = TelemetryStore(tmp_path / "telemetry.db")
    add_event(store, "old-match", NOW - 100, operation="diff")
    for index in range(60):
        add_event(store, f"row-{index}")
    add_event(store, "excluded-engine", engine="none")
    add_event(store, "excluded-preview", delivery="prepared")
    statements = []
    original_connect = store.connect

    def connect():
        conn = original_connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store, "connect", connect)
    all_rows = read_history(store, HistoryFilters())
    assert [row["event_id"] for row in all_rows] == [f"row-{i}" for i in range(59, 9, -1)]
    assert [row["event_id"] for row in read_history(store, HistoryFilters(operation="diff"))] == [
        "old-match"
    ]
    selects = [sql for sql in statements if sql.lstrip().startswith("SELECT")]
    assert len(selects) == 2
    assert all("LIMIT 50" in sql for sql in selects)


@pytest.mark.parametrize("field", ["operation", "client", "project"])
def test_history_filters_bind_literal_sql_and_unicode_values(tmp_path, field):
    store = TelemetryStore(tmp_path / "telemetry.db")
    literal = "żółć' OR 1=1 --"
    add_event(store, "literal", **{field: literal})
    add_event(store, "other")
    filters = HistoryFilters(**{field: literal})
    assert [row["event_id"] for row in read_history(store, filters)] == ["literal"]


def test_history_empty_database_and_nullable_metadata(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    assert read_history(store, HistoryFilters()) == []
    add_event(store, "legacy")
    with store.connect() as conn:
        conn.execute("UPDATE events SET client=NULL, project=NULL, raw_openai=NULL")
    row = read_history(store, HistoryFilters())[0]
    assert set(row) == {"sequence", *EVENT_COLUMNS}
    assert row["client"] is None and row["project"] is None and row["raw_openai"] is None
    assert read_history(store, HistoryFilters(client="codex")) == []


def test_history_rejects_unknown_time_range():
    with pytest.raises(ValueError, match="time_range"):
        HistoryFilters(time_range="month")
