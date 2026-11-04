"""Real CCR diffs must be bounded, redacted, read-only and independent of Qt/Textual."""

import hashlib
import os
import sqlite3
import subprocess
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from usagetrim.core.cache import ContextCache
from usagetrim.core.event_replay import EventInspection
from usagetrim.core.telemetry import TelemetryStore
from usagetrim.tui import replay_data as replay


@pytest.fixture(autouse=True)
def stable_cache_budget(monkeypatch):
    monkeypatch.delenv("USAGETRIM_CACHE_MAX_BYTES", raising=False)
    monkeypatch.delenv("USAGETRIM_CACHE_MAX_MB", raising=False)


def pair(tmp_path, before, after, *, delivery="returned"):
    store = TelemetryStore(tmp_path / "telemetry.db")
    cache = ContextCache(tmp_path / "cache.db")
    inspection = EventInspection(
        before_ref=cache.store(before),
        after_ref=cache.store(after),
        snapshot_state="stored",
    )
    store.record(10, 5, event_id="event", inspection=inspection, delivery=delivery)
    return store, cache, inspection


def test_real_unified_diff_reads_both_actual_snapshots(tmp_path):
    store, cache, inspection = pair(tmp_path, "one\nold\n", "one\nnew\n")
    result = replay.load_event_replay(store, "event", cache=cache)
    assert result.state is replay.ReplayState.READY
    assert result.before == "one\nold\n" and result.after == "one\nnew\n"
    assert result.before_present is True and result.after_present is True
    assert result.metadata.inspection == inspection
    assert result.unified_diff == ("--- before\n+++ after\n@@ -1,2 +1,2 @@\n one\n-old\n+new\n")


@pytest.mark.parametrize("text", ["", "same\n", "🦙 Zażółć 漢字\n", "[bold]literal markup[/bold]"])
def test_identical_and_empty_snapshots_are_valid_not_missing(tmp_path, text):
    store, cache, _ = pair(tmp_path, text, text)
    result = replay.load_event_replay(store, "event", cache=cache)
    assert result.state is replay.ReplayState.IDENTICAL
    assert result.before == result.after == text
    assert result.unified_diff == ""
    assert result.before_present and result.after_present


def test_no_trailing_newline_is_explicit_in_unified_diff(tmp_path):
    store, cache, _ = pair(tmp_path, "old", "new")
    result = replay.load_event_replay(store, "event", cache=cache)
    assert result.unified_diff == (
        "--- before\n+++ after\n@@ -1 +1 @@\n"
        "-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n"
    )


