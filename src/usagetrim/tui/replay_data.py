"""Read-only inspection and bounded diffs; no Textual or tokenizer dependency.

Call from an I/O worker, not the UI thread. Never infer outcomes from log text,
reconstruct outputs, migrate databases, or count inspection as CCR recovery.
"""

from __future__ import annotations

import difflib
import io
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from enum import StrEnum

from usagetrim.core.cache import ContextCache
from usagetrim.core.cleaner import strip_ansi
from usagetrim.core.event_replay import INSPECTION_COLUMNS, EventInspection
from usagetrim.core.redactor import redact_secrets
from usagetrim.core.telemetry import TelemetryStore
from usagetrim.tui.cache_content import CacheContentError, read_redacted_content
from usagetrim.tui.cache_data import _metadata_connection

DIFF_INPUT_BYTES = 128 * 1024
DIFF_STORED_BYTES = DIFF_INPUT_BYTES + 64 * 1024
DIFF_INPUT_LINES = 1000
DIFF_LINE_BYTES = 4096  # Includes a line's terminator after display sanitization.
DIFF_OUTPUT_BYTES = 256 * 1024
_CONTROL_ESCAPES = {
    value: f"\\x{value:02x}" for value in (*range(32), *range(127, 160)) if value not in (9, 10)
}


class ReplayState(StrEnum):
    READY = "ready"
    IDENTICAL = "identical"
    HISTORICAL = "historical"
    EVENT_MISSING = "event_missing"
    METADATA_UNAVAILABLE = "metadata_unavailable"
    INVALID_METADATA = "invalid_metadata"
    DISABLED = "disabled"
    CACHE_DISABLED = "cache_disabled"
    CAPTURE_TOO_LARGE = "capture_too_large"
    SNAPSHOT_UNAVAILABLE = "snapshot_unavailable"
    SNAPSHOT_MISSING = "snapshot_missing"
    PAYLOAD_INVALID = "payload_invalid"
    INTEGRITY_FAILED = "integrity_failed"
    INPUT_LIMIT = "input_limit"
    OUTPUT_LIMIT = "output_limit"


_MESSAGES = {
    ReplayState.READY: "Complete comparison of sanitized, redacted snapshots.",
    ReplayState.IDENTICAL: "No differences in sanitized, redacted snapshots.",
    ReplayState.HISTORICAL: "Inspection data was not recorded for this event.",
    ReplayState.EVENT_MISSING: "The selected telemetry event no longer exists.",
    ReplayState.METADATA_UNAVAILABLE: "Telemetry metadata is unavailable.",
    ReplayState.INVALID_METADATA: "Inspection metadata is invalid.",
    ReplayState.DISABLED: "Replay was disabled when this event was recorded.",
    ReplayState.CACHE_DISABLED: "Snapshot storage was not permitted for this event.",
    ReplayState.CAPTURE_TOO_LARGE: "The original capture exceeded the snapshot limit.",
    ReplayState.SNAPSHOT_UNAVAILABLE: "Snapshots cannot be read safely or support is unavailable.",
    ReplayState.SNAPSHOT_MISSING: "One or both snapshots are missing or were evicted.",
    ReplayState.PAYLOAD_INVALID: "A snapshot is damaged, incomplete, or unsupported.",
    ReplayState.INTEGRITY_FAILED: "Snapshot integrity could not be verified; comparison is unavailable.",
    ReplayState.INPUT_LIMIT: "Snapshots exceed the safe diff input limits; no comparison was made.",
    ReplayState.OUTPUT_LIMIT: "The complete diff exceeds the output limit; no partial diff is shown.",
}


@dataclass(frozen=True)
class InspectionData:
    event_id: str = field(repr=False)
    state: ReplayState
    inspection: EventInspection | None = None
    delivery: str | None = None


@dataclass(frozen=True)
class EventStatus:
    execution: str
    recovery: str
    delivery: str


