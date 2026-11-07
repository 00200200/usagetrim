"""Incremental CCR artifacts, without a subprocess runner or network."""

import hashlib
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Event

import pytest

from usagetrim.core import execution_artifacts as artifacts
from usagetrim.core.cache import ContextCache
from usagetrim.core.execution_artifacts import CHUNK_BYTES, ArtifactError, ArtifactState
from usagetrim.core.stream_redactor import LINE_LIMIT, MASK


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGETRIM_CACHE_MAX_BYTES", str(1024 * 1024))
    return ContextCache(tmp_path / "cache.db")


def stored(cache):
    with sqlite3.connect(cache.db_path) as conn:
        return b"".join(
            row[0]
            for row in conn.execute("SELECT data FROM execution_chunks ORDER BY ref_id,sequence")
        )


def test_empty_artifact_is_available_and_complete(cache):
    writer = cache.begin_artifact()
    assert re.fullmatch(r"tc_[0-9a-f]{16}", writer.ref_id)
    assert not cache.artifact_metadata(writer.ref_id).complete
    metadata = writer.finish()
    assert metadata.state is ArtifactState.COMPLETE and metadata.available
    assert metadata.line_count == metadata.stored_bytes == metadata.chunk_count == 0
    assert metadata.content_hash == hashlib.sha256(b"").hexdigest()
    assert cache.read_artifact(writer.ref_id).text == ""
    assert list(cache.iter_artifact(writer.ref_id)) == []


@pytest.mark.parametrize("finished", [False, True])
def test_artifact_search_stays_bounded_and_does_not_invoke_semantic_backend(
    cache, monkeypatch, finished
):
    from usagetrim.core import semantic_search

    def no_semantic_search(*args, **kwargs):
        pytest.fail("Execution artifact anchors must not enter semantic search")

    monkeypatch.setattr(semantic_search, "rank_chunks", no_semantic_search)
    legacy = cache.store("legacy diagnostic", source="review")
    assert "legacy diagnostic" in cache.hybrid_search(legacy, "diagnostic")
    writer = cache.begin_artifact()
    writer.append(b"execution diagnostic\n")
    if finished:
        writer.finish()
    notice = cache.search(writer.ref_id, "diagnostic")
    assert "not indexed" in notice and "line-range recovery" in notice
    assert cache.hybrid_search(writer.ref_id, "diagnostic") == notice
    with pytest.raises(ValueError, match="budget is too small"):
        cache.hybrid_search(writer.ref_id, "diagnostic", budget=1)
    with sqlite3.connect(cache.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM recovery_search WHERE ref=?", (writer.ref_id,)
        ).fetchone() == (0,)
    if not finished:
        writer.abort()


def test_many_small_appends_and_incremental_utf8(cache):
    data = ("Zażółć 漢字🙂\n" * 8 + "no newline").encode()
    writer = cache.begin_artifact()
    for byte in data:
        writer.append(bytes([byte]))
    metadata = writer.finish()
    assert metadata.complete and metadata.line_count == 9
    assert cache.read_artifact(writer.ref_id).text == data.decode()
    assert b"".join(part.data for part in cache.iter_artifact(writer.ref_id)) == data
    assert metadata.replacement_occurred is None


def test_large_log_is_never_materialized_by_the_writer(cache):
    writer = cache.begin_artifact()
    digest = hashlib.sha256()
    for index in range(200):
        part = (f"frame {index:04d}: " + "diagnostic " * 40 + "\n").encode()
        writer.append(part)
        digest.update(part)
        assert len(writer._buffer) < CHUNK_BYTES
        assert writer._redactor._line_bytes <= LINE_LIMIT
    metadata = writer.finish()
    assert metadata.complete and metadata.content_hash == digest.hexdigest()
    assert metadata.stored_bytes > artifacts.READ_LIMIT
    read = cache.read_artifact(writer.ref_id)
    assert read.metadata.complete and read.response_truncated
    assert len(read.text.encode()) <= artifacts.READ_LIMIT
    assert all(len(chunk.data) <= CHUNK_BYTES for chunk in cache.iter_artifact(writer.ref_id))


def test_long_single_line_is_unavailable_not_a_complete_partial_log(cache):
    writer = cache.begin_artifact()
    writer.append(b"prefix\n" + b"x" * (LINE_LIMIT * 4) + b"\nSHOULD_NOT_RETURN\n")
    metadata = writer.finish()
    result = cache.read_artifact(writer.ref_id)
    assert metadata.state is ArtifactState.INCOMPLETE and metadata.available
    assert metadata.reason == "redaction_unavailable"
    assert "unavailable" in result.text and "SHOULD_NOT_RETURN" not in result.text
    assert not result.response_truncated


