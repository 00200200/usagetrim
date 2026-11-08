"""Incremental execution artifacts within CCR's existing SQLite database.

No runner integration. Only sanitized UTF-8 reaches SQLite (including journals).
Logical quotas include all CCR payloads and conservative per-row charges; they
are not a bound on SQLite file allocation, free pages, journals or native RSS.
"""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum

from usagetrim.core import artifact_quota
from usagetrim.core.artifact_quota import (
    ARTIFACT_CHARGE,
    CHUNK_CHARGE,
    EXECUTION_CHARGE,
    OBSERVATION_CHARGE,
)
from usagetrim.core.cache import get_cache_budget_bytes
from usagetrim.core.stream_redactor import POLICY, StreamingRedactor

CHUNK_BYTES = 4096
READ_LIMIT = 64 * 1024
SQLITE_VALUE_LIMIT = 1024 * 1024
MAX_ACTIVE = 4
LEASE_SECONDS = 300
_HASH = re.compile(r"[0-9a-f]{64}")
_EXECUTION_ID = re.compile(r"[0-9a-f]{32}")
_REF = re.compile(r"tc_[0-9a-f]{16}")


class ArtifactState(Enum):
    WRITING = "writing"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    MISSING = "missing"
    CORRUPT = "corrupt"


class ArtifactStream(Enum):
    MERGED = "merged"
    STDOUT = "stdout"
    STDERR = "stderr"


class ArtifactError(ValueError):
    """Safe error categories; no paths, SQL, input or original exceptions."""

    def __init__(self, code):
        if code not in {
            "storage_error",
            "quota",
            "active_limit",
            "closed",
            "busy",
            "corrupt",
            "missing",
            "identity_conflict",
        }:
            code = "storage_error"
        self.code = code
        super().__init__(f"Execution artifact: {code}")


@dataclass(frozen=True)
class ArtifactMetadata:
    ref_id: str = field(repr=False)
    state: ArtifactState
    available: bool = False
    complete: bool = False
    stored_bytes: int = 0
    line_count: int = 0
    chunk_count: int = 0
    content_hash: str | None = None
    reason: str | None = None
    redaction_policy: str = POLICY
    encoding: str = "utf-8-replace"
    replacement_occurred: bool | None = None
    execution_id: str | None = field(default=None, repr=False)
    stream: ArtifactStream = ArtifactStream.MERGED


@dataclass(frozen=True)
class ArtifactFragment:
    sequence: int
    first_line: int
    last_line: int
    data: bytes = field(repr=False)
    execution_id: str | None = field(default=None, repr=False)
    stream: ArtifactStream = ArtifactStream.MERGED
    observation_first: int | None = None
    observation_last: int | None = None


@dataclass(frozen=True)
class ArtifactObservation:
    """Recorder acceptance order, not child emission order or durable payload."""

    execution_id: str = field(repr=False)
    sequence: int
    ref_id: str = field(repr=False)
    stream: ArtifactStream
    input_bytes: int


@dataclass(frozen=True)
class ExecutionRecovery:
    execution_id: str = field(repr=False)
    recorded: bool = False
    streams: tuple[ArtifactMetadata, ...] = ()
    observed_count: int = 0
    retained_observations: int = 0


@dataclass(frozen=True)
class ArtifactRead:
    metadata: ArtifactMetadata
    text: str = field(default="", repr=False)
    response_truncated: bool = False