@dataclass(frozen=True)
class ReplayData:
    metadata: InspectionData
    state: ReplayState
    # Presence is not a claim of payload integrity or permanent retention.
    before_present: bool | None = None
    after_present: bool | None = None
    before: str | None = field(default=None, repr=False)
    after: str | None = field(default=None, repr=False)
    unified_diff: str | None = field(default=None, repr=False)

    @property
    def message(self) -> str:
        return _MESSAGES[self.state]


def read_event_inspection(store: TelemetryStore, event_id: str) -> InspectionData:
    """Read one event and optional inspection in one read-only SQLite snapshot.

    Use the store's configured path without calling its initializer or migration.
    Old databases lacking event_inspection remain untouched.
    """
    try:
        with closing(
            sqlite3.connect(store.db_path.absolute().as_uri() + "?mode=ro", uri=True, timeout=5)
        ) as conn:
            conn.execute("BEGIN")
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?)",
                    ("events", "event_inspection"),
                )
            }
            if "events" not in tables:
                return InspectionData(event_id, ReplayState.METADATA_UNAVAILABLE)
            event = conn.execute(
                "SELECT delivery FROM events WHERE event_id=? LIMIT 1", (event_id,)
            ).fetchone()
            if event is None:
                return InspectionData(event_id, ReplayState.EVENT_MISSING)
            delivery = event[0] if event[0] in {"returned", "prepared"} else None
            if "event_inspection" not in tables:
                return InspectionData(event_id, ReplayState.HISTORICAL, delivery=delivery)
            row = conn.execute(
                f"SELECT {','.join(INSPECTION_COLUMNS)} FROM event_inspection WHERE event_id=?",
                (event_id,),
            ).fetchone()
        if row is None:
            return InspectionData(event_id, ReplayState.HISTORICAL, delivery=delivery)
        try:
            inspection = EventInspection(**dict(zip(INSPECTION_COLUMNS, row)))
        except (TypeError, ValueError):
            return InspectionData(event_id, ReplayState.INVALID_METADATA, delivery=delivery)
        return InspectionData(event_id, ReplayState.READY, inspection, delivery)
    except (OSError, sqlite3.Error, ValueError):
        return InspectionData(event_id, ReplayState.METADATA_UNAVAILABLE)


def format_event_status(
    inspection: EventInspection | None, *, delivery: str | None = None
) -> EventStatus:
    """Format independent facts; Pass means process exit 0, not passing tests."""
    execution = "Not recorded"
    recovery = "Not recorded"
    if inspection is not None:
        execution = {
            "signaled": "Signaled",
            "timed_out": "Timed out",
            "failed_to_start": "Failed to start",
            "cancelled": "Cancelled",
        }.get(inspection.termination_reason, "Not recorded")
        code = inspection.child_returncode
        if inspection.termination_reason in {None, "exited"} and code is not None:
            execution = "Pass (exit 0)" if code == 0 else f"Fail (exit {code})"
        if inspection.recovery_ref is not None:
            recovery = {
                "full": "Recovered (full redacted content)",
                "lines": "Retrieved line range",
                "query": "Retrieved search results",
            }.get(inspection.recovery_scope, "Recovery scope not recorded")
            if delivery == "prepared":
                scope = {
                    "full": "full redacted content",
                    "lines": "line range",
                    "query": "search results",
                }.get(inspection.recovery_scope, "scope not recorded")
                recovery = f"Prepared recovery ({scope}; delivery unconfirmed)"
    delivery_status = {
        "prepared": "Prepared (delivery unconfirmed)",
        "returned": "Returned",
    }.get(delivery, "Not recorded")
    return EventStatus(execution, recovery, delivery_status)


def _presence(cache, inspection):
    with _metadata_connection(cache) as conn:
        refs = (
            set()
            if conn is None
            else {
                row[0]
                for row in conn.execute(
                    "SELECT ref_id FROM output_cache WHERE ref_id IN (?, ?)",
                    (inspection.before_ref, inspection.after_ref),
                )
            }
        )
    return inspection.before_ref in refs, inspection.after_ref in refs