def test_ranges_use_only_relevant_chunks_and_do_not_increment_hits(cache, monkeypatch):
    writer = cache.begin_artifact()
    for start in range(0, 3000, 100):
        writer.append("".join(f"row {index:05d}\n" for index in range(start, start + 100)).encode())
    metadata = writer.finish()
    visited = []
    original = artifacts._fragments

    def track(*args):
        for fragment in original(*args):
            visited.append(fragment.sequence)
            yield fragment

    monkeypatch.setattr(artifacts, "_fragments", track)
    result = cache.read_artifact(writer.ref_id, first_line=2000, last_line=2001)
    assert result.text == "row 01999\nrow 02000\n"
    assert not result.response_truncated
    assert visited[0] > 0 and len(visited) < metadata.chunk_count
    with sqlite3.connect(cache.db_path) as conn:
        assert (
            conn.execute(
                "SELECT hit_count FROM output_cache WHERE ref_id=?", (writer.ref_id,)
            ).fetchone()[0]
            == 0
        )


def test_response_limit_does_not_damage_utf8_or_storage_completeness(cache):
    writer = cache.begin_artifact()
    writer.append("漢字🙂\n".encode())
    writer.finish()
    result = cache.read_artifact(writer.ref_id, max_bytes=4)
    assert result.text == "漢"
    assert result.response_truncated and result.metadata.complete
    assert cache.read_artifact(writer.ref_id).text == "漢字🙂\n"


def test_invalid_utf8_is_replaced_without_storing_invalid_bytes(cache):
    writer = cache.begin_artifact()
    writer.append(b"example \xff\xf0\x9f\n")
    metadata = writer.finish()
    assert metadata.complete
    assert cache.read_artifact(writer.ref_id).text == "example ��\n"
    assert stored(cache).decode("utf-8") == "example ��\n"


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_" + "A" * 36,
        "sk-ant-api03-" + "B" * 70,
        "Bearer " + "C" * 100,
        "AKIA" + "D" * 16,
        "eyJ" + "E" * 20 + ".eyJ" + "F" * 20 + "." + "G" * 20,
        "mysql://user:password@example/db",
        "-----BEGIN PRIVATE KEY-----\nPRIVATE_BODY_EXAMPLE\n-----END PRIVATE KEY-----",
        "gh\x1b]0;title\x07p_" + "H" * 36,
    ],
)
def test_no_raw_secrets_are_written_even_before_finish(cache, secret):
    writer = cache.begin_artifact()
    payload = (secret + "\n").encode()
    for start in range(0, len(payload), 3):
        writer.append(payload[start : start + 3])
        assert secret.encode() not in cache.db_path.read_bytes()
    metadata = writer.finish()
    assert metadata.complete
    result = cache.read_artifact(writer.ref_id)
    assert MASK in result.text and secret not in result.text
    assert "PRIVATE_BODY_EXAMPLE" not in result.text
    assert "H" * 36 not in result.text
    for path in cache.db_path.parent.glob("cache.db*"):
        assert secret.encode() not in path.read_bytes()
    assert secret not in repr(result)


def test_abort_preserves_safe_records_but_discards_unconfirmed_raw_line(cache):
    writer = cache.begin_artifact()
    writer.append(b"safe record\nprivate unfinished")
    metadata = writer.abort()
    assert metadata.state is ArtifactState.INCOMPLETE and metadata.reason == "interrupted"
    assert cache.read_artifact(writer.ref_id).text == "safe record\n"
    assert b"private unfinished" not in cache.db_path.read_bytes()
    with pytest.raises(ArtifactError, match="closed"):
        writer.append(b"later")


def test_flush_never_publishes_a_quarantined_credential_prefix(cache):
    writer = cache.begin_artifact()
    writer.append(b"safe record\nghp_" + b"A" * 20)
    writer.flush()
    result = cache.read_artifact(writer.ref_id)
    assert result.metadata.state is ArtifactState.WRITING
    assert result.text == "safe record\n"
    assert b"ghp_" not in cache.db_path.read_bytes()
    writer.append(b"A" * 16 + b"\n")
    writer.finish()
    assert cache.read_artifact(writer.ref_id).text == "safe record\n" + MASK + "\n"


def test_context_manager_without_finish_is_incomplete(cache):
    with cache.begin_artifact() as writer:
        writer.append(b"safe\n")
    assert cache.artifact_metadata(writer.ref_id).state is ArtifactState.INCOMPLETE