@contextmanager
def _connect(cache, *, write=False):
    # Connections belong to the calling thread and are always really closed.
    conn = sqlite3.connect(cache.db_path, timeout=1)
    try:
        # Also bound accidental/corrupt metadata values, not just Python reads
        # of payloads. Python 3.11+ exposes SQLite's native per-value/row limit.
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, SQLITE_VALUE_LIMIT)
        conn.execute("PRAGMA cache_size=-1024")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield conn
        if write:
            conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema(cache):
    with _connect(cache, write=True) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS execution_artifacts (
                ref_id TEXT PRIMARY KEY REFERENCES output_cache(ref_id) ON DELETE CASCADE,
                owner TEXT NOT NULL, state TEXT NOT NULL,
                touched REAL NOT NULL, stored_bytes INTEGER NOT NULL DEFAULT 0,
                newlines INTEGER NOT NULL DEFAULT 0, ends_newline INTEGER NOT NULL DEFAULT 0,
                chunk_count INTEGER NOT NULL DEFAULT 0, content_hash TEXT NOT NULL,
                reason TEXT, policy TEXT NOT NULL
            )"""
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(execution_artifacts)")}
        if "execution_id" not in columns:
            conn.execute("ALTER TABLE execution_artifacts ADD COLUMN execution_id TEXT")
        if "stream" not in columns:
            conn.execute(
                "ALTER TABLE execution_artifacts ADD COLUMN stream TEXT NOT NULL DEFAULT 'merged'"
            )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS execution_chunks (
                ref_id TEXT NOT NULL REFERENCES execution_artifacts(ref_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL, first_line INTEGER NOT NULL,
                last_line INTEGER NOT NULL, data BLOB NOT NULL, checksum TEXT NOT NULL,
                PRIMARY KEY(ref_id, sequence)
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS execution_lines ON execution_chunks(ref_id,last_line,sequence)"
        )
        # Keep the six-column payload table compatible with earlier M2 writers.
        # Origin metadata is additive and published with the chunk transaction.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS execution_chunk_origins (
                ref_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                observation_first INTEGER NOT NULL, observation_last INTEGER NOT NULL,
                PRIMARY KEY(ref_id,sequence),
                FOREIGN KEY(ref_id,sequence) REFERENCES execution_chunks(ref_id,sequence)
                    ON DELETE CASCADE
            )"""
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS execution_stream_identity ON execution_artifacts(execution_id,stream)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS execution_active ON execution_artifacts(state,touched,ref_id)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS execution_groups (
                execution_id TEXT PRIMARY KEY, next_observation INTEGER NOT NULL DEFAULT 0,
                retained_observations INTEGER NOT NULL DEFAULT 0,
                merged_ref TEXT, stdout_ref TEXT, stderr_ref TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS execution_observations (
                execution_id TEXT NOT NULL, sequence INTEGER NOT NULL, ref_id TEXT NOT NULL,
                stream TEXT NOT NULL, input_bytes INTEGER NOT NULL,
                PRIMARY KEY(execution_id,sequence)
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS execution_observation_ref ON execution_observations(ref_id,sequence)"
        )
        # Legacy cache connections do not enable foreign_keys. The trigger keeps
        # eviction/clear consistent for those connections too.
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS execution_artifact_delete
            AFTER DELETE ON output_cache BEGIN
                DELETE FROM execution_chunks WHERE ref_id=OLD.ref_id;
                DELETE FROM execution_artifacts WHERE ref_id=OLD.ref_id;
            END"""
        )
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS execution_artifact_no_overwrite
            BEFORE UPDATE OF content,content_hash ON output_cache
            WHEN EXISTS(SELECT 1 FROM execution_artifacts WHERE ref_id=OLD.ref_id)
            BEGIN SELECT RAISE(ABORT, 'Execution artifact reference is reserved'); END"""
        )
        artifact_quota.ensure_accounting(conn)


def is_artifact(conn, ref_id):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_artifacts'"
    ).fetchone()
    return bool(
        exists
        and conn.execute("SELECT 1 FROM execution_artifacts WHERE ref_id=?", (ref_id,)).fetchone()
    )


def _chunk_digest(
    sequence,
    first,
    last,
    data,
    *,
    execution_id=None,
    stream=ArtifactStream.MERGED,
    observation_first=None,
    observation_last=None,
):
    header = f"{sequence}:{first}:{last}:"
    if observation_first is not None or observation_last is not None:
        header = "execution-chunk-v2:" + header
        header += f"{execution_id}:{stream.value}:{observation_first}:{observation_last}:"
    digest = hashlib.sha256(header.encode("ascii"))
    digest.update(data)
    return digest.hexdigest()


def _corrupt_database_error(error):
    code = getattr(error, "sqlite_errorcode", 0) & 255
    return code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_TOOBIG}


def _usage(conn):
    return artifact_quota.usage(conn)


def _reserve(conn, additional, budget, protected_execution_id=None):
    while _usage(conn) + additional > budget:
        row = conn.execute(
            """SELECT ref_id FROM output_cache WHERE ref_id NOT IN (
                SELECT ref_id FROM execution_artifacts WHERE state='writing'
            ) AND (? IS NULL OR ref_id NOT IN (
                SELECT ref_id FROM execution_artifacts WHERE execution_id=?
            )) ORDER BY CASE WHEN hit_count>0 THEN 1 ELSE 0 END,
                hit_count,last_inquired_at,created_at,ref_id LIMIT 1""",
            (protected_execution_id, protected_execution_id),
        ).fetchone()
        if row is None:
            raise ArtifactError("quota")
        ref_id = row[0]
        for table in ("recovery_search", "recovery_indexed"):
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                conn.execute(f"DELETE FROM {table} WHERE ref=?", (ref_id,))
        conn.execute("DELETE FROM output_cache WHERE ref_id=?", (ref_id,))


