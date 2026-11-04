"""Bounded, read-only telemetry history queries for the dashboard."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from usagetrim.core.event_replay import INSPECTION_COLUMNS, EventInspection
from usagetrim.core.telemetry import EVENT_COLUMNS, TelemetryStore
from usagetrim.tui.replay_data import InspectionData, ReplayState


@dataclass(frozen=True)
class HistoryFilters:
    operation: str | None = None
    client: str | None = None
    project: str | None = None
    time_range: str = "all"

    def __post_init__(self) -> None:
        if self.time_range not in {"all", "24h", "7d"}:
            raise ValueError("time_range must be all, 24h or 7d")


def _event_predicates(filters: HistoryFilters) -> tuple[list[str], list[Any]]:
    """Share gain eligibility and exact metadata filters across dashboard queries."""
    clauses = ["engine = ?", "delivery != ?"]
    parameters: list[Any] = ["usagetrim", "prepared"]
    for column, value in (
        ("operation", filters.operation),
        ("client", filters.client),
        ("project", filters.project),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            parameters.append(value)
    return clauses, parameters


def read_history(
    store: TelemetryStore,
    filters: HistoryFilters,
    *,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Return at most 50 matching events; filtering happens entirely in SQLite.

    Match gain's eligibility rules. Text filters are exact, case-sensitive values;
    blank controls mean no filter. Window boundaries are inclusive, in UTC epoch
    seconds. All-time includes every eligible timestamp, including future values.
    """
    clauses, parameters = _event_predicates(filters)
    if filters.time_range != "all":
        now = time.time() if now is None else now
        seconds = 86400 if filters.time_range == "24h" else 7 * 86400
        clauses.extend(["timestamp >= ?", "timestamp <= ?"])
        parameters.extend([now - seconds, now])

    with store.connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT sequence,{','.join(EVENT_COLUMNS)} FROM events "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY timestamp DESC, sequence DESC LIMIT 50",
            parameters,
        ).fetchall()
    return [dict(row) for row in rows]


def read_history_inspections(
    store: TelemetryStore, history: list[dict]
) -> dict[str, InspectionData]:
    """Read inspection metadata for one bounded history page, without N+1 queries."""
    events = {row["event_id"]: row for row in history[:50]}
    result = {
        key: InspectionData(key, ReplayState.HISTORICAL, delivery=row.get("delivery"))
        for key, row in events.items()
    }
    if not events:
        return result
    try:
        with closing(store.connect()) as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    ("event_inspection",),
                ).fetchone()
                is None
            ):
                return result
            rows = conn.execute(
                f"SELECT event_id,{','.join(INSPECTION_COLUMNS)} FROM event_inspection "
                f"WHERE event_id IN ({','.join('?' for _ in events)})",
                tuple(events),
            ).fetchall()
        for row in rows:
            key = row[0]
            try:
                inspection = EventInspection(**dict(zip(INSPECTION_COLUMNS, row[1:])))
                result[key] = InspectionData(
                    key, ReplayState.READY, inspection, events[key].get("delivery")
                )
            except (TypeError, ValueError):
                result[key] = InspectionData(key, ReplayState.INVALID_METADATA)
    except sqlite3.Error:
        return {key: InspectionData(key, ReplayState.METADATA_UNAVAILABLE) for key in events}
    return result