def test_abandoned_write_is_fenced_and_never_inferred_complete(cache):
    writer = cache.begin_artifact()
    writer.append(b"safe\n" * 2000)
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute("UPDATE execution_artifacts SET touched=0 WHERE ref_id=?", (writer.ref_id,))
    reopened = ContextCache(cache.db_path)
    assert reopened.artifact_metadata(writer.ref_id).state is ArtifactState.WRITING
    assert reopened.expire_artifact_writes(stale_before=1) == 1
    assert reopened.artifact_metadata(writer.ref_id).reason == "lease_expired"
    with pytest.raises(ArtifactError, match="closed"):
        writer.append(b"late")


def test_quota_failure_keeps_only_previously_committed_safe_chunks(cache):
    budget = (
        artifacts.ARTIFACT_CHARGE
        + artifacts.EXECUTION_CHARGE
        + artifacts.OBSERVATION_CHARGE
        + artifacts.CHUNK_CHARGE
        + CHUNK_BYTES
        + 100
    )
    writer = cache.begin_artifact(quota_bytes=budget)
    with pytest.raises(ArtifactError, match="quota"):
        writer.append(b"safe diagnostic\n" * 1000)
    metadata = cache.artifact_metadata(writer.ref_id)
    assert metadata.state is ArtifactState.INCOMPLETE and metadata.reason == "quota"
    assert metadata.chunk_count == 1
    assert metadata.stored_bytes == CHUNK_BYTES
    assert cache.read_artifact(writer.ref_id).metadata.available


def test_active_limit_and_legacy_eviction_protect_writers(cache):
    writers = [cache.begin_artifact() for _ in range(artifacts.MAX_ACTIVE)]
    with pytest.raises(ArtifactError, match="active_limit"):
        cache.begin_artifact()
    old = cache.store("legacy eligible for eviction")
    assert cache.evict_lri(max_items_to_remove=10) == [old]
    for writer in writers:
        assert cache.artifact_metadata(writer.ref_id).state is ArtifactState.WRITING
        writer.abort()


def test_shared_quota_evicts_completed_artifact_and_its_chunks(cache):
    first = cache.begin_artifact(quota_bytes=3000)
    first.append(b"safe\n" * 100)
    first.finish()
    second = cache.begin_artifact(quota_bytes=3000)
    second.append(b"safe\n" * 100)
    second.finish()
    assert cache.artifact_metadata(first.ref_id).state is ArtifactState.MISSING
    assert cache.artifact_metadata(second.ref_id).complete
    with sqlite3.connect(cache.db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM execution_chunks WHERE ref_id=?", (first.ref_id,)
            ).fetchone()[0]
            == 0
        )


def test_parallel_artifacts_are_isolated(cache):
    barrier = Barrier(2)

    def capture(label):
        with cache.begin_artifact() as writer:
            barrier.wait(timeout=5)
            for _ in range(20):
                writer.append((label + " diagnostic\n").encode())
            writer.finish()
            return writer.ref_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        one = executor.submit(capture, "first")
        two = executor.submit(capture, "second")
        first, second = one.result(timeout=10), two.result(timeout=10)
    assert first != second
    assert cache.read_artifact(first).text == "first diagnostic\n" * 20
    assert cache.read_artifact(second).text == "second diagnostic\n" * 20


def test_overlapping_calls_to_one_writer_fail_without_damaging_its_owner(cache, monkeypatch):
    entered, release = Event(), Event()
    writer = cache.begin_artifact()
    observe = writer._observe

    def blocked(input_bytes):
        entered.set()
        assert release.wait(timeout=5)
        return observe(input_bytes)

    monkeypatch.setattr(writer, "_observe", blocked)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(writer.append, b"safe\n")
        try:
            assert entered.wait(timeout=5)
            with pytest.raises(ArtifactError, match="busy"):
                writer.append(b"other\n")
        finally:
            release.set()
        future.result(timeout=10)
    writer.finish()
    assert cache.read_artifact(writer.ref_id).text == "safe\n"