def expire_writes(cache, *, stale_before=None):
    """Fence abandoned leases, never infer that an abandoned capture completed.

    A live but idle writer must heartbeat within five minutes. Reclamation may
    fence it; subsequent writes fail rather than reviving its old lease.
    """
    cutoff = time.time() - LEASE_SECONDS if stale_before is None else stale_before
    try:
        valid = type(cutoff) in {int, float} and math.isfinite(cutoff)
        if valid:
            cutoff = float(cutoff)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("stale_before must be a finite timestamp")
    try:
        ensure_schema(cache)
        with _connect(cache, write=True) as conn:
            cursor = conn.execute(
                """UPDATE execution_artifacts SET state='incomplete',reason='lease_expired'
                WHERE state='writing' AND touched < ?""",
                (cutoff,),
            )
            return cursor.rowcount
    except (sqlite3.Error, OSError):
        raise ArtifactError("storage_error") from None


def _execution_identity(execution_id):
    if execution_id is None:
        return uuid.uuid4().hex
    if not isinstance(execution_id, str) or not _EXECUTION_ID.fullmatch(execution_id):
        raise ValueError("execution_id must be a canonical UUID4 hex identifier")
    if uuid.UUID(hex=execution_id).version != 4:
        raise ValueError("execution_id must be a canonical UUID4 hex identifier")
    return execution_id


def begin_artifact(
    cache,
    *,
    quota_bytes=None,
    max_active=MAX_ACTIVE,
    execution_id=None,
    stream=ArtifactStream.MERGED,
):
    execution_id = _execution_identity(execution_id)
    try:
        stream = ArtifactStream(stream)
    except (TypeError, ValueError):
        raise ValueError("stream must be merged, stdout or stderr") from None
    budget = get_cache_budget_bytes() if quota_bytes is None else quota_bytes
    if type(budget) is not int or budget <= 0:
        raise ValueError("quota_bytes must be a positive integer")
    if type(max_active) is not int or not 1 <= max_active <= MAX_ACTIVE:
        raise ValueError("max_active must be from 1 to 4")
    # A per-writer quota may tighten the shared CCR budget, never enlarge it.
    budget = min(budget, max(0, get_cache_budget_bytes()))
    owner = uuid.uuid4().hex
    ref_id = f"tc_{uuid.uuid4().hex[:16]}"
    try:
        expire_writes(cache)
        with _connect(cache, write=True) as conn:
            active = conn.execute(
                "SELECT COUNT(*) FROM execution_artifacts WHERE state='writing'"
            ).fetchone()[0]
            if active >= max_active:
                raise ArtifactError("active_limit")
            group = conn.execute(
                "SELECT merged_ref,stdout_ref,stderr_ref FROM execution_groups WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            slot = {
                ArtifactStream.MERGED: 0,
                ArtifactStream.STDOUT: 1,
                ArtifactStream.STDERR: 2,
            }[stream]
            if group and (
                group[slot] is not None
                or (slot == 0 and any(group[1:]))
                or (slot != 0 and group[0] is not None)
            ):
                raise ArtifactError("identity_conflict")
            _reserve(
                conn,
                ARTIFACT_CHARGE + (EXECUTION_CHARGE if group is None else 0),
                budget,
                execution_id,
            )
            if group is None:
                conn.execute(
                    "INSERT INTO execution_groups(execution_id) VALUES (?)", (execution_id,)
                )
            conn.execute(
                """INSERT INTO output_cache
                (ref_id,content_hash,source,content,created_at,hit_count,last_inquired_at)
                VALUES (?,NULL,'execution-artifact','',?,0,0)""",
                (ref_id, time.time()),
            )
            # CREATE TABLE IF NOT EXISTS cannot add defaults to an older table.
            # Initialize counters explicitly, including on nullable legacy schemas.
            conn.execute(
                """INSERT INTO execution_artifacts
                (ref_id,owner,state,touched,stored_bytes,newlines,ends_newline,chunk_count,
                content_hash,policy,execution_id,stream)
                VALUES (?,?,'writing',?,0,0,0,0,?,?,?,?)""",
                (
                    ref_id,
                    owner,
                    time.time(),
                    hashlib.sha256(b"").hexdigest(),
                    POLICY,
                    execution_id,
                    stream.value,
                ),
            )
            column = {
                ArtifactStream.MERGED: "merged_ref",
                ArtifactStream.STDOUT: "stdout_ref",
                ArtifactStream.STDERR: "stderr_ref",
            }[stream]
            conn.execute(
                f"UPDATE execution_groups SET {column}=? WHERE execution_id=?",
                (ref_id, execution_id),
            )
    except (sqlite3.Error, OSError):
        raise ArtifactError("storage_error") from None
    return ArtifactWriter(cache, ref_id, owner, budget, execution_id, stream)


