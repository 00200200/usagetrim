"""Execution identity, independent streams, observation order and quota accounting."""

import hashlib
import importlib.util
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier

import pytest

from usagetrim.core import artifact_quota
from usagetrim.core import execution_artifacts as artifacts
from usagetrim.core.cache import ContextCache
from usagetrim.core.execution_artifacts import ArtifactError, ArtifactState, ArtifactStream
from usagetrim.core.stream_redactor import MASK, StreamingRedactor


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGETRIM_CACHE_MAX_BYTES", str(1024 * 1024))
    return ContextCache(tmp_path / "cache.db")


def pair(cache):
    stdout = cache.begin_artifact(stream="stdout")
    stderr = cache.begin_artifact(stream="stderr", execution_id=stdout.execution_id)
    return stdout, stderr


def test_identity_order_and_recovery_metadata_do_not_invent_a_merged_view(cache):
    stdout, stderr = pair(cache)
    first = stdout.append(b"first partial")
    second = stderr.append(b"error line\n")
    third = stdout.append(b" line\n")
    stdout.finish()
    stderr.finish()
    assert [first.sequence, second.sequence, third.sequence] == [0, 1, 2]
    observations = list(cache.iter_execution_observations(stdout.execution_id))
    assert observations == [first, second, third]
    assert [item.stream for item in observations] == [
        ArtifactStream.STDOUT,
        ArtifactStream.STDERR,
        ArtifactStream.STDOUT,
    ]
    recovery = cache.execution_recovery(stdout.execution_id)
    assert recovery.recorded and recovery.observed_count == recovery.retained_observations == 3
    assert {item.stream for item in recovery.streams} == {
        ArtifactStream.STDOUT,
        ArtifactStream.STDERR,
    }
    assert all(item.complete and item.available for item in recovery.streams)
    assert cache.read_artifact(stdout.ref_id).text == "first partial line\n"
    assert cache.read_artifact(stderr.ref_id).text == "error line\n"
    fragment = next(cache.iter_artifact(stdout.ref_id))
    assert fragment.execution_id == stdout.execution_id and fragment.stream is ArtifactStream.STDOUT
    assert (fragment.observation_first, fragment.observation_last) == (0, 2)
    assert "first partial" not in repr(observations) + repr(recovery) + repr(fragment)


def test_decoders_do_not_mix_utf8_state_between_streams(cache):
    stdout, stderr = pair(cache)
    stdout.append(b"\xe6\xbc")
    stderr.append(b"stderr \xff\n")
    stdout.append(b"\xa2\n")
    stdout.finish()
    stderr.finish()
    assert cache.read_artifact(stdout.ref_id).text == "漢\n"
    assert cache.read_artifact(stderr.ref_id).text == "stderr �\n"


@pytest.mark.parametrize("unfinished", [False, True])
def test_scalar_and_ascii_paths_preserve_ccr_fragments_and_observation_spans(
    tmp_path, monkeypatch, unfinished
):
    path = Path(__file__).parent / "fixtures/execution/redactor_reference.py"
    spec = importlib.util.spec_from_file_location("stream_scalar_reference", path)
    reference = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reference)
    events = [
        (0, b"safe\tline\n"),
        (1, b"stderr\xff\n"),
        (0, b"gh\x1b[31m"),
        (1, b"CR\r"),
        (0, b"p_" + b"A" * 36 + b"\n"),
        (1, b"\nnext\n"),
        (0, b"\xe6\xbc"),
        (1, b"plain no newline"),
        (0, b"\xa2\n"),
    ]
    if unfinished:
        events.append((1, b"\x1b]unfinished"))

    def capture(redactor_type, db):
        monkeypatch.setattr(artifacts, "StreamingRedactor", redactor_type)
        cache = ContextCache(db)
        writers = pair(cache)
        for source, data in events:
            writers[source].append(data)
        summaries = []
        for writer in writers:
            metadata = writer.finish()
            fragments = [
                (
                    part.sequence,
                    part.first_line,
                    part.last_line,
                    part.data,
                    part.observation_first,
                    part.observation_last,
                )
                for part in cache.iter_artifact(writer.ref_id)
            ]
            summaries.append(
                (
                    metadata.complete,
                    metadata.available,
                    metadata.stored_bytes,
                    metadata.line_count,
                    metadata.content_hash,
                    fragments,
                )
            )
        return summaries

    before = capture(reference.ScalarRedactor, tmp_path / "scalar.db")
    after = capture(StreamingRedactor, tmp_path / "ascii.db")
    assert after == before
    assert after[0][0] is True
    assert after[1][0] is (not unfinished)