@pytest.mark.parametrize(
    "damage", ["payload", "checksum", "index", "missing_chunk", "large_payload", "aggregate"]
)
def test_corruption_never_returns_a_plausible_complete_log(cache, damage):
    writer = cache.begin_artifact()
    writer.append(b"safe record\n")
    writer.finish()
    with sqlite3.connect(cache.db_path) as conn:
        if damage == "payload":
            conn.execute("UPDATE execution_chunks SET data=?", (b"different UTF-8\n",))
        elif damage == "checksum":
            conn.execute("UPDATE execution_chunks SET checksum='invalid'")
        elif damage == "index":
            conn.execute("UPDATE execution_chunks SET first_line=2,last_line=2")
        elif damage == "missing_chunk":
            conn.execute("DELETE FROM execution_chunks")
        elif damage == "large_payload":
            conn.execute("UPDATE execution_chunks SET data=?", (b"x" * (CHUNK_BYTES + 1),))
        else:
            conn.execute("UPDATE execution_artifacts SET content_hash=?", ("0" * 64,))
    result = cache.read_artifact(writer.ref_id)
    assert result.metadata.state is ArtifactState.CORRUPT
    assert not result.metadata.complete and result.text == ""


def test_missing_and_evicted_records_are_distinct_from_empty(cache):
    assert cache.read_artifact("tc_missing").metadata.state is ArtifactState.MISSING
    writer = cache.begin_artifact()
    writer.finish()
    assert cache.evict_lri(max_items_to_remove=1) == [writer.ref_id]
    assert not cache.read_artifact(writer.ref_id).metadata.available


@pytest.mark.parametrize(
    "column,value",
    [
        ("content_hash", ""),
        ("state", "unknown"),
        ("policy", "unknown"),
        ("reason", "PRIVATE_ERROR_DETAIL"),
        ("newlines", -1),
        ("ends_newline", 2),
    ],
)
def test_invalid_metadata_is_not_exposed_as_valid_or_as_an_error_body(cache, column, value):
    writer = cache.begin_artifact()
    writer.append(b"safe\n")
    writer.finish()
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute(
            f"UPDATE execution_artifacts SET {column}=? WHERE ref_id=?", (value, writer.ref_id)
        )
    result = cache.read_artifact(writer.ref_id)
    assert result.metadata.state is ArtifactState.CORRUPT and result.text == ""
    assert "PRIVATE_ERROR_DETAIL" not in repr(result)


def test_storage_error_is_sanitized_and_releases_the_writer(cache, monkeypatch):
    writer = cache.begin_artifact()

    def fail(*args):
        raise sqlite3.OperationalError("disk full: PRIVATE_ERROR_CONTENT")

    with monkeypatch.context() as patch:
        patch.setattr(artifacts, "_reserve", fail)
        with pytest.raises(ArtifactError, match="storage_error") as caught:
            writer.append(b"safe\n" * 1000)
    assert "PRIVATE_ERROR_CONTENT" not in str(caught.value)
    metadata = cache.artifact_metadata(writer.ref_id)
    assert metadata.state is ArtifactState.INCOMPLETE and metadata.stored_bytes == 0


def test_unwritable_database_cannot_turn_a_write_failure_into_success(cache, monkeypatch):
    writer = cache.begin_artifact()

    @contextmanager
    def denied(*args, **kwargs):
        raise PermissionError("private path")
        yield

    with monkeypatch.context() as patch:
        patch.setattr(artifacts, "_connect", denied)
        with pytest.raises(ArtifactError, match="storage_error") as caught:
            writer.append(b"private pending")
    assert "private path" not in str(caught.value)
    assert cache.artifact_metadata(writer.ref_id).state is ArtifactState.WRITING
    assert not cache.artifact_metadata(writer.ref_id).complete


def test_incomplete_committed_unicode_prefix_remains_readable(cache):
    writer = cache.begin_artifact()
    writer.append(("漢" * 2000 + "\n").encode())
    active = cache.read_artifact(writer.ref_id)
    assert active.metadata.state is ArtifactState.WRITING
    assert active.text == "漢" * 1365
    assert not active.metadata.complete
    metadata = writer.abort()
    result = cache.read_artifact(writer.ref_id)
    assert metadata.state is ArtifactState.INCOMPLETE
    assert result.text == "漢" * 2000 + "\n"