class ArtifactWriter:
    """One bounded writer; separate artifacts can be appended concurrently.

    Successful append acknowledges processing, not durability of a partial line.
    At most 16 KiB of an unconfirmed line and one 4 KiB output chunk are retained.
    finish/abort/context-manager exit releases buffers; no background thread exists.
    """

    def __init__(self, cache, ref_id, owner, budget, execution_id, stream):
        self.ref_id = ref_id
        self._execution_id, self._stream = execution_id, stream
        self._cache, self._owner, self._budget = cache, owner, budget
        self._redactor = StreamingRedactor()
        self._buffer = bytearray()
        self._digest = hashlib.sha256()
        self._closed = False
        self._lock = threading.Lock()
        self._pending_observation = None
        self._last_observation = None
        self._record_first = self._record_last = None
        self._buffer_first = self._buffer_last = None

    @property
    def execution_id(self):
        return self._execution_id

    @property
    def stream(self):
        return self._stream

    @contextmanager
    def _operation(self):
        if not self._lock.acquire(blocking=False):
            raise ArtifactError("busy")
        try:
            if self._closed:
                raise ArtifactError("closed")
            yield
        except (sqlite3.Error, OSError):
            self._stop("storage_error")
            raise ArtifactError("storage_error") from None
        except ArtifactError as exc:
            if not self._closed:
                self._stop(exc.code if exc.code == "quota" else "interrupted")
            raise
        except BaseException:
            self._stop("interrupted")
            raise
        finally:
            self._lock.release()

    def _owned(self, conn):
        row = conn.execute(
            """SELECT stored_bytes,newlines,ends_newline,chunk_count FROM execution_artifacts
            WHERE ref_id=? AND owner=? AND state='writing' AND execution_id=? AND stream=?""",
            (self.ref_id, self._owner, self.execution_id, self.stream.value),
        ).fetchone()
        if row is None:
            raise ArtifactError("closed")
        return row

    def _heartbeat(self):
        with _connect(self._cache, write=True) as conn:
            self._owned(conn)
            conn.execute(
                "UPDATE execution_artifacts SET touched=? WHERE ref_id=?",
                (time.time(), self.ref_id),
            )

    def heartbeat(self):
        with self._operation():
            self._heartbeat()

    def _observe(self, input_bytes):
        # The acceptance-order reservation also refreshes the lease: append no
        # longer needs a separate heartbeat connection/commit before this one.
        with _connect(self._cache, write=True) as conn:
            self._owned(conn)
            _reserve(conn, OBSERVATION_CHARGE, self._budget)
            row = conn.execute(
                "SELECT next_observation FROM execution_groups WHERE execution_id=?",
                (self.execution_id,),
            ).fetchone()
            if row is None or type(row[0]) is not int or not 0 <= row[0] < 2**63 - 1:
                raise ArtifactError("corrupt")
            sequence = row[0]
            conn.execute(
                "INSERT INTO execution_observations VALUES (?,?,?,?,?)",
                (self.execution_id, sequence, self.ref_id, self.stream.value, input_bytes),
            )
            conn.execute(
                "UPDATE execution_groups SET next_observation=? WHERE execution_id=?",
                (sequence + 1, self.execution_id),
            )
            conn.execute(
                "UPDATE execution_artifacts SET touched=? WHERE ref_id=?",
                (time.time(), self.ref_id),
            )
        return ArtifactObservation(
            self.execution_id, sequence, self.ref_id, self.stream, input_bytes
        )

    def _flush(self):
        if not self._buffer:
            return False
        data = bytes(self._buffer)
        # Every committed prefix, including an interrupted write, must remain
        # valid UTF-8. Carry at most three trailing bytes into the next chunk.
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data" or exc.end != len(data):
                raise ArtifactError("corrupt") from None
            data = data[: exc.start]
        if not data:
            return False
        digest = self._digest.copy()
        digest.update(data)
        with _connect(self._cache, write=True) as conn:
            stored, newlines, _, sequence = self._owned(conn)
            _reserve(conn, len(data) + CHUNK_CHARGE, self._budget)
            first = newlines + 1
            last = first + data.count(b"\n") - int(data.endswith(b"\n"))
            conn.execute(
                """INSERT INTO execution_chunks
                (ref_id,sequence,first_line,last_line,data,checksum) VALUES (?,?,?,?,?,?)""",
                (
                    self.ref_id,
                    sequence,
                    first,
                    last,
                    data,
                    _chunk_digest(
                        sequence,
                        first,
                        last,
                        data,
                        execution_id=self.execution_id,
                        stream=self.stream,
                        observation_first=self._buffer_first,
                        observation_last=self._buffer_last,
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO execution_chunk_origins VALUES (?,?,?,?)",
                (self.ref_id, sequence, self._buffer_first, self._buffer_last),
            )
            conn.execute(
                """UPDATE execution_artifacts SET stored_bytes=?,newlines=?,ends_newline=?,
                chunk_count=?,content_hash=?,touched=? WHERE ref_id=?""",
                (
                    stored + len(data),
                    newlines + data.count(b"\n"),
                    int(data.endswith(b"\n")),
                    sequence + 1,
                    digest.hexdigest(),
                    time.time(),
                    self.ref_id,
                ),
            )
        self._digest = digest
        del self._buffer[: len(data)]
        self._buffer_first = self._record_first if self._buffer else None
        self._buffer_last = self._record_last if self._buffer else None
        return True

    def _accept(self, records):
        for text in records:
            data = text.encode("utf-8")
            for start in range(0, len(data), CHUNK_BYTES):
                part = memoryview(data)[start : start + CHUNK_BYTES]
                while part:
                    if self._record_first is None or self._record_last is None:
                        raise ArtifactError("corrupt")
                    self._buffer_first = (
                        self._record_first
                        if self._buffer_first is None
                        else min(self._buffer_first, self._record_first)
                    )
                    self._buffer_last = self._record_last
                    take = min(CHUNK_BYTES - len(self._buffer), len(part))
                    self._buffer.extend(part[:take])
                    part = part[take:]
                    if len(self._buffer) == CHUNK_BYTES:
                        self._flush()

    def append(self, data: bytes):
        if type(data) is not bytes:
            raise TypeError("Execution artifact input must be bytes")
        with self._operation():
            observation = self._observe(len(data))
            self._record_first = (
                observation.sequence
                if self._pending_observation is None
                else self._pending_observation
            )
            self._last_observation = self._record_last = observation.sequence
            self._accept(self._redactor.feed(data))
            self._pending_observation = (
                self._record_first if self._redactor.has_pending_input else None
            )
        return observation

    def flush(self):
        """Commit safe records only; never flush a quarantined partial line."""
        with self._operation():
            if not self._flush():
                self._heartbeat()

    def finish(self):
        with self._operation():
            self._record_first = (
                self._last_observation
                if self._pending_observation is None
                else self._pending_observation
            )
            self._record_last = self._last_observation
            self._accept(self._redactor.finish())
            self._flush()
            incomplete = self._redactor.incomplete
            with _connect(self._cache, write=True) as conn:
                self._owned(conn)
                conn.execute(
                    "UPDATE execution_artifacts SET state=?,reason=?,touched=? WHERE ref_id=?",
                    (
                        "incomplete" if incomplete else "complete",
                        "redaction_unavailable" if incomplete else None,
                        time.time(),
                        self.ref_id,
                    ),
                )
            self._closed = True
            self._redactor.discard()
        return artifact_metadata(self._cache, self.ref_id)

    def _stop(self, reason):
        self._closed = True
        self._buffer.clear()
        self._buffer_first = self._buffer_last = None
        self._redactor.discard()
        try:
            with _connect(self._cache, write=True) as conn:
                conn.execute(
                    """UPDATE execution_artifacts SET state='incomplete',reason=?,touched=?
                    WHERE ref_id=? AND owner=? AND state='writing'""",
                    (reason, time.time(), self.ref_id, self._owner),
                )
        except (sqlite3.Error, OSError):
            # A full/unwritable disk may prevent even this metadata update.
            # The committed state remains writing, never complete; lease expiry
            # can reclaim it later. No raw fallback/spill is permitted.
            pass

    def abort(self):
        if self._closed:
            return artifact_metadata(self._cache, self.ref_id)
        with self._operation():
            # Preserve already-sanitized buffered records, but discard the raw
            # pending line and decoder state without pretending they completed.
            self._flush()
            self._stop("interrupted")
        return artifact_metadata(self._cache, self.ref_id)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if not self._closed:
            try:
                self.abort()
            except ArtifactError:
                if exc_type is None:
                    raise


def artifact_metadata(cache, ref_id):
    try:
        with _connect(cache) as conn:
            if not is_artifact(conn, ref_id):
                anchor = conn.execute(
                    """SELECT 1 FROM output_cache WHERE ref_id=?
                    AND source='execution-artifact' AND content_hash IS NULL""",
                    (ref_id,),
                ).fetchone()
                return ArtifactMetadata(
                    ref_id, ArtifactState.CORRUPT if anchor else ArtifactState.MISSING
                )
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_chunks'"
            ).fetchone():
                return ArtifactMetadata(ref_id, ArtifactState.CORRUPT)
            columns = {item[1] for item in conn.execute("PRAGMA table_info(execution_artifacts)")}
            identity_columns = (
                "execution_id,stream" if {"execution_id", "stream"} <= columns else "NULL,'merged'"
            )
            row = conn.execute(
                f"""SELECT state,stored_bytes,newlines,ends_newline,chunk_count,
                content_hash,reason,policy,{identity_columns} FROM execution_artifacts WHERE ref_id=?""",
                (ref_id,),
            ).fetchone()
            group = None
            if (
                row
                and row[8] is not None
                and conn.execute(
                    """SELECT COUNT(*) FROM sqlite_master WHERE type='table'
                AND name IN ('execution_groups','execution_chunk_origins')"""
                ).fetchone()[0]
                == 2
            ):
                group = conn.execute(
                    "SELECT merged_ref,stdout_ref,stderr_ref FROM execution_groups WHERE execution_id=?",
                    (row[8],),
                ).fetchone()
            present = conn.execute(
                "SELECT 1 FROM execution_chunks WHERE ref_id=? LIMIT 1", (ref_id,)
            ).fetchone()
    except sqlite3.Error as exc:
        if _corrupt_database_error(exc):
            return ArtifactMetadata(ref_id, ArtifactState.CORRUPT)
        raise ArtifactError("storage_error") from None
    except OSError:
        raise ArtifactError("storage_error") from None
    if row is None:
        return ArtifactMetadata(ref_id, ArtifactState.MISSING)
    try:
        state, size, newlines, ends, chunks, digest, reason, policy, execution_id, stream = row
        state = ArtifactState(state)
        stream = ArtifactStream(stream)
        if execution_id is None:
            if stream is not ArtifactStream.MERGED:
                raise ValueError
        else:
            _execution_identity(execution_id)
            slot = {ArtifactStream.MERGED: 0, ArtifactStream.STDOUT: 1, ArtifactStream.STDERR: 2}[
                stream
            ]
            if group is None or group[slot] != ref_id:
                raise ValueError
        if state not in {ArtifactState.WRITING, ArtifactState.COMPLETE, ArtifactState.INCOMPLETE}:
            raise ValueError
        if any(type(x) is not int or x < 0 for x in (size, newlines, chunks)):
            raise ValueError
        if (
            type(ends) is not int
            or ends not in (0, 1)
            or newlines > size
            or chunks > size
            or size > chunks * CHUNK_BYTES
        ):
            raise ValueError
        if ends and not newlines:
            raise ValueError
        if not isinstance(digest, str) or not _HASH.fullmatch(digest) or policy != POLICY:
            raise ValueError
        if size == 0 and (ends or newlines or chunks or digest != hashlib.sha256(b"").hexdigest()):
            raise ValueError
        if bool(size) != bool(present):
            raise ValueError
        if reason not in {
            None,
            "lease_expired",
            "quota",
            "storage_error",
            "interrupted",
            "redaction_unavailable",
        }:
            raise ValueError
        if state in {ArtifactState.WRITING, ArtifactState.COMPLETE} and reason is not None:
            raise ValueError
        lines = newlines + int(size > 0 and not ends)
        return ArtifactMetadata(
            ref_id,
            state,
            size > 0 or state is ArtifactState.COMPLETE,
            state is ArtifactState.COMPLETE,
            size,
            lines,
            chunks,
            digest,
            reason,
            execution_id=execution_id,
            stream=stream,
        )
    except (TypeError, ValueError):
        return ArtifactMetadata(ref_id, ArtifactState.CORRUPT)


def _require_execution_id(execution_id):
    if not isinstance(execution_id, str):
        raise ValueError("execution_id must be a canonical UUID4 hex identifier")
    return _execution_identity(execution_id)


def execution_recovery(cache, execution_id):
    """Return registered refs, including missing evicted streams; no child status."""
    execution_id = _require_execution_id(execution_id)
    try:
        with _connect(cache) as conn:
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4096)
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='execution_groups' AND type='table'"
            ).fetchone():
                return ExecutionRecovery(execution_id)
            row = conn.execute(
                """SELECT merged_ref,stdout_ref,stderr_ref,next_observation,retained_observations
                FROM execution_groups WHERE execution_id=?""",
                (execution_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise ArtifactError(
            "corrupt" if _corrupt_database_error(exc) else "storage_error"
        ) from None
    except OSError:
        raise ArtifactError("storage_error") from None
    if row is None:
        return ExecutionRecovery(execution_id)
    merged, stdout, stderr, observed, retained = row
    if (
        type(observed) is not int
        or type(retained) is not int
        or not 0 <= retained <= observed
        or (merged is not None and (stdout is not None or stderr is not None))
    ):
        raise ArtifactError("corrupt")
    streams = []
    for stream, ref_id in (
        (ArtifactStream.MERGED, merged),
        (ArtifactStream.STDOUT, stdout),
        (ArtifactStream.STDERR, stderr),
    ):
        if ref_id is None:
            continue
        if not isinstance(ref_id, str) or not _REF.fullmatch(ref_id):
            raise ArtifactError("corrupt")
        metadata = artifact_metadata(cache, ref_id)
        if metadata.state not in {ArtifactState.MISSING, ArtifactState.CORRUPT} and (
            metadata.execution_id != execution_id or metadata.stream is not stream
        ):
            metadata = replace(
                metadata, state=ArtifactState.CORRUPT, available=False, complete=False
            )
        streams.append(replace(metadata, execution_id=execution_id, stream=stream))
    if not streams:
        raise ArtifactError("corrupt")
    return ExecutionRecovery(execution_id, True, tuple(streams), observed, retained)


def iter_execution_observations(cache, execution_id):
    """Bounded metadata pages in recorder acceptance order, with no raw bytes.

    Eviction can leave gaps. ExecutionRecovery reports allocated/retained counts;
    neither this iterator nor those counts prove capture/delivery completeness.
    """
    execution_id = _require_execution_id(execution_id)
    cursor, end = 0, None
    try:
        while True:
            if end is not None and cursor >= end:
                return
            with _connect(cache) as conn:
                conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4096)
                if end is None:
                    if not conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='execution_groups' AND type='table'"
                    ).fetchone():
                        return
                    row = conn.execute(
                        "SELECT next_observation,merged_ref,stdout_ref,stderr_ref FROM execution_groups WHERE execution_id=?",
                        (execution_id,),
                    ).fetchone()
                    if row is None:
                        return
                    end = row[0]
                    registry = dict(
                        zip(
                            (ArtifactStream.MERGED, ArtifactStream.STDOUT, ArtifactStream.STDERR),
                            row[1:],
                        )
                    )
                    if type(end) is not int or end < 0:
                        raise ArtifactError("corrupt")
                if cursor >= end:
                    return
                rows = conn.execute(
                    """SELECT sequence,ref_id,stream,input_bytes FROM execution_observations
                    WHERE execution_id=? AND sequence>=? AND sequence<? ORDER BY sequence LIMIT 64""",
                    (execution_id, cursor, end),
                ).fetchall()
            if not rows:
                return
            for sequence, ref_id, stream, input_bytes in rows:
                try:
                    stream = ArtifactStream(stream)
                except (TypeError, ValueError):
                    raise ArtifactError("corrupt") from None
                if (
                    type(sequence) is not int
                    or not cursor <= sequence < end
                    or type(input_bytes) is not int
                    or input_bytes < 0
                    or not isinstance(ref_id, str)
                    or not _REF.fullmatch(ref_id)
                    or registry[stream] != ref_id
                ):
                    raise ArtifactError("corrupt")
                cursor = sequence + 1
                yield ArtifactObservation(execution_id, sequence, ref_id, stream, input_bytes)
    except sqlite3.Error as exc:
        raise ArtifactError(
            "corrupt" if _corrupt_database_error(exc) else "storage_error"
        ) from None
    except OSError:
        raise ArtifactError("storage_error") from None


