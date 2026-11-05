"""Internal bounded capture and owned-tree shutdown; no CLI/MCP integration."""

from __future__ import annotations

import math
import os
import queue
import selectors
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field

from usagetrim.core.cache import ContextCache
from usagetrim.core.execution_artifacts import ArtifactError, ArtifactState, ArtifactStream
from usagetrim.core.execution_contract import (
    CaptureView,
    ExecutionIssue,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    RecoveryInfo,
    StreamCapture,
    TerminationReason,
)
from usagetrim.core.execution_process import ProcessTree, pipe_available

_STREAMS = ("stdout", "stderr")
_HEARTBEAT_SECONDS = 30
_POLL_SECONDS = 0.05


def _requested_stop(cancel_event, deadline):
    if cancel_event is not None and cancel_event.is_set():
        return TerminationReason.CANCELLED
    if deadline is not None and time.monotonic() >= deadline:
        return TerminationReason.TIMED_OUT
    return None


@dataclass(frozen=True)
class ShutdownPolicy:
    """Finite shutdown phases; not extra command execution time allowances."""

    graceful_seconds: float = 2.0
    force_seconds: float = 2.0
    reader_join_seconds: float = 2.0

    def __post_init__(self):
        for value in (self.graceful_seconds, self.force_seconds, self.reader_join_seconds):
            if type(value) not in {float, int} or not math.isfinite(value) or not 0 < value <= 60:
                raise ValueError(
                    "shutdown durations must be finite, positive and at most 60 seconds"
                )


class ExecutionInterrupted(KeyboardInterrupt):
    """Ctrl-C after bounded cleanup; the partial result is available explicitly."""

    def __init__(self, result):
        super().__init__("Execution interrupted; inspect result.issues for cleanup failures")
        self.result = result


class ExecutionRunnerError(RuntimeError):
    """Unexpected runner failure, reported only after owned resources are closed."""

    def __init__(self, issues):
        super().__init__("Execution runner failed; inspect issues for cleanup failures")
        self.issues = issues