def test_legacy_refs_search_and_clear_remain_compatible(cache):
    original = "legacy diagnostic\n" * 100
    ref = cache.store(original)
    assert ref == "tc_" + hashlib.sha256(original.encode()).hexdigest()[:16]
    writer = cache.begin_artifact()
    writer.append(b"new artifact\n")
    writer.finish()
    assert cache.retrieve(ref) == original
    assert "legacy diagnostic" in cache.search(ref, "diagnostic")
    assert "storage_complete=True" in cache.retrieve(writer.ref_id)
    assert "not indexed" in cache.search(writer.ref_id, "diagnostic")
    cache.clear()
    assert cache.artifact_metadata(writer.ref_id).state is ArtifactState.MISSING
    with sqlite3.connect(cache.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM execution_chunks").fetchone()[0] == 0


def test_old_schema_is_preserved_and_artifact_schema_is_additive(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE output_cache(ref_id TEXT PRIMARY KEY,content_hash TEXT,source TEXT,content TEXT,created_at REAL)"
        )
        conn.execute("INSERT INTO output_cache VALUES ('tc_old','oldhash','old','old log',1)")
    cache = ContextCache(path)
    assert cache.retrieve("tc_old") == "old log"
    writer = cache.begin_artifact()
    writer.append(b"new record\n")
    writer.finish()
    assert cache.retrieve("tc_old") == "old log"
    assert cache.read_artifact(writer.ref_id).text == "new record\n"


def test_quota_looks_at_legacy_blob_lengths_without_reading_the_payload(cache, monkeypatch):
    legacy = "old uncompressed record\n" * 5000
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute(
            "INSERT INTO output_cache(ref_id,content_hash,content,source,created_at) VALUES ('tc_old','hash',?,'old',1)",
            (legacy,),
        )
    original = artifacts._connect
    seen = []

    class Blob:
        def __init__(self, raw, table):
            self.raw, self.table = raw, table

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.raw.close()

        def __len__(self):
            return len(self.raw)

        def read(self, size):
            assert self.table != "output_cache", (
                "Legacy payload must not be copied for quota checks"
            )
            assert size <= CHUNK_BYTES + 1
            return self.raw.read(size)

    class Connection:
        def __init__(self, raw):
            self.raw = raw

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def blobopen(self, table, *args, **kwargs):
            seen.append(table)
            return Blob(self.raw.blobopen(table, *args, **kwargs), table)

    @contextmanager
    def instrument(*args, **kwargs):
        with original(*args, **kwargs) as conn:
            yield Connection(conn)

    with monkeypatch.context() as patch:
        patch.setattr(artifacts, "_connect", instrument)
        writer = cache.begin_artifact()
        writer.append(b"new\n")
        writer.finish()
        assert cache.read_artifact(writer.ref_id).text == "new\n"
    assert "output_cache" in seen and "execution_chunks" in seen
    assert cache.retrieve("tc_old") == legacy


def test_corrupt_database_file_has_a_safe_state(cache):
    writer = cache.begin_artifact()
    writer.finish()
    cache.db_path.write_bytes(b"this is not a SQLite database")
    result = cache.read_artifact(writer.ref_id)
    assert result.metadata.state is ArtifactState.CORRUPT
    assert not result.metadata.available and result.text == ""


@pytest.mark.parametrize("damage", ["metadata", "table"])
def test_orphan_anchor_is_never_misrepresented_as_an_empty_recovery(cache, damage):
    writer = cache.begin_artifact()
    writer.finish()
    with sqlite3.connect(cache.db_path) as conn:
        if damage == "metadata":
            conn.execute("DELETE FROM execution_artifacts WHERE ref_id=?", (writer.ref_id,))
        else:
            conn.execute("DROP TABLE execution_artifacts")
    result = cache.read_artifact(writer.ref_id)
    assert result.metadata.state is ArtifactState.CORRUPT
    assert "state=corrupt" in cache.retrieve(writer.ref_id)


def test_failed_quota_reservation_rolls_back_eviction(cache):
    ref = cache.store("legacy log to preserve")
    with pytest.raises(ArtifactError, match="quota"):
        cache.begin_artifact(quota_bytes=1)
    assert cache.retrieve(ref) == "legacy log to preserve"


def test_iterator_releases_database_before_yield_and_detects_eviction(cache):
    writer = cache.begin_artifact()
    writer.append(b"safe\n" * 2000)
    writer.finish()
    iterator = cache.iter_artifact(writer.ref_id)
    fragment = next(iterator)
    assert len(fragment.data) <= CHUNK_BYTES
    cache.clear()
    with pytest.raises(ArtifactError, match="corrupt"):
        list(iterator)


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), True, "invalid"])
def test_expiry_timestamp_is_validated_before_mutation(cache, timestamp):
    with pytest.raises(ValueError, match="finite timestamp"):
        cache.expire_artifact_writes(stale_before=timestamp)


@pytest.mark.parametrize(
    "options",
    [
        {"max_bytes": 0},
        {"max_bytes": artifacts.READ_LIMIT + 1},
        {"first_line": 0},
        {"first_line": True},
        {"first_line": 5, "last_line": 4},
    ],
)
def test_read_limit_validation(cache, options):
    with pytest.raises(ValueError):
        cache.read_artifact("tc_missing", **options)