def _fragments(cache, metadata, first_line, last_line):
    sequence = None
    while True:
        if sequence is not None and sequence >= metadata.chunk_count:
            return
        with _connect(cache) as conn:
            if sequence is None:
                row = conn.execute(
                    """SELECT sequence FROM execution_chunks WHERE ref_id=?
                    AND last_line>=? AND first_line<=? AND sequence<?
                    ORDER BY last_line,sequence LIMIT 1""",
                    (metadata.ref_id, first_line, last_line, metadata.chunk_count),
                ).fetchone()
                if row is None:
                    if first_line <= metadata.line_count:
                        raise ArtifactError("corrupt")
                    return
                sequence = row[0]
                if type(sequence) is not int or not 0 <= sequence < metadata.chunk_count:
                    raise ArtifactError("corrupt")
            if sequence >= metadata.chunk_count:
                return
            observation_columns = (
                "origin.observation_first,origin.observation_last"
                if metadata.execution_id is not None
                else "NULL,NULL"
            )
            table = "execution_chunks AS chunk"
            if metadata.execution_id is not None:
                table += (
                    " LEFT JOIN execution_chunk_origins AS origin ON origin.ref_id=chunk.ref_id"
                    " AND origin.sequence=chunk.sequence"
                )
            row = conn.execute(
                f"""SELECT chunk.first_line,chunk.last_line,chunk.rowid,
                CASE WHEN typeof(chunk.data)='blob' THEN length(chunk.data) ELSE 0 END,
                chunk.checksum,{observation_columns}
                FROM {table} WHERE chunk.ref_id=? AND chunk.sequence=?""",
                (metadata.ref_id, sequence),
            ).fetchone()
            if row is None:
                raise ArtifactError("corrupt")
            first, last, rowid, size, checksum, observed_first, observed_last = row
            if type(size) is not int or not 0 < size <= CHUNK_BYTES:
                raise ArtifactError("corrupt")
            # Do not select an arbitrarily large corrupt BLOB into memory. The
            # read transaction pins metadata and blob while this bounded read runs.
            with conn.blobopen("execution_chunks", "data", rowid, readonly=True) as blob:
                if len(blob) != size:
                    raise ArtifactError("corrupt")
                data = blob.read(CHUNK_BYTES + 1)
        if metadata.execution_id is not None and (
            type(observed_first) is not int
            or type(observed_last) is not int
            or not 0 <= observed_first <= observed_last
        ):
            raise ArtifactError("corrupt")
        if (
            type(data) is not bytes
            or not 0 < len(data) <= CHUNK_BYTES
            or len(data) != size
            or type(first) is not int
            or type(last) is not int
            or not 1 <= first <= last
            or last != first + data.count(b"\n") - int(data.endswith(b"\n"))
            or _chunk_digest(
                sequence,
                first,
                last,
                data,
                execution_id=metadata.execution_id,
                stream=metadata.stream,
                observation_first=observed_first,
                observation_last=observed_last,
            )
            != checksum
        ):
            raise ArtifactError("corrupt")
        if first > last_line:
            return
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise ArtifactError("corrupt") from None
        yield ArtifactFragment(
            sequence,
            first,
            last,
            data,
            metadata.execution_id,
            metadata.stream,
            observed_first,
            observed_last,
        )
        sequence += 1
        if last > last_line or (last == last_line and data.endswith(b"\n")):
            return


