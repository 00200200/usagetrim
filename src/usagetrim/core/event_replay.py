"""Opt-in redacted event snapshots; telemetry stores metadata, never payloads.

USAGETRIM_EVENT_REPLAY=1 is necessary but not sufficient: each caller must also
explicitly allow caching after applying its no-cache/disabled-storage policy.
Snapshots describe sanitized text, not byte-exact terminal streams.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from usagetrim.core.cache import DEFAULT_CACHE_DB, ContextCache
from usagetrim.core.cleaner import strip_ansi
from usagetrim.core.redactor import redact_secrets

SNAPSHOT_LIMIT = 4 * 1024 * 1024
_REF = re.compile(r"tc_[0-9a-f]{16}\Z")
INSPECTION_COLUMNS = (
    "before_ref",
    "after_ref",
    "snapshot_state",
    "child_returncode",
    "termination_reason",
    "tool_error",
    "recovery_ref",
    "recovery_scope",
)


@dataclass(frozen=True)
class EventInspection:
    """Allowlisted metadata only; absence is distinct from empty output."""

    before_ref: str | None = None
    after_ref: str | None = None
    snapshot_state: str = "disabled"
    child_returncode: int | None = None
    termination_reason: str | None = None
    tool_error: str | None = None
    recovery_ref: str | None = None
    recovery_scope: str | None = None

    def __post_init__(self):
        for ref in (self.before_ref, self.after_ref, self.recovery_ref):
            if ref is not None and (not isinstance(ref, str) or _REF.fullmatch(ref) is None):
                raise ValueError("Invalid inspection reference")
        if self.snapshot_state not in {
            "disabled",
            "cache_disabled",
            "stored",
            "too_large",
            "unavailable",
        }:
            raise ValueError("Invalid snapshot state")
        has_before = self.before_ref is not None
        has_after = self.after_ref is not None
        if self.snapshot_state == "stored" and not (has_before and has_after):
            raise ValueError("Stored snapshots require both references")
        if self.snapshot_state != "stored" and (has_before or has_after):
            raise ValueError("Stored snapshots require both references")
        if self.child_returncode is not None and (
            type(self.child_returncode) is not int or not -(2**63) <= self.child_returncode < 2**63
        ):
            raise ValueError("Invalid child return code")
        if self.termination_reason not in {
            None,
            "exited",
            "signaled",
            "timed_out",
            "failed_to_start",
            "cancelled",
        }:
            raise ValueError("Invalid termination reason")
        if self.termination_reason == "failed_to_start" and self.child_returncode is not None:
            raise ValueError("A failed start has no child return code")
        if self.tool_error not in {
            None,
            "invalid_arguments",
            "execution_error",
            "internal_error",
            "storage_error",
            "permission_denied",
            "not_found",
        }:
            raise ValueError("Invalid tool error category")
        if self.recovery_scope not in {None, "full", "lines", "query"}:
            raise ValueError("Invalid recovery scope")
        if self.recovery_scope is not None and self.recovery_ref is None:
            raise ValueError("A recovery scope requires a reference")


def _safe_text(text: str) -> str:
    if len(text) > SNAPSHOT_LIMIT or len(text.encode("utf-8")) > SNAPSHOT_LIMIT:
        raise OverflowError("Snapshot exceeds limit")
    # Strip before redaction so ANSI cannot conceal a split credential. Redact
    # again at ContextCache.store(); no command or exception text is added.
    sanitized = redact_secrets(strip_ansi(text))
    if len(sanitized.encode("utf-8")) > SNAPSHOT_LIMIT:
        raise OverflowError("Redacted snapshot exceeds limit")
    return sanitized


def _store_snapshot(cache: ContextCache, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    ref = "tc_" + digest[:16]
    with sqlite3.connect(cache.db_path, timeout=5) as conn:
        row = conn.execute(
            "SELECT content_hash FROM output_cache WHERE ref_id=?", (ref,)
        ).fetchone()
    if row is not None:
        if row[0] != digest:
            raise ValueError("Snapshot reference collision")
        # Reuse the content-addressed entry without rewriting its source/time
        # or increasing inquiry counters. Availability may change after this.
        return ref
    return cache.store(text, source="event-replay")


def capture_event_snapshots(
    before: str | None,
    after: str | None,
    *,
    cache_allowed: bool = False,
    cache: ContextCache | None = None,
    inspection: EventInspection | None = None,
) -> EventInspection:
    """Best-effort capture, with an explicit caller veto overriding opt-in.

    A partial pair is never advertised as complete. An orphaned first snapshot
    after a second write failure remains subject to ordinary CCR eviction.
    No cross-database transaction or permanent retention is promised.
    """
    metadata = inspection or EventInspection()
    metadata = replace(metadata, before_ref=None, after_ref=None, snapshot_state="disabled")
    if os.environ.get("USAGETRIM_EVENT_REPLAY") != "1":
        return metadata
    if cache_allowed is not True:
        return replace(metadata, snapshot_state="cache_disabled")
    if before is None or after is None:
        return replace(metadata, snapshot_state="unavailable")
    try:
        before, after = _safe_text(before), _safe_text(after)
        cache = cache or ContextCache()
        before_ref = _store_snapshot(cache, before)
        after_ref = before_ref if before == after else _store_snapshot(cache, after)
        return replace(
            metadata, before_ref=before_ref, after_ref=after_ref, snapshot_state="stored"
        )
    except OverflowError:
        return replace(metadata, snapshot_state="too_large")
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        return replace(metadata, snapshot_state="unavailable")


def snapshot_availability(
    inspection: EventInspection, *, cache: ContextCache | None = None
) -> tuple[bool, bool]:
    """Check current row presence only, not payload integrity or completeness.

    This read never creates a cache, decompresses content or increments hits.
    The eventual bounded content reader must validate each payload separately.
    """
    path = (
        cache.db_path
        if cache is not None
        else Path(os.environ.get("USAGETRIM_CACHE_DIR", str(DEFAULT_CACHE_DB.parent))) / "cache.db"
    )
    try:
        with sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
            refs = {
                row[0]
                for row in conn.execute(
                    "SELECT ref_id FROM output_cache WHERE ref_id IN (?, ?)",
                    (inspection.before_ref, inspection.after_ref),
                )
            }
        return inspection.before_ref in refs, inspection.after_ref in refs
    except (OSError, sqlite3.Error):
        return False, False