def test_unregistered_stream_is_distinct_from_a_completed_empty_stream(cache):
    stdout = cache.begin_artifact(stream="stdout")
    stdout.finish()
    first = cache.execution_recovery(stdout.execution_id)
    assert len(first.streams) == 1 and first.streams[0].stream is ArtifactStream.STDOUT
    assert first.streams[0].available and first.streams[0].complete
    stderr = cache.begin_artifact(execution_id=stdout.execution_id, stream="stderr")
    stderr.finish()
    final = cache.execution_recovery(stdout.execution_id)
    assert len(final.streams) == 2 and all(
        item.available and item.complete for item in final.streams
    )
    assert final.observed_count == final.retained_observations == 0


def test_redactors_do_not_mix_pending_credentials_or_pem_context(cache):
    stdout, stderr = pair(cache)
    stdout.append(b"ghp_" + b"A" * 18)
    stderr.append(b"ordinary error\n")
    stdout.append(b"A" * 18 + b"\n")
    stdout.append(b"-----BEGIN PRIVATE KEY-----\nPRIVATE_BODY")
    stderr.append(b"still readable\n")
    stdout.append(b"\n-----END PRIVATE KEY-----\n")
    stdout.finish()
    stderr.finish()
    result = cache.read_artifact(stdout.ref_id)
    assert "A" * 36 not in result.text and "PRIVATE_BODY" not in result.text
    assert MASK in result.text
    assert cache.read_artifact(stderr.ref_id).text == "ordinary error\nstill readable\n"
    with sqlite3.connect(cache.db_path) as conn:
        for (data,) in conn.execute("SELECT data FROM execution_chunks"):
            assert b"A" * 36 not in data and b"PRIVATE_BODY" not in data
    assert b"A" * 36 not in cache.db_path.read_bytes()
    assert b"PRIVATE_BODY" not in cache.db_path.read_bytes()


def test_observation_span_covers_utf8_carry_and_finalization(cache):
    writer = cache.begin_artifact(stream="stdout")
    writer.append(b"\xe6")
    writer.append(b"\xbc\xa2")
    writer.finish()
    fragment = next(cache.iter_artifact(writer.ref_id))
    assert fragment.data.decode() == "漢"
    assert (fragment.observation_first, fragment.observation_last) == (0, 1)


def test_observation_spans_are_checked_as_part_of_chunk_integrity(cache):
    writer = cache.begin_artifact(stream="stdout")
    writer.append(b"line\n")
    writer.finish()
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute(
            "UPDATE execution_chunk_origins SET observation_last=99 WHERE ref_id=?",
            (writer.ref_id,),
        )
    assert cache.read_artifact(writer.ref_id).metadata.state is ArtifactState.CORRUPT


def test_observation_registry_does_not_accept_a_wrong_stream_label(cache):
    writer = cache.begin_artifact(stream="stdout")
    writer.append(b"line\n")
    writer.finish()
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute("UPDATE execution_observations SET stream='stderr'")
    with pytest.raises(ArtifactError, match="corrupt"):
        list(cache.iter_execution_observations(writer.execution_id))