def iter_artifact(cache, ref_id):
    """Yield at most 4 KiB per fragment, snapshotting the committed chunk count.

    Each byte fragment is independently valid UTF-8. No database cursor or
    transaction survives across a yield.
    A corrupt/evicted record raises; consumers must not claim a full recovery
    until iteration and the final aggregate checksum verification both finish.
    """
    metadata = artifact_metadata(cache, ref_id)
    if metadata.state in {ArtifactState.MISSING, ArtifactState.CORRUPT}:
        raise ArtifactError(metadata.state.value)
    digest, size, count = hashlib.sha256(), 0, 0
    try:
        for fragment in _fragments(cache, metadata, 1, max(1, metadata.line_count)):
            digest.update(fragment.data)
            size += len(fragment.data)
            count += 1
            yield fragment
        if (
            size != metadata.stored_bytes
            or count != metadata.chunk_count
            or digest.hexdigest() != metadata.content_hash
        ):
            raise ArtifactError("corrupt")
    except sqlite3.Error as exc:
        raise ArtifactError(
            "corrupt" if _corrupt_database_error(exc) else "storage_error"
        ) from None
    except OSError:
        raise ArtifactError("storage_error") from None


def read_artifact(cache, ref_id, *, first_line=1, last_line=None, max_bytes=READ_LIMIT):
    """Read a bounded line slice; response truncation is independent of storage state."""
    if type(first_line) is not int or first_line < 1:
        raise ValueError("first_line must be positive")
    if last_line is not None and (type(last_line) is not int or last_line < first_line):
        raise ValueError("last_line must be at least first_line")
    if type(max_bytes) is not int or not 1 <= max_bytes <= READ_LIMIT:
        raise ValueError("max_bytes must be from 1 to 65536")
    metadata = artifact_metadata(cache, ref_id)
    if metadata.state in {ArtifactState.MISSING, ArtifactState.CORRUPT}:
        return ArtifactRead(metadata)
    last = metadata.line_count if last_line is None else min(last_line, metadata.line_count)
    if first_line > last:
        return ArtifactRead(metadata)
    output = bytearray()
    truncated = False
    try:
        for fragment in _fragments(cache, metadata, first_line, last):
            line = fragment.first_line
            for piece in fragment.data.splitlines(keepends=True):
                if first_line <= line <= last:
                    take = min(len(piece), max_bytes - len(output))
                    output.extend(piece[:take])
                    if take < len(piece):
                        truncated = True
                        break
                line += piece.count(b"\n")
            if truncated:
                break
        raw = bytes(output)
        # A response cap may cut a code point, never return replacement for it.
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            if not truncated or exc.end != len(raw) or exc.reason != "unexpected end of data":
                raise ArtifactError("corrupt") from None
            text = raw[: exc.start].decode("utf-8")
        if first_line == 1 and last == metadata.line_count and not truncated:
            if (
                len(raw) != metadata.stored_bytes
                or hashlib.sha256(raw).hexdigest() != metadata.content_hash
            ):
                raise ArtifactError("corrupt")
        return ArtifactRead(metadata, text, truncated)
    except ArtifactError as exc:
        if exc.code != "corrupt":
            raise
        return ArtifactRead(
            replace(metadata, state=ArtifactState.CORRUPT, available=False, complete=False)
        )
    except sqlite3.Error as exc:
        if _corrupt_database_error(exc):
            return ArtifactRead(
                replace(metadata, state=ArtifactState.CORRUPT, available=False, complete=False)
            )
        raise ArtifactError("storage_error") from None
    except OSError:
        raise ArtifactError("storage_error") from None
