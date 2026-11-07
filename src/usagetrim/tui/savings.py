"""Bounded SQLite aggregation for the dashboard's token savings chart."""

from __future__ import annotations

import time
from dataclasses import dataclass

from usagetrim.core.telemetry import TelemetryStore
from usagetrim.tui.history import HistoryFilters, _event_predicates


@dataclass(frozen=True)
class SavingsSeries:
    start: float
    end: float
    interval_seconds: int
    saved_openai: tuple[int, ...]
    event_count: int


def read_savings(
    store: TelemetryStore,
    filters: HistoryFilters,
    *,
    now: float | None = None,
) -> SavingsSeries:
    """Return 24 hourly, 7 daily, or 30 daily buckets, including empty buckets.

    Windows roll back from `now`, not local midnight, matching history cutoffs
    without DST ambiguity. Buckets are [start, end), except the final bucket also
    includes `now`. All-time history is unchanged; only its chart is capped at
    the last 30 days. Clamp net savings after aggregation, as gain does.
    """
    now = time.time() if now is None else now
    count, interval = {
        "24h": (24, 3600),
        "7d": (7, 86400),
        "all": (30, 86400),
    }[filters.time_range]
    start = now - count * interval
    clauses, parameters = _event_predicates(filters)
    clauses.extend(["timestamp >= ?", "timestamp <= ?"])
    parameters.extend([start, now])
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT MIN(?, CAST((timestamp - ?) / ? AS INTEGER)) AS bucket, "
            "MAX(0, COALESCE(SUM(raw_openai), 0) - COALESCE(SUM(compact_openai), 0)), "
            "COUNT(*) FROM events "
            f"WHERE {' AND '.join(clauses)} "
            "GROUP BY bucket ORDER BY bucket LIMIT ?",
            [count - 1, start, interval, *parameters, count],
        ).fetchall()
    values = [0] * count
    event_count = 0
    for bucket, saved, events in rows:
        values[bucket] = int(saved)
        event_count += events
    return SavingsSeries(start, now, interval, tuple(values), event_count)
