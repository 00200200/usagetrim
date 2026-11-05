"""Opt-in aggregate instrumentation for one isolated execution benchmark worker.

No event history, SQL text, parameters, captured bytes, or exception bodies are
retained. Patches exist only in this disposable worker and are restored on exit.
Transaction boundaries, SQLite settings and redaction policy stay unchanged.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from contextlib import ExitStack
from functools import wraps


def _append_bytes(args, kwargs):
    data = args[1] if len(args) > 1 else kwargs.get("data")
    return len(data) if type(data) is bytes else 0


class ExecutionProfile:
    def __init__(self, cache=None):
        self._rows = {}
        self._lock = threading.Lock()
        self._restore = ExitStack()
        self._queue_peak = 0
        self._sqlite_settings = {}
        if cache is not None:
            from usagetrim.core.execution_artifacts import _connect

            # One read-only snapshot before instrumentation/timing. Settings
            # are queried on a connection configured by M2 itself.
            with _connect(cache) as connection:
                for pragma in (
                    "journal_mode",
                    "synchronous",
                    "page_size",
                    "cache_size",
                    "busy_timeout",
                    "foreign_keys",
                ):
                    self._sqlite_settings[pragma] = connection.execute(
                        f"PRAGMA {pragma}"
                    ).fetchone()[0]

    def record(self, name, seconds=0.0, *, bytes_count=0, failed=False):
        with self._lock:
            row = self._rows.get(name)
            if row is None:
                row = {"calls": 0, "seconds": 0.0, "bytes": 0, "errors": 0}
                self._rows[name] = row
            row["calls"] += 1
            row["seconds"] += seconds
            row["bytes"] += bytes_count
            row["errors"] += int(failed)

    def seconds(self, name):
        with self._lock:
            return self._rows.get(name, {}).get("seconds", 0.0)

    def _patch(self, owner, name, value):
        original = getattr(owner, name)
        self._restore.callback(setattr, owner, name, original)
        setattr(owner, name, value)

    def _timed(self, name, function, *, byte_count=None):
        @wraps(function)
        def timed(*args, **kwargs):
            start = time.perf_counter()
            failed = True
            try:
                result = function(*args, **kwargs)
                failed = False
                if name == "writer.flush" and result is True:
                    self.record("writer.flush_committed")
                return result
            finally:
                self.record(
                    name,
                    time.perf_counter() - start,
                    bytes_count=byte_count(args, kwargs) if byte_count else 0,
                    failed=failed,
                )

        return timed

    def _call(self, name, function, *args, **kwargs):
        start = time.perf_counter()
        failed = True
        try:
            result = function(*args, **kwargs)
            failed = False
            return result
        finally:
            self.record(name, time.perf_counter() - start, failed=failed)

    def __enter__(self):
        from usagetrim.core import artifact_quota, execution_artifacts, execution_runner

        profile = self
        connection_type = sqlite3.Connection
        connect = sqlite3.connect

        class Connection(connection_type):
            def execute(self, sql, /, *args, **kwargs):
                # Only a fixed statement class is retained; never SQL or binds.
                first = (
                    sql.lstrip().split(None, 1)[0].upper()
                    if isinstance(sql, str) and sql.strip()
                    else "OTHER"
                )
                kind = (
                    first.lower()
                    if first in {"PRAGMA", "BEGIN", "SELECT", "INSERT", "UPDATE", "DELETE"}
                    else "other"
                )
                return profile._call(
                    f"sqlite.execute.{kind}", super().execute, sql, *args, **kwargs
                )

            def commit(self):
                return profile._call("sqlite.commit", super().commit)

            def rollback(self):
                return profile._call("sqlite.rollback", super().rollback)

            def close(self):
                return profile._call("sqlite.close", super().close)

        def opened(*args, **kwargs):
            # M2 uses the default Connection; do not override an explicit factory.
            kwargs.setdefault("factory", Connection)
            return profile._call("sqlite.connect", connect, *args, **kwargs)

        writer_type = execution_artifacts.ArtifactWriter
        try:
            self._patch(sqlite3, "connect", opened)
            for method in ("append", "_observe", "_flush", "finish", "abort", "heartbeat"):
                original = getattr(writer_type, method)
                name = "writer." + method.lstrip("_")
                if method == "append":
                    counter = _append_bytes
                else:
                    counter = None
                self._patch(writer_type, method, self._timed(name, original, byte_count=counter))

            accept = writer_type._accept

            @wraps(accept)
            def accepted(writer, records):
                # _accept consumes the lazy redactor generator. Timing feed()
                # creation alone would miss actual redaction. No per-line timers.
                flush_before = profile.seconds("writer.flush")
                start = time.perf_counter()
                try:
                    return accept(writer, records)
                finally:
                    elapsed = time.perf_counter() - start
                    nested_flush = profile.seconds("writer.flush") - flush_before
                    profile.record("writer.accept_inclusive", elapsed)
                    profile.record(
                        "redaction_and_assembly_residual", max(0.0, elapsed - nested_flush)
                    )

            self._patch(writer_type, "_accept", accepted)
            self._patch(
                execution_artifacts,
                "_reserve",
                self._timed("quota.reserve", execution_artifacts._reserve),
            )
            self._patch(artifact_quota, "usage", self._timed("quota.usage", artifact_quota.usage))
            self._patch(
                execution_runner.ProcessTree,
                "start",
                self._timed("process.start", execution_runner.ProcessTree.start),
            )
            self._patch(
                execution_runner.ProcessTree,
                "poll",
                self._timed("process.poll", execution_runner.ProcessTree.poll),
            )
            self._patch(
                execution_runner.ProcessTree,
                "close",
                self._timed("process.close", execution_runner.ProcessTree.close),
            )
            self._patch(
                execution_runner,
                "_join_readers",
                self._timed("runner.reader_shutdown", execution_runner._join_readers),
            )
            self._patch(
                execution_artifacts,
                "read_artifact",
                self._timed("recovery.preview", execution_artifacts.read_artifact),
            )
            chunks = execution_runner._PipeReader._chunks

            @wraps(chunks)
            def read_chunks(reader):
                iterator = chunks(reader)
                try:
                    while True:
                        start = time.perf_counter()
                        try:
                            data = next(iterator)
                        except StopIteration:
                            profile.record(
                                f"pipe.poll_read.{reader.stream}", time.perf_counter() - start
                            )
                            return
                        except BaseException:
                            profile.record(
                                f"pipe.poll_read.{reader.stream}",
                                time.perf_counter() - start,
                                failed=True,
                            )
                            raise
                        profile.record(
                            f"pipe.poll_read.{reader.stream}",
                            time.perf_counter() - start,
                            bytes_count=len(data),
                        )
                        yield data
                        del data
                finally:
                    iterator.close()

            self._patch(execution_runner._PipeReader, "_chunks", read_chunks)
            queue_type = execution_runner.queue.Queue

            class Queue(queue_type):
                def put(self, *args, **kwargs):
                    start = time.perf_counter()
                    try:
                        return super().put(*args, **kwargs)
                    except queue.Full:
                        profile.record("queue.full_timeouts")
                        raise
                    finally:
                        profile.record("queue.put", time.perf_counter() - start)

                def get(self, *args, **kwargs):
                    start = time.perf_counter()
                    try:
                        return super().get(*args, **kwargs)
                    except queue.Empty:
                        profile.record("queue.empty_timeouts")
                        raise
                    finally:
                        profile.record("queue.get", time.perf_counter() - start)

                def _put(self, item):
                    super()._put(item)
                    # Called with the queue mutex held. There is no reverse
                    # profile-lock -> queue-lock path.
                    with profile._lock:
                        profile._queue_peak = max(profile._queue_peak, self._qsize())

            self._queue_peak = 0
            self._patch(execution_runner.queue, "Queue", Queue)
            return self
        except BaseException:
            self._restore.close()
            raise

    def __exit__(self, *exception):
        self._restore.close()

    def report(self):
        with self._lock:
            stages = {name: dict(row) for name, row in sorted(self._rows.items())}
            queue_peak = self._queue_peak
        return {
            "scope": "one run_execution call, excluding warm-up and extra retrieval",
            "aggregation": "inclusive wall times; nested stages and reader threads overlap",
            "redaction_residual": "_accept minus nested _flush; includes redaction, UTF-8 encoding and buffer assembly",
            "sqlite_scope": "connect/execute/commit/rollback/close; cursor fetch and trigger SQL are not separately timed",
            "queue_high_water_events": queue_peak,
            "sqlite_settings": dict(self._sqlite_settings),
            "stages": stages,
        }