@pytest.mark.parametrize(
    "options",
    [
        {"execution_id": "private-command-or-key"},
        {"execution_id": "12345678123412349234123456789abc"},
        {"execution_id": "0" * 32},
        {"stream": "private-command-or-key"},
    ],
)
def test_bad_identifiers_are_rejected_without_persistence(cache, options):
    with pytest.raises(ValueError) as caught:
        cache.begin_artifact(**options)
    assert "private-command-or-key" not in str(caught.value)
    with sqlite3.connect(cache.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM output_cache").fetchone()[0] == 0


def test_source_identity_is_immutable_and_cannot_be_registered_twice(cache):
    stdout, stderr = pair(cache)
    with pytest.raises(AttributeError):
        stdout.stream = ArtifactStream.STDERR
    with pytest.raises(ArtifactError, match="identity_conflict"):
        cache.begin_artifact(execution_id=stdout.execution_id, stream="stdout")
    with pytest.raises(ArtifactError, match="identity_conflict"):
        cache.begin_artifact(execution_id=stdout.execution_id, stream="merged")
    stdout.abort()
    stderr.abort()


def test_merged_default_does_not_claim_stdout_or_stderr(cache):
    writer = cache.begin_artifact()
    writer.append(b"merged input\n")
    metadata = writer.finish()
    assert metadata.stream is ArtifactStream.MERGED
    assert metadata.execution_id == writer.execution_id
    with pytest.raises(ArtifactError, match="identity_conflict"):
        cache.begin_artifact(execution_id=writer.execution_id, stream="stderr")


def test_parallel_streams_get_unique_global_observations(cache):
    stdout, stderr = pair(cache)
    barrier = Barrier(2)

    def capture(writer, label):
        barrier.wait(timeout=5)
        result = [writer.append(f"{label} {i}\n".encode()) for i in range(10)]
        writer.finish()
        return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(capture, stdout, "out")
        second = executor.submit(capture, stderr, "err")
        accepted = first.result(timeout=10) + second.result(timeout=10)
    observed = list(cache.iter_execution_observations(stdout.execution_id))
    assert [item.sequence for item in observed] == list(range(20))
    assert observed == sorted(accepted, key=lambda item: item.sequence)
    for stream in (ArtifactStream.STDOUT, ArtifactStream.STDERR):
        assert len([item for item in observed if item.stream is stream]) == 10


def test_evicted_stream_remains_registered_as_missing_and_events_have_explicit_gaps(cache):
    stdout, stderr = pair(cache)
    stdout.append(b"out\n")
    stderr.append(b"err\n")
    stdout.finish()
    assert cache.evict_lri(max_items_to_remove=1) == [stdout.ref_id]
    metadata = cache.execution_recovery(stdout.execution_id)
    streams = {item.stream: item for item in metadata.streams}
    assert streams[ArtifactStream.STDOUT].state is ArtifactState.MISSING
    assert streams[ArtifactStream.STDERR].state is ArtifactState.WRITING
    assert metadata.observed_count == 2 and metadata.retained_observations == 1
    assert [item.sequence for item in cache.iter_execution_observations(stdout.execution_id)] == [1]
    stderr.abort()


def test_registering_second_stream_cannot_evict_its_own_execution_group(cache):
    stdout = cache.begin_artifact(stream="stdout")
    stdout.append(b"out\n")
    stdout.finish()
    with pytest.raises(ArtifactError, match="quota"):
        cache.begin_artifact(execution_id=stdout.execution_id, stream="stderr", quota_bytes=2000)
    assert cache.read_artifact(stdout.ref_id).text == "out\n"
    assert cache.execution_recovery(stdout.execution_id).observed_count == 1


def expected_usage(cache):
    with sqlite3.connect(cache.db_path) as conn:
        legacy = sum(
            len(value.encode()) if isinstance(value, str) else len(value or b"")
            for (value,) in conn.execute("SELECT content FROM output_cache")
        )
        payload = conn.execute(
            "SELECT COALESCE(SUM(stored_bytes+chunk_count*?+?),0) FROM execution_artifacts",
            (artifacts.CHUNK_CHARGE, artifacts.ARTIFACT_CHARGE),
        ).fetchone()[0]
        groups = conn.execute("SELECT COUNT(*) FROM execution_groups").fetchone()[0]
        observations = conn.execute("SELECT COUNT(*) FROM execution_observations").fetchone()[0]
        return (
            legacy
            + payload
            + groups * artifacts.EXECUTION_CHARGE
            + observations * artifacts.OBSERVATION_CHARGE
        )


def assert_usage(cache):
    with artifacts._connect(cache) as conn:
        assert artifacts._usage(conn) == expected_usage(cache)


def test_ledger_tracks_legacy_upserts_streams_observations_eviction_and_clear(cache):
    legacy = cache.store("legacy before accounting")
    stdout, stderr = pair(cache)
    assert_usage(cache)
    stdout.append(b"first\n")
    stderr.append(b"second\n")
    stdout.flush()
    assert_usage(cache)
    assert cache.store("legacy before accounting") == legacy
    cache.store("legacy after accounting")
    assert_usage(cache)
    stdout.finish()
    stderr.abort()
    assert_usage(cache)
    cache.evict_lri(max_items_to_remove=1)
    assert_usage(cache)
    cache.clear()
    assert_usage(cache)
    with artifacts._connect(cache) as conn:
        assert artifacts._usage(conn) == 0


def test_triggers_track_a_legacy_connection_opened_before_accounting(cache):
    old = sqlite3.connect(cache.db_path)
    try:
        old.execute("SELECT COUNT(*) FROM output_cache").fetchone()
        writer = cache.begin_artifact()
        old.execute("INSERT INTO output_cache(ref_id,content) VALUES ('tc_external','one')")
        old.commit()
        assert_usage(cache)
        old.execute("UPDATE output_cache SET content='a longer value' WHERE ref_id='tc_external'")
        old.commit()
        assert_usage(cache)
        old.execute("DELETE FROM output_cache WHERE ref_id='tc_external'")
        old.commit()
        assert_usage(cache)
        writer.abort()
    finally:
        old.close()


def test_accounting_rolls_back_payload_and_evictions_atomically(cache):
    ref = cache.store("legacy retained after rollback")
    writer = cache.begin_artifact()
    with artifacts._connect(cache) as conn:
        before = artifacts._usage(conn)
    with pytest.raises(RuntimeError, match="rollback"):
        with artifacts._connect(cache, write=True) as conn:
            conn.execute("DELETE FROM output_cache WHERE ref_id=?", (ref,))
            conn.execute(
                "UPDATE execution_artifacts SET stored_bytes=stored_bytes+5 WHERE ref_id=?",
                (writer.ref_id,),
            )
            raise RuntimeError("rollback")
    with artifacts._connect(cache) as conn:
        assert artifacts._usage(conn) == before
    assert cache.retrieve(ref) == "legacy retained after rollback"
    writer.abort()


def test_write_hot_path_never_rescans_legacy_payloads(cache, monkeypatch):
    for index in range(30):
        cache.store(f"legacy {index}")
    writer = cache.begin_artifact()

    def forbidden(*args, **kwargs):
        raise AssertionError("accounting reseeded in write hot path")

    monkeypatch.setattr(artifact_quota, "_seed_sizes", forbidden)
    original = artifacts._connect
    statements = []

    @contextmanager
    def instrument(*args, **kwargs):
        with original(*args, **kwargs) as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(artifacts, "_connect", instrument)
    for _ in range(40):
        writer.append(b"safe diagnostic\n" * 100)
    writer.finish()
    another = cache.begin_artifact()
    another.finish()
    assert not any("SELECT rowid,ref_id,typeof(content)" in sql for sql in statements)
    assert not any("SUM(stored_bytes" in sql for sql in statements)
    assert any("SELECT bytes FROM execution_quota" in sql for sql in statements)
    assert_usage(cache)


def test_append_and_flush_do_not_use_a_second_heartbeat_transaction(cache, monkeypatch):
    writer = cache.begin_artifact()
    original = artifacts._connect
    writes = []

    @contextmanager
    def instrument(*args, **kwargs):
        if kwargs.get("write"):
            writes.append(True)
        with original(*args, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(artifacts, "_connect", instrument)
    writer.append(b"small\n")
    assert len(writes) == 1
    writer.flush()
    assert len(writes) == 2
    writer.finish()


def test_fragment_iteration_does_not_open_an_unused_connection_after_last_chunk(cache, monkeypatch):
    writer = cache.begin_artifact()
    writer.append(b"no final newline")
    writer.finish()
    original = artifacts._connect
    reads = []

    @contextmanager
    def instrument(*args, **kwargs):
        if not kwargs.get("write"):
            reads.append(True)
        with original(*args, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(artifacts, "_connect", instrument)
    assert (
        b"".join(chunk.data for chunk in cache.iter_artifact(writer.ref_id)) == b"no final newline"
    )
    assert len(reads) == 2  # One metadata read and one bounded chunk read.


def test_observation_pages_release_the_database_and_keep_their_sequence_ceiling(cache):
    writer = cache.begin_artifact(stream="stdout")
    for _ in range(80):
        writer.append(b"line\n")
    observations = cache.iter_execution_observations(writer.execution_id)
    assert next(observations).sequence == 0
    late = writer.append(b"late\n")
    assert late.sequence == 80
    assert [item.sequence for item in observations] == list(range(1, 80))
    writer.finish()
    assert cache.execution_recovery(writer.execution_id).observed_count == 81


def test_observation_metadata_cannot_exhaust_disk_without_using_quota(cache):
    budget = (
        artifacts.ARTIFACT_CHARGE + artifacts.EXECUTION_CHARGE + 2 * artifacts.OBSERVATION_CHARGE
    )
    writer = cache.begin_artifact(quota_bytes=budget)
    writer.append(b"")
    writer.append(b"")
    with pytest.raises(ArtifactError, match="quota"):
        writer.append(b"")
    assert cache.artifact_metadata(writer.ref_id).state is ArtifactState.INCOMPLETE
    assert len(list(cache.iter_execution_observations(writer.execution_id))) == 2
    assert_usage(cache)


def _pre_identity_artifact(cache):
    """Create the old schema without defaults on its nullable counter columns."""
    ref = "tc_0123456789abcdef"
    data = b"old merged log\n"
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute(
            "INSERT INTO output_cache(ref_id,source,content) VALUES (?,'execution-artifact','')",
            (ref,),
        )
        conn.execute("""CREATE TABLE execution_artifacts (
            ref_id TEXT PRIMARY KEY,owner TEXT,state TEXT,touched REAL,stored_bytes INTEGER,
            newlines INTEGER,ends_newline INTEGER,chunk_count INTEGER,content_hash TEXT,
            reason TEXT,policy TEXT)""")
        conn.execute(
            "INSERT INTO execution_artifacts VALUES (?,?,'complete',0,?,1,1,1,?,NULL,?)",
            (ref, uuid.uuid4().hex, len(data), hashlib.sha256(data).hexdigest(), artifacts.POLICY),
        )
        conn.execute("""CREATE TABLE execution_chunks (
            ref_id TEXT,sequence INTEGER,first_line INTEGER,last_line INTEGER,data BLOB,
            checksum TEXT,PRIMARY KEY(ref_id,sequence))""")
        conn.execute(
            "INSERT INTO execution_chunks VALUES (?,0,1,1,?,?)",
            (ref, data, artifacts._chunk_digest(0, 1, 1, data)),
        )
    return ref, data


def test_pre_identity_artifacts_remain_readable_without_migration_or_invented_sources(cache):
    ref, data = _pre_identity_artifact(cache)
    assert cache.read_artifact(ref).text == data.decode()
    assert cache.artifact_metadata(ref).execution_id is None
    writer = cache.begin_artifact(stream="stdout")
    assert writer.finish().complete
    assert cache.read_artifact(ref).text == data.decode()
    assert next(cache.iter_artifact(ref)).observation_first is None
    assert cache.artifact_metadata(ref).stream is ArtifactStream.MERGED
    with sqlite3.connect(cache.db_path) as conn:
        assert len(conn.execute("PRAGMA table_info(execution_chunks)").fetchall()) == 6
    assert_usage(cache)


def test_quota_initialization_on_nullable_legacy_schema_is_idempotent(cache, monkeypatch):
    ref, data = _pre_identity_artifact(cache)
    original_seed = artifact_quota._seed_sizes
    seeded = []

    def seed(conn):
        seeded.append(True)
        return original_seed(conn)

    monkeypatch.setattr(artifact_quota, "_seed_sizes", seed)
    original_metadata = cache.artifact_metadata(ref)
    original_chunks = list(cache.iter_artifact(ref))
    initial = len(data) + artifacts.CHUNK_CHARGE + artifacts.ARTIFACT_CHARGE
    for _ in range(3):
        artifacts.ensure_schema(cache)
        with artifacts._connect(cache) as conn:
            assert artifacts._usage(conn) == initial
        assert_usage(cache)
    assert seeded == [True]

    writer = cache.begin_artifact(stream="stdout")
    with artifacts._connect(cache) as conn:
        assert conn.execute(
            """SELECT stored_bytes,newlines,ends_newline,chunk_count
            FROM execution_artifacts WHERE ref_id=?""",
            (writer.ref_id,),
        ).fetchone() == (0, 0, 0, 0)
        assert artifacts._usage(conn) == (
            initial + artifacts.ARTIFACT_CHARGE + artifacts.EXECUTION_CHARGE
        )
    assert_usage(cache)

    writer.append(b"new output\n")
    writer.flush()
    assert_usage(cache)
    legacy = cache.store("legacy entry after migration")
    assert cache.store("legacy entry after migration") == legacy
    assert_usage(cache)
    # Mark the old artifact as inquired; only the new legacy entry is evicted.
    cache.retrieve(ref)
    assert cache.evict_lri(max_items_to_remove=1) == [legacy]
    assert_usage(cache)

    with artifacts._connect(cache) as conn:
        before = artifacts._usage(conn)
    with pytest.raises(RuntimeError, match="rollback"):
        with artifacts._connect(cache, write=True) as conn:
            conn.execute(
                "UPDATE execution_artifacts SET stored_bytes=stored_bytes+7 WHERE ref_id=?",
                (writer.ref_id,),
            )
            raise RuntimeError("rollback")
    with artifacts._connect(cache) as conn:
        assert artifacts._usage(conn) == before
    assert_usage(cache)

    assert writer.finish().complete
    assert cache.read_artifact(writer.ref_id).text == "new output\n"
    artifacts.ensure_schema(cache)
    assert seeded == [True]
    assert_usage(cache)
    assert cache.artifact_metadata(ref) == original_metadata
    assert list(cache.iter_artifact(ref)) == original_chunks
