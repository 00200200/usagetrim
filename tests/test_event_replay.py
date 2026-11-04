"""Inspection metadata and opt-in snapshots must preserve telemetry privacy."""

import sqlite3
from dataclasses import asdict

import pytest

from usagetrim.core.cache import ContextCache, decompress_payload
from usagetrim.core.event_replay import (
    EventInspection,
    capture_event_snapshots,
    snapshot_availability,
)
from usagetrim.core.telemetry import TelemetryStore, record_text


@pytest.fixture
def replay(monkeypatch, tmp_path):
    monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", "1")
    monkeypatch.setenv("USAGETRIM_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("USAGETRIM_CACHE_MAX_BYTES", raising=False)
    monkeypatch.delenv("USAGETRIM_CACHE_MAX_MB", raising=False)
    return tmp_path


def rows(store, table):
    assert table in {"events", "event_inspection", "output_cache"}
    with sqlite3.connect(store.db_path) as conn:
        return conn.execute(f"SELECT * FROM {table}").fetchall()


def test_existing_database_migration_preserves_events_and_statistics(tmp_path):
    path = tmp_path / "telemetry.db"
    store = TelemetryStore(path)
    store.record(100, 30, 90, 20, 80, 10, event_id="historical")
    with store.connect() as conn:
        conn.execute("DROP TRIGGER event_inspection_cleanup")
        conn.execute("DROP TABLE event_inspection")
    original = rows(store, "events")
    stats = store.get_stats()
    migrated = TelemetryStore(path)
    assert rows(migrated, "events") == original
    assert migrated.get_stats() == stats
    assert migrated.get_inspection("historical") is None
    assert rows(migrated, "event_inspection") == []


def test_snapshot_pair_uses_one_event_identity_and_recovers_both_texts(replay):
    cache = ContextCache()
    metadata = capture_event_snapshots("original\n", "compact\n", cache_allowed=True, cache=cache)
    store = TelemetryStore()
    # Identity is generated once by record(), not independently by capture.
    assert store.record(100, 20, inspection=metadata) is None
    events, inspections = rows(store, "events"), rows(store, "event_inspection")
    assert len(events) == len(inspections) == 1
    assert events[0][1] == inspections[0][0]
    assert store.get_inspection(inspections[0][0]) == metadata
    assert metadata.snapshot_state == "stored"
    assert cache.retrieve(metadata.before_ref) == "original\n"
    assert cache.retrieve(metadata.after_ref) == "compact\n"


def test_snapshot_redaction_precedes_persistence_including_ansi_split_tokens(replay):
    secret = "ghp_" + "A" * 36
    before = "ghp_\x1b[31m" + "A" * 36
    cache = ContextCache()
    metadata = capture_event_snapshots(before, secret, cache_allowed=True, cache=cache)
    assert metadata.before_ref == metadata.after_ref
    assert "REDACTED" in cache.retrieve(metadata.before_ref)
    store = TelemetryStore()
    store.record(10, inspection=metadata, command=secret)
    with sqlite3.connect(cache.db_path) as conn:
        payloads = [
            decompress_payload(row[0]) for row in conn.execute("SELECT content FROM output_cache")
        ]
        assert all(secret not in payload and "A" * 36 not in payload for payload in payloads)
    # Telemetry has no payload column; raw strings must not occur in its file.
    assert secret.encode() not in store.db_path.read_bytes()
    assert before.encode() not in store.db_path.read_bytes()
    assert secret.encode() not in cache.db_path.read_bytes()
    assert before.encode() not in cache.db_path.read_bytes()
    assert "A" * 36 not in repr(rows(store, "event_inspection"))
    assert len(payloads) == 1


@pytest.mark.parametrize("setting", [None, "", "0", "true"])
def test_replay_is_disabled_by_default_without_creating_cache(tmp_path, monkeypatch, setting):
    monkeypatch.setenv("USAGETRIM_CACHE_DIR", str(tmp_path))
    if setting is None:
        monkeypatch.delenv("USAGETRIM_EVENT_REPLAY", raising=False)
    else:
        monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", setting)
    result = capture_event_snapshots("before", "after", cache_allowed=True)
    assert result == EventInspection()
    assert not (tmp_path / "cache.db").exists()


@pytest.mark.parametrize("allowed", [False, None, "yes", 1])
def test_no_cache_or_missing_caller_permission_overrides_opt_in(replay, allowed):
    result = capture_event_snapshots("before", "after", cache_allowed=allowed)
    assert result.snapshot_state == "cache_disabled"
    assert result.before_ref is None and result.after_ref is None
    assert not (replay / "cache.db").exists()


def test_missing_text_is_not_a_complete_empty_snapshot(replay):
    missing = capture_event_snapshots(None, "", cache_allowed=True)
    assert missing.snapshot_state == "unavailable" and missing.before_ref is None
    empty = capture_event_snapshots("", "", cache_allowed=True)
    assert empty.snapshot_state == "stored"
    assert ContextCache().retrieve(empty.before_ref) == ""


def test_snapshot_limits_are_checked_for_both_inputs_before_any_write(replay, monkeypatch):
    from usagetrim.core import event_replay

    monkeypatch.setattr(event_replay, "SNAPSHOT_LIMIT", 16)
    result = capture_event_snapshots("ok", "🙂" * 5, cache_allowed=True)
    assert result.snapshot_state == "too_large"
    assert result.before_ref is None and result.after_ref is None
    assert not (replay / "cache.db").exists()
    exact = capture_event_snapshots("x" * 16, "y" * 16, cache_allowed=True)
    assert exact.snapshot_state == "stored"


def test_redaction_expansion_and_invalid_unicode_are_not_silently_truncated(replay, monkeypatch):
    from usagetrim.core import event_replay

    monkeypatch.setattr(event_replay, "SNAPSHOT_LIMIT", 8)
    monkeypatch.setattr(event_replay, "redact_secrets", lambda text: "[REDACTED]" * 2)
    assert capture_event_snapshots("x", "x", cache_allowed=True).snapshot_state == "too_large"
    assert (
        capture_event_snapshots("\udcff", "x", cache_allowed=True).snapshot_state == "unavailable"
    )
    assert not (replay / "cache.db").exists()


def test_existing_and_identical_payloads_are_not_written_again(replay, monkeypatch):
    cache = ContextCache()
    existing = cache.store("same", source="original-source")

    def forbidden(*args, **kwargs):
        pytest.fail("Identical existing CCR content must be reused")

    monkeypatch.setattr(ContextCache, "store", forbidden)
    result = capture_event_snapshots("same", "same", cache_allowed=True, cache=cache)
    assert result.before_ref == result.after_ref == existing
    assert len(rows(cache, "output_cache")) == 1
    with sqlite3.connect(cache.db_path) as conn:
        assert conn.execute("SELECT source,hit_count FROM output_cache").fetchone() == (
            "original-source",
            0,
        )


def test_duplicate_event_does_not_attach_different_inspection(replay):
    store = TelemetryStore()
    first = capture_event_snapshots("before", "after", cache_allowed=True)
    store.record(100, 20, event_id="stable", inspection=first)
    store.record(999, event_id="stable", inspection=EventInspection(tool_error="internal_error"))
    assert store.get_stats().total_runs == 1
    assert store.get_stats().raw_claude_tokens == 100
    assert store.get_inspection("stable") == first
    # A legacy event must not be retroactively decorated by a duplicate insert.
    store.record(1, event_id="legacy")
    store.record(1, event_id="legacy", inspection=first)
    assert store.get_inspection("legacy") is None


def test_ccr_write_failure_preserves_event_without_partial_pair(replay, monkeypatch):
    original = ContextCache.store
    calls = []

    def fail_second(self, text, *args, **kwargs):
        calls.append(text)
        if len(calls) == 2:
            raise sqlite3.OperationalError("secret exception content")
        return original(self, text, *args, **kwargs)

    monkeypatch.setattr(ContextCache, "store", fail_second)
    details = capture_event_snapshots("before", "after", cache_allowed=True)
    assert details.snapshot_state == "unavailable"
    assert details.before_ref is None and details.after_ref is None
    store = TelemetryStore()
    store.record(100, 10, event_id="failed-cache", inspection=details)
    assert store.get_stats().total_runs == 1
    assert "secret exception content" not in repr(rows(store, "event_inspection"))


def test_inspection_failure_rolls_back_event_transaction(replay):
    store = TelemetryStore()
    with store.connect() as conn:
        conn.execute("""CREATE TRIGGER reject_inspection BEFORE INSERT ON event_inspection
            BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.record(10, event_id="atomic", inspection=EventInspection())
    assert rows(store, "events") == []
    assert rows(store, "event_inspection") == []


def test_eviction_and_missing_cache_preserve_honest_metadata(replay):
    metadata = capture_event_snapshots("before", "after", cache_allowed=True)
    assert snapshot_availability(metadata) == (True, True)
    cache = ContextCache()
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute("DELETE FROM output_cache WHERE ref_id=?", (metadata.before_ref,))
    assert snapshot_availability(metadata) == (False, True)
    cache.db_path.unlink()
    assert snapshot_availability(metadata) == (False, False)
    assert not cache.db_path.exists()


@pytest.mark.parametrize("direct_delete", [False, True])
def test_history_deletion_removes_only_inspection_not_shared_ccr(replay, direct_delete):
    store = TelemetryStore()
    details = capture_event_snapshots("shared", "shared", cache_allowed=True)
    store.record(10, event_id="event", inspection=details)
    if direct_delete:
        # Legacy SQLite connections generally have foreign_keys disabled.
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("DELETE FROM events")
    else:
        store.clear()
    assert rows(store, "event_inspection") == []
    assert ContextCache().retrieve(details.before_ref) == "shared"


@pytest.mark.parametrize(
    "field,value",
    [
        ("tool_error", "secret exception"),
        ("termination_reason", "raw exception"),
        ("recovery_ref", "token or command"),
        ("child_returncode", True),
        ("snapshot_state", "unknown"),
        ("recovery_scope", "whole command"),
    ],
)
def test_metadata_rejects_unbounded_or_unknown_error_content(field, value):
    with pytest.raises(ValueError):
        EventInspection(**{field: value})


def test_record_text_retains_legacy_stats_and_requires_caller_cache_permission(replay):
    assert record_text("raw", "output", offline=True, event_id="legacy") is None
    store = TelemetryStore()
    before = asdict(store.get_stats())
    assert store.get_inspection("legacy").snapshot_state == "cache_disabled"
    assert not (replay / "cache.db").exists()
    assert record_text("raw", "output", offline=True, event_id="opted", cache_allowed=True) is None
    after = asdict(store.get_stats())
    assert after == {name: 2 * value for name, value in before.items()}
    details = store.get_inspection("opted")
    assert details.snapshot_state == "stored"
    assert ContextCache().retrieve(details.before_ref) == "raw"
    assert ContextCache().retrieve(details.after_ref) == "output"


def test_record_text_telemetry_failure_is_best_effort_without_cross_db_claim(replay):
    store = TelemetryStore()
    with store.connect() as conn:
        conn.execute("""CREATE TRIGGER reject_inspection BEFORE INSERT ON event_inspection
            BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
    assert (
        record_text("before", "after", offline=True, event_id="failed", cache_allowed=True) is None
    )
    assert rows(store, "events") == []
    assert rows(store, "event_inspection") == []
    # CCR writes committed separately. Do not delete possibly shared snapshots
    # to pretend that both databases participated in the rollback.
    assert len(rows(ContextCache(), "output_cache")) == 2


def test_known_child_success_is_independent_of_snapshot_failure(replay, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("private storage error")

    monkeypatch.setattr(ContextCache, "store", unavailable)
    details = capture_event_snapshots(
        "before",
        "after",
        cache_allowed=True,
        inspection=EventInspection(child_returncode=0, termination_reason="exited"),
    )
    assert details.snapshot_state == "unavailable"
    assert details.child_returncode == 0
    assert details.termination_reason == "exited"
    assert details.tool_error is None


@pytest.mark.parametrize("inspection", [None, EventInspection()])
def test_record_text_without_replay_or_status_skips_inspection_and_capture(
    replay, monkeypatch, inspection
):
    from usagetrim.core import telemetry

    monkeypatch.delenv("USAGETRIM_EVENT_REPLAY")

    def forbidden(*args, **kwargs):
        pytest.fail("Plain legacy telemetry must not invoke replay capture")

    monkeypatch.setattr(telemetry, "capture_event_snapshots", forbidden)
    record_text(
        "before",
        "after",
        offline=True,
        event_id="plain",
        cache_allowed=True,
        inspection=inspection,
    )
    store = TelemetryStore()
    assert store.get_stats().total_runs == 1
    assert store.get_inspection("plain") is None
    assert rows(store, "event_inspection") == []
    assert not (replay / "cache.db").exists()


@pytest.mark.parametrize(
    "details",
    [
        EventInspection(child_returncode=7, termination_reason="exited"),
        EventInspection(recovery_ref="tc_0123456789abcdef", recovery_scope="lines"),
    ],
)
def test_record_text_keeps_explicit_metadata_with_replay_disabled(replay, monkeypatch, details):
    monkeypatch.delenv("USAGETRIM_EVENT_REPLAY")
    record_text("before", "after", offline=True, event_id="status", inspection=details)
    assert TelemetryStore().get_inspection("status") == details
    assert not (replay / "cache.db").exists()