def test_history_without_inspection_is_not_reconstructed(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record(100, 10, event_id="historical")
    result = replay.load_event_replay(store, "historical")
    assert result.state is replay.ReplayState.HISTORICAL
    assert result.metadata.inspection is None
    assert result.before is None and result.after is None and result.unified_diff is None
    assert not (tmp_path / "cache.db").exists()


def test_old_database_without_inspection_table_is_not_migrated(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE events (event_id TEXT, delivery TEXT)")
        conn.execute("INSERT INTO events VALUES ('old', 'returned')")
    original = path.read_bytes()
    result = replay.load_event_replay(SimpleNamespace(db_path=path), "old")
    assert result.state is replay.ReplayState.HISTORICAL
    assert path.read_bytes() == original
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name='event_inspection'").fetchone()
            is None
        )


def test_missing_database_and_deleted_event_are_distinct(tmp_path):
    missing = tmp_path / "absent.db"
    result = replay.load_event_replay(SimpleNamespace(db_path=missing), "event")
    assert result.state is replay.ReplayState.METADATA_UNAVAILABLE
    assert not missing.exists()
    store = TelemetryStore(tmp_path / "telemetry.db")
    assert replay.load_event_replay(store, "deleted").state is replay.ReplayState.EVENT_MISSING


@pytest.mark.parametrize(
    "state,expected",
    [
        ("disabled", replay.ReplayState.DISABLED),
        ("cache_disabled", replay.ReplayState.CACHE_DISABLED),
        ("too_large", replay.ReplayState.CAPTURE_TOO_LARGE),
        ("unavailable", replay.ReplayState.SNAPSHOT_UNAVAILABLE),
    ],
)
def test_recorded_snapshot_states_are_not_confused(tmp_path, state, expected, monkeypatch):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record(1, event_id="event", inspection=EventInspection(snapshot_state=state))

    def forbidden(*args, **kwargs):
        pytest.fail("Unavailable snapshots must not trigger content reads")

    monkeypatch.setattr(replay, "read_redacted_content", forbidden)
    result = replay.load_event_replay(store, "event")
    assert result.state is expected
    assert result.before is None and result.unified_diff is None


def test_evicted_snapshot_reports_which_row_is_missing(tmp_path):
    store, cache, inspection = pair(tmp_path, "before", "after")
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute("DELETE FROM output_cache WHERE ref_id=?", (inspection.after_ref,))
    result = replay.load_event_replay(store, "event", cache=cache)
    assert result.state is replay.ReplayState.SNAPSHOT_MISSING
    assert result.before_present is True and result.after_present is False
    assert result.before is None and result.after is None


@pytest.mark.parametrize("payload", [b"ZLIBbroken", b"ZLIB" + zlib.compress(b"data")[:-1], b"\xff"])
def test_corrupt_or_incomplete_payload_is_not_a_diff(tmp_path, payload):
    store, cache, inspection = pair(tmp_path, "before", "after")
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute(
            "UPDATE output_cache SET content=? WHERE ref_id=?", (payload, inspection.before_ref)
        )
    result = replay.load_event_replay(store, "event", cache=cache)
    assert result.state is replay.ReplayState.PAYLOAD_INVALID
    assert result.before_present and result.after_present
    assert result.unified_diff is None


def test_invalid_metadata_never_exposes_error_content(tmp_path):
    store, cache, _ = pair(tmp_path, "before", "after")
    secret = "private raw exception"
    with store.connect() as conn:
        conn.execute("UPDATE event_inspection SET tool_error=?", (secret,))
    result = replay.load_event_replay(store, "event", cache=cache)
    assert result.state is replay.ReplayState.INVALID_METADATA
    assert secret not in repr(result) and secret not in result.message


@pytest.mark.parametrize(
    "kind",
    ["bytes", "lines", "line_length", "compressed_bomb", "stored_payload"],
)
@pytest.mark.parametrize("side", ["before", "after"])
def test_input_limits_refuse_before_running_diff(tmp_path, monkeypatch, kind, side):
    texts = {
        "bytes": "x" * (replay.DIFF_INPUT_BYTES + 1),
        "lines": "x\n" * (replay.DIFF_INPUT_LINES + 1),
        "line_length": "x" * (replay.DIFF_LINE_BYTES + 1),
        "compressed_bomb": "placeholder",
        "stored_payload": "placeholder",
    }
    before, after = (texts[kind], "after") if side == "before" else ("before", texts[kind])
    store, cache, inspection = pair(tmp_path, before, after)
    ref = inspection.before_ref if side == "before" else inspection.after_ref
    if kind in {"compressed_bomb", "stored_payload"}:
        payload = (
            b"ZLIB" + zlib.compress(b"x" * (replay.DIFF_INPUT_BYTES + 1))
            if kind == "compressed_bomb"
            else b"x" * (replay.DIFF_STORED_BYTES + 1)
        )
        with sqlite3.connect(cache.db_path) as conn:
            conn.execute("UPDATE output_cache SET content=? WHERE ref_id=?", (payload, ref))

    def forbidden(*args, **kwargs):
        pytest.fail("Input limits must be checked before diff computation")

    monkeypatch.setattr(replay.difflib, "unified_diff", forbidden)
    result = replay.load_event_replay(store, "event", cache=cache)
    assert result.state is replay.ReplayState.INPUT_LIMIT
    assert result.unified_diff is None and result.before is None


def test_exact_byte_limit_with_acceptable_lines_is_allowed(tmp_path):
    line = "x" * (replay.DIFF_LINE_BYTES - 1) + "\n"
    text = line * (replay.DIFF_INPUT_BYTES // len(line))
    assert len(text.encode()) == replay.DIFF_INPUT_BYTES
    store, cache, _ = pair(tmp_path, text, text)
    assert (
        replay.load_event_replay(store, "event", cache=cache).state is replay.ReplayState.IDENTICAL
    )


@pytest.mark.parametrize("text", ["x\n" * replay.DIFF_INPUT_LINES, "x" * replay.DIFF_LINE_BYTES])
def test_exact_line_limits_are_allowed(tmp_path, text):
    store, cache, _ = pair(tmp_path, text, text)
    assert (
        replay.load_event_replay(store, "event", cache=cache).state is replay.ReplayState.IDENTICAL
    )


def test_unicode_line_limit_counts_bytes_not_characters(tmp_path):
    text = "漢" * (replay.DIFF_LINE_BYTES // 3 + 1)
    assert len(text) < replay.DIFF_LINE_BYTES < len(text.encode("utf-8"))
    store, cache, _ = pair(tmp_path, text, "after")
    assert (
        replay.load_event_replay(store, "event", cache=cache).state
        is replay.ReplayState.INPUT_LIMIT
    )


def test_diff_error_does_not_expose_sqlite_exception_text(tmp_path, monkeypatch):
    store, cache, _ = pair(tmp_path, "before", "after")

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("private credentials in exception")

    monkeypatch.setattr(replay, "_presence", unavailable)
    result = replay.load_event_replay(store, "event", cache=cache)
    assert result.state is replay.ReplayState.SNAPSHOT_UNAVAILABLE
    assert "private credentials" not in result.message and "private credentials" not in repr(result)


def test_eviction_between_presence_and_content_read_is_safe(tmp_path, monkeypatch):
    store, cache, inspection = pair(tmp_path, "before", "after")
    reader = replay.read_redacted_content

    def delete_then_read(cache, ref, **limits):
        with sqlite3.connect(cache.db_path) as conn:
            conn.execute("DELETE FROM output_cache WHERE ref_id=?", (inspection.before_ref,))
        return reader(cache, ref, **limits)

    monkeypatch.setattr(replay, "read_redacted_content", delete_then_read)
    result = replay.load_event_replay(store, "event", cache=cache)
    assert result.state is replay.ReplayState.SNAPSHOT_MISSING
    assert result.before_present is False and result.after_present is True
    assert result.before is None and result.unified_diff is None


def test_output_limit_does_not_return_partial_comparison(tmp_path, monkeypatch):
    store, cache, _ = pair(tmp_path, "before\n", "after\n")
    monkeypatch.setattr(replay, "DIFF_OUTPUT_BYTES", 10)
    result = replay.load_event_replay(store, "event", cache=cache)
    assert result.state is replay.ReplayState.OUTPUT_LIMIT
    assert result.unified_diff is None and result.before is None and result.after is None


def test_decompression_uses_diff_limit_not_four_megabyte_default(tmp_path, monkeypatch):
    store, cache, inspection = pair(tmp_path, "before", "after")
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute(
            "UPDATE output_cache SET content=? WHERE ref_id=?",
            (b"ZLIB" + zlib.compress(b"x" * (replay.DIFF_INPUT_BYTES + 1)), inspection.before_ref),
        )
    real_decoder = zlib.decompressobj
    limits = []

    class Decoder:
        def __init__(self):
            self.decoder = real_decoder()

        def decompress(self, data, max_length):
            limits.append(max_length)
            return self.decoder.decompress(data, max_length)

        def __getattr__(self, name):
            return getattr(self.decoder, name)

    monkeypatch.setattr(zlib, "decompressobj", Decoder)
    result = replay.load_event_replay(store, "event", cache=cache)
    assert result.state is replay.ReplayState.INPUT_LIMIT
    assert limits == [replay.DIFF_INPUT_BYTES + 1]


def test_old_snapshots_are_redacted_after_ansi_removal_and_controls_are_visible(tmp_path):
    store, cache, inspection = pair(tmp_path, "before", "after")
    secret = "ghp_" + "A" * 36
    legacy = "[bold]literal[/bold]\n" + "ghp_\x1b[31m" + "A" * 36 + "\x07\n"
    digest = hashlib.sha256(legacy.encode("utf-8")).hexdigest()
    legacy_ref = "tc_" + digest[:16]
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute(
            "UPDATE output_cache SET ref_id=?,content_hash=?,content=? WHERE ref_id=?",
            (legacy_ref, digest, legacy, inspection.before_ref),
        )
    with store.connect() as conn:
        conn.execute(
            "UPDATE event_inspection SET before_ref=? WHERE event_id='event'", (legacy_ref,)
        )
    result = replay.load_event_replay(store, "event", cache=cache)
    assert result.state is replay.ReplayState.READY
    assert secret not in result.before and "A" * 36 not in result.unified_diff
    assert "REDACTED" in result.before
    assert "\x1b" not in result.before and "\x07" not in result.before
    assert "\\x07" in result.before and "[bold]literal[/bold]" in result.before
    assert legacy not in repr(result) and result.before not in repr(result)


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize(
    "damage",
    ["payload", "hash", "missing_hash", "empty_hash", "short_hash", "long_hash", "blob_hash"],
)
def test_diff_refuses_unverified_content_without_running_comparison(
    tmp_path, monkeypatch, side, damage
):
    store, cache, inspection = pair(tmp_path, "before\n", "after\n")
    ref = inspection.before_ref if side == "before" else inspection.after_ref
    with sqlite3.connect(cache.db_path) as conn:
        digest = conn.execute(
            "SELECT content_hash FROM output_cache WHERE ref_id=?", (ref,)
        ).fetchone()[0]
        if damage == "payload":
            # Still valid UTF-8, with a valid lookup reference and old hash.
            conn.execute(
                "UPDATE output_cache SET content=? WHERE ref_id=?", ("modified text\n", ref)
            )
        else:
            wrong_hash = digest[:16] + ("0" if digest[16] != "0" else "1") + digest[17:]
            value = {
                "hash": wrong_hash,
                "missing_hash": None,
                "empty_hash": "",
                "short_hash": digest[:16],
                "long_hash": digest + "0",
                "blob_hash": digest.encode(),
            }[damage]
            conn.execute("UPDATE output_cache SET content_hash=? WHERE ref_id=?", (value, ref))

    def forbidden(*args, **kwargs):
        pytest.fail("Unverified snapshots must not reach the diff algorithm")

    monkeypatch.setattr(replay.difflib, "unified_diff", forbidden)
    result = replay.load_event_replay(store, "event", cache=cache)
    assert result.state is replay.ReplayState.INTEGRITY_FAILED
    assert result.before is None and result.after is None and result.unified_diff is None
    assert result.before_present and result.after_present


def test_identical_reference_does_not_bypass_integrity_check(tmp_path):
    store, cache, inspection = pair(tmp_path, "same", "same")
    assert inspection.before_ref == inspection.after_ref
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute(
            "UPDATE output_cache SET content='changed' WHERE ref_id=?", (inspection.before_ref,)
        )
    result = replay.load_event_replay(store, "event", cache=cache)
    assert result.state is replay.ReplayState.INTEGRITY_FAILED
    assert result.unified_diff is None


def test_hash_verification_uses_complete_decompressed_utf8_bytes(tmp_path):
    before, after = "żółć before\n", "漢字 after\n"
    store, cache, inspection = pair(tmp_path, before, after)
    with sqlite3.connect(cache.db_path) as conn:
        for ref, text in ((inspection.before_ref, before), (inspection.after_ref, after)):
            conn.execute(
                "UPDATE output_cache SET content=? WHERE ref_id=?",
                (b"ZLIB" + zlib.compress(text.encode("utf-8")), ref),
            )
    result = replay.load_event_replay(store, "event", cache=cache)
    assert result.state is replay.ReplayState.READY
    assert result.before == before and result.after == after
    assert "-żółć before" in result.unified_diff and "+漢字 after" in result.unified_diff


def test_namespaced_refs_use_full_content_hash_not_short_ref_prefix(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    cache = ContextCache(tmp_path / "cache.db")
    before = "plain text before\n"
    before_ref = cache.store(before, namespace="measurement-owner")
    assert before_ref != "tc_" + hashlib.sha256(before.encode()).hexdigest()[:16]
    store.record(
        10,
        event_id="event",
        inspection=EventInspection(
            before_ref=before_ref,
            after_ref=cache.store("plain text after\n"),
            snapshot_state="stored",
        ),
    )
    result = replay.load_event_replay(store, "event", cache=cache)
    assert result.state is replay.ReplayState.READY
    assert result.before == before


def test_integrity_read_is_read_only_and_explorer_keeps_legacy_default(tmp_path):
    from usagetrim.tui.cache_content import read_redacted_content

    store, cache, inspection = pair(tmp_path, "before", "after")
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute(
            "UPDATE output_cache SET content_hash=NULL WHERE ref_id=?", (inspection.before_ref,)
        )
    original = cache.db_path.read_bytes()
    assert read_redacted_content(cache, inspection.before_ref) == "before"
    result = replay.load_event_replay(store, "event", cache=cache)
    assert result.state is replay.ReplayState.INTEGRITY_FAILED
    assert cache.db_path.read_bytes() == original
    with sqlite3.connect(cache.db_path) as conn:
        assert conn.execute("SELECT SUM(hit_count) FROM output_cache").fetchone()[0] == 0


def test_legacy_cache_without_hash_column_cannot_supply_verified_diff(tmp_path):
    from usagetrim.tui.cache_content import read_redacted_content

    path = tmp_path / "old-cache.db"
    text = "legacy snapshot"
    ref = "tc_" + hashlib.sha256(text.encode()).hexdigest()[:16]
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE output_cache (ref_id TEXT PRIMARY KEY, content TEXT)")
        conn.execute("INSERT INTO output_cache VALUES (?, ?)", (ref, text))
    cache = SimpleNamespace(db_path=path)
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record(
        10,
        event_id="event",
        inspection=EventInspection(before_ref=ref, after_ref=ref, snapshot_state="stored"),
    )
    assert read_redacted_content(cache, ref) == text
    result = replay.load_event_replay(store, "event", cache=cache)
    assert result.state is replay.ReplayState.INTEGRITY_FAILED
    assert result.unified_diff is None


def test_diff_does_not_increment_hits_or_use_unbounded_retrieve(tmp_path, monkeypatch):
    store, cache, _ = pair(tmp_path, "before", "after")

    def forbidden(*args, **kwargs):
        pytest.fail("Diff must not use unbounded retrieval")

    monkeypatch.setattr(ContextCache, "retrieve", forbidden)
    assert replay.load_event_replay(store, "event", cache=cache).state is replay.ReplayState.READY
    with sqlite3.connect(cache.db_path) as conn:
        assert conn.execute("SELECT SUM(hit_count) FROM output_cache").fetchone()[0] == 0


@pytest.mark.parametrize(
    "metadata,expected",
    [
        (None, "Not recorded"),
        (EventInspection(child_returncode=0, termination_reason="exited"), "Pass (exit 0)"),
        (EventInspection(child_returncode=7, termination_reason="exited"), "Fail (exit 7)"),
        (EventInspection(child_returncode=-15, termination_reason="signaled"), "Signaled"),
        (EventInspection(termination_reason="timed_out"), "Timed out"),
        (EventInspection(termination_reason="failed_to_start"), "Failed to start"),
        (EventInspection(termination_reason="cancelled"), "Cancelled"),
    ],
)
def test_execution_status_uses_only_recorded_metadata(metadata, expected):
    status = replay.format_event_status(metadata)
    assert status.execution == expected and status.recovery == "Not recorded"


@pytest.mark.parametrize(
    "scope,expected",
    [
        ("full", "Recovered (full redacted content)"),
        ("lines", "Retrieved line range"),
        ("query", "Retrieved search results"),
        (None, "Recovery scope not recorded"),
    ],
)
def test_recovery_is_independent_of_execution_status(scope, expected):
    metadata = EventInspection(
        recovery_ref="tc_0123456789abcdef",
        recovery_scope=scope,
        child_returncode=7,
        termination_reason="exited",
    )
    status = replay.format_event_status(metadata, delivery="returned")
    assert status.recovery == expected and status.execution == "Fail (exit 7)"


def test_prepared_is_not_a_delivery_confirmation():
    metadata = EventInspection(
        child_returncode=0,
        termination_reason="exited",
        recovery_ref="tc_0123456789abcdef",
        recovery_scope="full",
    )
    status = replay.format_event_status(metadata, delivery="prepared")
    assert status.execution == "Pass (exit 0)"
    assert status.delivery == "Prepared (delivery unconfirmed)"
    assert status.recovery == "Prepared recovery (full redacted content; delivery unconfirmed)"


def test_module_import_and_status_api_do_not_require_textual():
    # A fresh interpreter prevents cached transitive imports from hiding an
    # accidental dependency on Textual in any of the reader's dependencies.
    script = """
import builtins
original_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'textual' or name.startswith('textual.'):
        raise AssertionError('Textual must not be imported by replay data')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded
from usagetrim.tui.replay_data import format_event_status
from usagetrim.tui.history import read_history_inspections
assert format_event_status(None).execution == 'Not recorded'
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert result.returncode == 0, result.stderr