def _display_text(text):
    # Strip before re-redaction so ANSI cannot hide fragments of a credential.
    text = redact_secrets(strip_ansi(text))
    # Make remaining terminal control bytes visible. Rich/Textual markup is
    # ordinary text here; consumers must render these strings without markup.
    return text.translate(_CONTROL_ESCAPES)


def _diff_lines(text):
    if len(text.encode("utf-8")) > DIFF_INPUT_BYTES:
        return None
    lines = []
    for line in io.StringIO(text):
        if len(lines) == DIFF_INPUT_LINES or len(line.encode("utf-8")) > DIFF_LINE_BYTES:
            return None
        lines.append(line)
    return lines


def load_event_replay(
    store: TelemetryStore, event_id: str, *, cache: ContextCache | None = None
) -> ReplayData:
    """Load complete bounded snapshots and produce a complete unified diff.

    None payloads mean unavailable; empty strings are valid snapshots/diffs.
    No connection, widget or cache inquiry counter is shared or modified.
    """
    metadata = read_event_inspection(store, event_id)
    if metadata.state is not ReplayState.READY:
        return ReplayData(metadata, metadata.state)
    inspection = metadata.inspection
    state = {
        "disabled": ReplayState.DISABLED,
        "cache_disabled": ReplayState.CACHE_DISABLED,
        "too_large": ReplayState.CAPTURE_TOO_LARGE,
        "unavailable": ReplayState.SNAPSHOT_UNAVAILABLE,
    }.get(inspection.snapshot_state)
    if state is not None:
        return ReplayData(metadata, state)
    present = (None, None)
    try:
        present = _presence(cache, inspection)
        if not all(present):
            return ReplayData(metadata, ReplayState.SNAPSHOT_MISSING, *present)
        before = read_redacted_content(
            cache,
            inspection.before_ref,
            max_bytes=DIFF_INPUT_BYTES,
            max_stored_bytes=DIFF_STORED_BYTES,
            verify_hash=True,
        )
        if inspection.before_ref == inspection.after_ref:
            after = before
        else:
            after = read_redacted_content(
                cache,
                inspection.after_ref,
                max_bytes=DIFF_INPUT_BYTES,
                max_stored_bytes=DIFF_STORED_BYTES,
                verify_hash=True,
            )
        before, after = _display_text(before), _display_text(after)
        old_lines, new_lines = _diff_lines(before), _diff_lines(after)
        if old_lines is None or new_lines is None:
            return ReplayData(metadata, ReplayState.INPUT_LIMIT, *present)
        if before == after:
            return ReplayData(metadata, ReplayState.IDENTICAL, *present, before, after, "")
        parts, size = [], 0
        for part in difflib.unified_diff(old_lines, new_lines, fromfile="before", tofile="after"):
            if not part.endswith("\n"):
                part += "\n\\ No newline at end of file\n"
            size += len(part.encode("utf-8"))
            if size > DIFF_OUTPUT_BYTES:
                return ReplayData(metadata, ReplayState.OUTPUT_LIMIT, *present)
            parts.append(part)
        return ReplayData(metadata, ReplayState.READY, *present, before, after, "".join(parts))
    except CacheContentError as exc:
        state = {
            "missing": ReplayState.SNAPSHOT_MISSING,
            "limit": ReplayState.INPUT_LIMIT,
            "unavailable": ReplayState.SNAPSHOT_UNAVAILABLE,
            "integrity": ReplayState.INTEGRITY_FAILED,
        }.get(exc.reason, ReplayState.PAYLOAD_INVALID)
        if exc.reason == "missing":
            try:
                present = _presence(cache, inspection)
            except (OSError, sqlite3.Error):
                present = (None, None)
        return ReplayData(metadata, state, *present)
    except (OSError, sqlite3.Error):
        return ReplayData(metadata, ReplayState.SNAPSHOT_UNAVAILABLE, *present)