@dataclass(frozen=True)
class CaptureLimits:
    chunk_bytes: int = 8192
    queue_chunks: int = 8
    preview_bytes: int = 4096
    quota_bytes: int | None = None

    def __post_init__(self):
        for value, maximum, name in (
            (self.chunk_bytes, 65536, "chunk_bytes"),
            (self.queue_chunks, 64, "queue_chunks"),
            (self.preview_bytes, 65536, "preview_bytes"),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be a positive integer at most {maximum}")
        if self.quota_bytes is not None and (
            type(self.quota_bytes) is not int or self.quota_bytes <= 0
        ):
            raise ValueError("quota_bytes must be a positive integer or None")


@dataclass(frozen=True)
class _PipeEvent:
    stream: str
    data: bytes = field(default=b"", repr=False)
    terminal: bool = False
    failed: bool = False


class _PipeReader:
    """Own a pipe on one thread; the collector is the only storage writer."""

    def __init__(self, stream, events, ready, chunk_bytes, execution_id, stop=None):
        self.stream, self.events, self.ready = stream, events, ready
        self.chunk_bytes = chunk_bytes
        self.pipe = None
        self.stop = stop if stop is not None else threading.Event()
        self.finished = threading.Event()
        self.eof = self.failed = False
        self.thread = threading.Thread(
            target=self._run,
            name=f"usagetrim-capture-{stream}-{execution_id[:8]}",
            daemon=False,
        )

    def _emit(self, event):
        while not self.stop.is_set():
            try:
                self.events.put(event, timeout=_POLL_SECONDS)
                return
            except queue.Full:
                pass

    def _chunks(self):
        if os.name == "nt":
            while not self.stop.is_set():
                available = pipe_available(self.pipe)
                if available < 0:
                    self.eof = True
                    return
                if not available:
                    self.stop.wait(_POLL_SECONDS)
                    continue
                data = self.pipe.read(min(available, self.chunk_bytes))
                if not data:
                    self.eof = True
                    return
                yield data
                del data
        else:
            os.set_blocking(self.pipe.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(self.pipe, selectors.EVENT_READ)
                while not self.stop.is_set():
                    if not selector.select(_POLL_SECONDS):
                        continue
                    try:
                        data = self.pipe.read(self.chunk_bytes)
                    except BlockingIOError:
                        continue
                    if data is None:
                        continue
                    if not data:
                        self.eof = True
                        return
                    yield data
                    del data

    def _run(self):
        self.ready.wait()
        if self.pipe is None:
            # Startup failed before a child existed. No terminal queue item:
            # even a one-slot queue must allow both idle readers to be joined.
            self.finished.set()
            return
        try:
            for data in self._chunks():
                self._emit(_PipeEvent(self.stream, data))
                del data
        except BaseException:
            # Thread failures must be reported, never silently strand the pump.
            # No exception object or message crosses this boundary.
            self.failed = True
        finally:
            try:
                self.pipe.close()
            except BaseException:
                self.failed = True
            self._emit(_PipeEvent(self.stream, terminal=True, failed=self.failed))
            self.finished.set()


class _Issues:
    """Bounded diagnostics; cleanup preserves each distinct, fixed failure category."""

    def __init__(self):
        self.items = {}

    def add(self, phase, stream, category):
        # All arguments originate in the runner, not subprocess exception text.
        key = (phase, stream, category) if phase == "cleanup" else (phase, stream)
        self.items.setdefault(
            key,
            ExecutionIssue(
                phase,
                f"{stream}_{category}" if stream else category,
                f"{stream or 'Execution'}: {category.replace('_', ' ')}.",
            ),
        )


def _storage_category(error):
    if isinstance(error, ArtifactError):
        return error.code
    return "storage_error"


class _StreamSink:
    def __init__(self, stream, issues):
        self.stream, self.issues = stream, issues
        self.writer = None
        self.failed = False
        self.eof = False
        self.settled = False
        self.finished_complete = False

    def abort(self):
        if self.writer is not None and not self.settled:
            self.settled = True
            try:
                self.writer.abort()
            except Exception:
                # Only this storage boundary is contained. Never log arbitrary
                # adapter/SQLite exceptions, and never retry by spilling raw data.
                self.issues.add("storage", self.stream, "abort_unavailable")

    def fail(self, phase, category):
        self.failed = True
        self.issues.add(phase, self.stream, category)
        self.abort()

    def append(self, data):
        if self.failed or self.writer is None:
            return
        try:
            self.writer.append(data)
        except Exception as error:
            self.fail("storage", _storage_category(error))

    def heartbeat(self):
        if self.failed or self.writer is None:
            return
        try:
            self.writer.heartbeat()
        except Exception as error:
            self.fail("storage", _storage_category(error))

    def finish(self):
        if self.failed or not self.eof or self.writer is None:
            self.abort()
            return
        try:
            metadata = self.writer.finish()
            self.settled = True
            self.finished_complete = metadata.complete
            if not metadata.complete:
                self.failed = True
                self.issues.add("capture", self.stream, "redacted_view_incomplete")
        except Exception as error:
            self.fail("storage", _storage_category(error))


def _prepare_storage(cache, limits, execution_id, sinks):
    if cache is None:
        try:
            cache = ContextCache()
        except Exception:
            for sink in sinks.values():
                sink.fail("storage", "storage_unavailable")
            return None
    for stream, sink in sinks.items():
        try:
            sink.writer = cache.begin_artifact(
                execution_id=execution_id, stream=stream, quota_bytes=limits.quota_bytes
            )
        except Exception as error:
            sink.fail("storage", _storage_category(error))
    return cache


class _Control:
    def __init__(self, tree, deadline, cancel_event, shutdown, issues):
        self.tree, self.deadline, self.cancel_event = tree, deadline, cancel_event
        self.shutdown, self.issues = shutdown, issues
        self.reason = None
        self.interrupted = False
        self.stopping = self.forced = False
        self.force_at = self.end_at = None

    def begin(self, reason, *, immediate=False):
        if self.reason is None:
            self.reason = reason
        if self.stopping:
            return
        self.stopping = True
        now = time.monotonic()
        self.force_at = now if immediate else now + self.shutdown.graceful_seconds
        self.end_at = self.force_at + self.shutdown.force_seconds
        if not immediate:
            try:
                if self.tree.process is not None:
                    self.tree.gentle()
            except Exception:
                self.issues.add("cleanup", None, "graceful_termination_failed")
            except KeyboardInterrupt:
                self.interrupted = True
                self.force_at = time.monotonic()
                self.end_at = self.force_at + self.shutdown.force_seconds

    def tick(self):
        reason = _requested_stop(self.cancel_event, self.deadline)
        if reason is not None:
            self.begin(reason)
        if self.stopping and not self.forced and time.monotonic() >= self.force_at:
            try:
                self.tree.force()
            except Exception:
                self.forced = True
                self.issues.add("cleanup", None, "forced_termination_failed")
            except KeyboardInterrupt:
                self.interrupted = True
            else:
                self.forced = True

    def alive(self):
        try:
            return self.tree.alive()
        except Exception:
            self.issues.add("cleanup", None, "tree_state_unavailable")
            return True
        except KeyboardInterrupt:
            self.interrupted = True
            self.begin(TerminationReason.CANCELLED)
            return True


def _receive(event, sinks, done, *, discard=False):
    if event.terminal:
        done.add(event.stream)
        sinks[event.stream].eof = not event.failed
        if event.failed:
            sinks[event.stream].fail("capture", "pipe_read_failed")
    elif not discard:
        sinks[event.stream].append(event.data)


def _pump(events, sinks, done, control, *, discard=False):
    """Poll deadlines even under continuous output; keep draining during shutdown."""
    heartbeat_at = time.monotonic() + _HEARTBEAT_SECONDS
    while True:
        control.tick()
        exited = control.tree.poll() is not None
        drained = len(done) == len(_STREAMS)
        if exited and drained:
            if not control.stopping:
                # A successful scope has no more pipe data or direct child to
                # interrupt. Close remaining owned background descendants too.
                # On POSIX retain the waitable leader until this final signal.
                try:
                    control.tree.force()
                except Exception:
                    control.issues.add("cleanup", None, "scope_close_failed")
                if control.tree.job is None:
                    return
                # Job accounting can verify descendant exit without reaping
                # or enumerating unrelated processes. Allow a bounded drain.
                control.begin(None, immediate=True)
                control.forced = True
            if (control.forced and control.tree.job is None) or not control.alive():
                return
        if control.stopping and time.monotonic() >= control.end_at:
            return
        try:
            event = events.get(timeout=_POLL_SECONDS)
        except queue.Empty:
            pass
        else:
            try:
                _receive(event, sinks, done, discard=discard)
            finally:
                events.task_done()
                del event
        if not discard and time.monotonic() >= heartbeat_at:
            for sink in sinks.values():
                sink.heartbeat()
            heartbeat_at = time.monotonic() + _HEARTBEAT_SECONDS


def _join_readers(readers, stop, ready, events, sinks, done, shutdown, issues):
    """Stop interruptible readers and release queue backpressure before joining."""
    stop.set()
    ready.set()
    interrupted = False
    deadline = time.monotonic() + shutdown.reader_join_seconds
    for reader in readers:
        if reader.thread.ident is None:
            continue
        # Event waiting avoids CPython's interrupted Thread.join bookkeeping
        # being mistaken for proof that a reader actually finished its cleanup.
        while not reader.finished.is_set() and time.monotonic() < deadline:
            try:
                reader.finished.wait(min(_POLL_SECONDS, max(0, deadline - time.monotonic())))
            except KeyboardInterrupt:
                interrupted = True
            except Exception:
                issues.add("cleanup", reader.stream, "reader_join_failed")
                break
        try:
            reader.thread.join(timeout=max(0, deadline - time.monotonic()))
        except KeyboardInterrupt:
            interrupted = True
        except Exception:
            issues.add("cleanup", reader.stream, "reader_join_failed")
        if not reader.finished.is_set() or reader.thread.is_alive():
            issues.add("cleanup", reader.stream, "reader_join_failed")
    # At most queue_chunks plus the two in-flight reader items can remain.
    while True:
        try:
            event = events.get_nowait()
        except queue.Empty:
            break
        try:
            _receive(event, sinks, done)
        except KeyboardInterrupt:
            interrupted = True
            sinks[event.stream].fail("capture", "interrupted")
        except Exception:
            issues.add("capture", None, "queue_cleanup_failed")
            for sink in sinks.values():
                sink.failed = True
        finally:
            events.task_done()
    for reader in readers:
        if reader.finished.is_set() and reader.eof and not reader.failed:
            sinks[reader.stream].eof = True
        elif reader.pipe is not None:
            issues.add("cleanup", reader.stream, "pipe_eof_unverified")
            sinks[reader.stream].fail("capture", "pipe_incomplete")
    return interrupted


def _attach_pipes(process, readers, issues):
    """A partially constructed child must not strand ready-gated readers."""
    complete = True
    for reader in readers:
        if reader.pipe is not None:
            continue
        try:
            reader.pipe = getattr(process, reader.stream, None)
        except Exception:
            reader.pipe = None
        if reader.pipe is None:
            complete = False
            issues.add("capture", reader.stream, "pipe_unavailable")
    return complete


def _views(cache, execution_id, sinks, issues, limits):
    registered = {}
    if cache is not None:
        try:
            recovery = cache.execution_recovery(execution_id)
            registered = {item.stream.value: item for item in recovery.streams}
        except Exception:
            issues.add("storage", None, "recovery_metadata_unavailable")
    views = []
    for stream, sink in sinks.items():
        text, truncated, info, complete = "", None, None, False
        encoding = None
        if sink.writer is not None and cache is not None:
            ref_id = sink.writer.ref_id
            try:
                view = cache.read_artifact(ref_id, max_bytes=limits.preview_bytes)
                metadata = view.metadata
                known = registered.get(stream)
                valid = (
                    metadata.execution_id == execution_id
                    and metadata.stream is ArtifactStream(stream)
                    and known is not None
                    and known.ref_id == ref_id
                )
                available = metadata.available and valid
                recoverable = available and metadata.complete and not sink.failed and sink.eof
                if available:
                    text, truncated, encoding = (
                        view.text,
                        view.response_truncated,
                        metadata.encoding,
                    )
                info = RecoveryInfo(ref_id=ref_id, available=available, complete=recoverable)
                complete = recoverable and sink.finished_complete
                if not available:
                    issues.add("storage", stream, "recovery_unavailable")
                if metadata.state is ArtifactState.CORRUPT:
                    issues.add("storage", stream, "recovery_corrupt")
            except Exception:
                # Availability is unknown when storage cannot answer the lookup.
                info = RecoveryInfo(ref_id=ref_id)
                issues.add("storage", stream, "recovery_read_failed")
        views.append(
            StreamCapture(
                stream,
                CaptureView(
                    text=text,
                    text_scope="preview",
                    stream_layout="separate",
                    complete=complete,
                    encoding=encoding,
                    replacement_occurred=None,
                ),
                info,
                truncated,
            )
        )
    return tuple(views)


def run_execution(
    request: ExecutionRequest,
    *,
    cache: ContextCache | None = None,
    limits: CaptureLimits | None = None,
    cancel_event: threading.Event | None = None,
    shutdown: ShutdownPolicy | None = None,
) -> ExecutionResult:
    """Capture with a monotonic deadline, per-call cancellation and owned cleanup.

    No global signal handler is installed. Ctrl-C is re-raised as the compatible
    ExecutionInterrupted subtype after cleanup, carrying the partial result.
    SQLite calls are synchronous and cannot be preempted; see execution-runner.md.
    """
    if not isinstance(request, ExecutionRequest):
        raise TypeError("request must be an ExecutionRequest")
    if limits is None:
        limits = CaptureLimits()
    if not isinstance(limits, CaptureLimits):
        raise TypeError("limits must be CaptureLimits")
    shutdown = shutdown if shutdown is not None else ShutdownPolicy()
    if not isinstance(shutdown, ShutdownPolicy):
        raise TypeError("shutdown must be ShutdownPolicy")
    if cancel_event is not None and not isinstance(cancel_event, threading.Event):
        raise TypeError("cancel_event must be a threading.Event")
    started_at = time.monotonic()
    deadline = None if request.timeout_seconds is None else started_at + request.timeout_seconds
    execution_id = uuid.uuid4().hex
    issues = _Issues()
    initial_reason = _requested_stop(cancel_event, deadline)
    if initial_reason is not None:
        return ExecutionResult(
            request,
            initial_reason,
            None,
            capture=CaptureView(text_scope="preview", stream_layout="separate", complete=False),
            elapsed_seconds=time.monotonic() - started_at,
            execution_id=execution_id,
        )
    sinks = {stream: _StreamSink(stream, issues) for stream in _STREAMS}
    events = queue.Queue(maxsize=limits.queue_chunks)
    ready = threading.Event()
    stop = threading.Event()
    readers = [
        _PipeReader(stream, events, ready, limits.chunk_bytes, execution_id, stop)
        for stream in _STREAMS
    ]
    tree = control = None
    process, returncode, done = None, None, set()
    pumped = interrupted = internal_error = start_failed = False
    pre_start_reason = None
    try:
        cache = _prepare_storage(cache, limits, execution_id, sinks)
        pre_start_reason = _requested_stop(cancel_event, deadline)
        if pre_start_reason is None:
            try:
                for reader in readers:
                    reader.thread.start()
            except RuntimeError:
                issues.add("capture", None, "reader_start_failed")
                start_failed = True
        if pre_start_reason is None and not start_failed:
            try:
                tree = ProcessTree()
                control = _Control(tree, deadline, cancel_event, shutdown, issues)
                pre_start_reason = _requested_stop(cancel_event, deadline)
                if pre_start_reason is None:
                    process = tree.start(
                        request.argv
                        if request.mode is ExecutionMode.ARGV
                        else request.shell_command,
                        shell=request.mode is ExecutionMode.SHELL,
                        cwd=request.cwd,
                    )
            except (OSError, ValueError, TypeError):
                issues.add("start", None, "failed_to_start")
                start_failed = True
                process = tree.process if tree is not None else None
        if process is not None:
            if not _attach_pipes(process, readers, issues):
                internal_error = True
                control.begin(None, immediate=True)
            ready.set()
            if start_failed:
                control.begin(TerminationReason.FAILED_TO_START, immediate=True)
            _pump(events, sinks, done, control)
            pumped = True
    except KeyboardInterrupt:
        interrupted = True
    except Exception:
        # Do not expose arbitrary adapter errors, command lines or exception bodies.
        internal_error = True
        issues.add("processing", None, "runner_failed")
    finally:
        # Tree.start may have allocated the child before assignment/resume failed
        # or an interrupt arrived. Retain ownership of that suspended process.
        if tree is not None:
            try:
                process = tree.process
            except Exception:
                internal_error = True
                issues.add("cleanup", None, "process_handle_unavailable")
        if process is not None:
            if not _attach_pipes(process, readers, issues):
                internal_error = True
            if control is None:
                control = _Control(tree, deadline, cancel_event, shutdown, issues)
        ready.set()
        if process is not None:
            if not pumped:
                control.begin(
                    TerminationReason.CANCELLED if interrupted else None,
                    immediate=internal_error or start_failed,
                )
                while True:
                    try:
                        _pump(events, sinks, done, control, discard=internal_error)
                        break
                    except KeyboardInterrupt:
                        interrupted = True
                        control.begin(TerminationReason.CANCELLED)
                    except Exception:
                        internal_error = True
                        issues.add("processing", None, "runner_failed")
                        try:
                            tree.force()
                        except Exception:
                            issues.add("cleanup", None, "forced_termination_failed")
                        break
        try:
            interrupted |= _join_readers(
                readers, stop, ready, events, sinks, done, shutdown, issues
            )
        except Exception:
            internal_error = True
            issues.add("cleanup", None, "reader_cleanup_failed")
        if tree is not None:
            try:
                if (
                    getattr(tree, "job", None) is not None
                    and process is not None
                    and control.alive()
                ):
                    issues.add("cleanup", None, "tree_exit_unverified")
            except Exception:
                issues.add("cleanup", None, "tree_state_unavailable")
            close_deadline = time.monotonic() + shutdown.force_seconds
            while True:
                try:
                    tree.close()  # Windows kill-on-close is a final containment barrier.
                    break
                except KeyboardInterrupt:
                    interrupted = True
                    if time.monotonic() >= close_deadline:
                        issues.add("cleanup", None, "tree_close_failed")
                        break
                except Exception:
                    issues.add("cleanup", None, "tree_close_failed")
                    break
        if process is not None:
            # Never use an unbounded wait, even when direct-child reaping failed.
            reap_deadline = time.monotonic() + shutdown.force_seconds
            while True:
                try:
                    returncode = process.wait(timeout=max(0, reap_deadline - time.monotonic()))
                    break
                except KeyboardInterrupt:
                    interrupted = True
                    if time.monotonic() >= reap_deadline:
                        issues.add("cleanup", None, "child_not_reaped")
                        break
                except subprocess.TimeoutExpired:
                    issues.add("cleanup", None, "child_not_reaped")
                    break
                except Exception:
                    issues.add("cleanup", None, "child_status_unavailable")
                    break
            try:
                observed = getattr(tree, "observed_returncode", None)
                if getattr(tree, "status_unavailable", False):
                    returncode = observed
                    issues.add("cleanup", None, "child_status_unavailable")
                elif returncode is None:
                    returncode = observed
                if getattr(tree, "job", None) is None:
                    # Verification is read-only after reaping. No signal may
                    # be sent after releasing the leader's PID reservation.
                    if control.alive():
                        issues.add("cleanup", None, "tree_exit_unverified")
            except Exception:
                issues.add("cleanup", None, "child_status_unavailable")
    reason = pre_start_reason or (control.reason if control is not None else None)
    if interrupted and reason is None:
        reason = TerminationReason.CANCELLED
    if control is not None:
        interrupted |= control.interrupted
        if interrupted:
            reason = reason or TerminationReason.CANCELLED
    incomplete = reason is not None or start_failed or internal_error
    for sink in sinks.values():
        try:
            if incomplete:
                sink.failed = True
                sink.abort()
            else:
                sink.finish()
        except KeyboardInterrupt:
            interrupted = incomplete = True
            reason = reason or TerminationReason.CANCELLED
            sink.fail("capture", "interrupted")
    # Finalization is synchronous SQLite work. Observe a request that arrived
    # during it without claiming the execution completed within its deadline.
    if reason is None and not start_failed:
        reason = _requested_stop(cancel_event, deadline)
        if reason is not None:
            for sink in sinks.values():
                sink.failed = True
                sink.abort()
    elapsed = time.monotonic() - started_at
    streams = _views(cache, execution_id, sinks, issues, limits)
    if not interrupted and (
        internal_error or (process is not None and returncode is None and reason is None)
    ):
        raise ExecutionRunnerError(tuple(issues.items.values())) from None
    if reason is not None:
        pass
    elif start_failed or process is None:
        reason = TerminationReason.FAILED_TO_START
    elif os.name == "posix" and returncode < 0:
        reason = TerminationReason.SIGNALED
    else:
        reason = TerminationReason.EXITED
    result = ExecutionResult(
        request,
        reason,
        None if reason is TerminationReason.FAILED_TO_START else returncode,
        capture=CaptureView(
            text_scope="preview",
            stream_layout="separate",
            complete=all(item.capture.complete for item in streams),
        ),
        elapsed_seconds=elapsed,
        issues=tuple(issues.items.values()),
        execution_id=execution_id,
        streams=streams,
    )
    if interrupted:
        raise ExecutionInterrupted(result)
    return result
