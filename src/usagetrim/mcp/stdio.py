"""Small bounded-execution dispatcher for the legacy newline JSON-RPC transport.

Legacy handlers stay on the reader thread. Only opt-in bounded exec uses the
fixed worker pool. No futures/executor queue, global cancellation or stdout
redirection is used.
"""

from __future__ import annotations

import codecs
import io
import json
import math
import os
import queue
import selectors
import stat
import threading
import time
from dataclasses import dataclass, field

MAX_ACTIVE_EXECUTIONS = 2
SHUTDOWN_SECONDS = 20
_INPUT_POLL_SECONDS = 0.1
_INPUT_CHUNK_BYTES = 8192


def _input_lines(source, closed):
    """Read fresh stdio input without waiting indefinitely for another line.

    No other reader may consume this descriptor. In-memory/embedding iterators
    retain their original semantics; native Windows console input also retains
    its legacy iterator (unlike MCP pipes, it has no PeekNamedPipe readiness).
    """
    selector = None
    mode = None
    try:
        descriptor = source.fileno()
        mode = os.fstat(descriptor).st_mode
        regular = stat.S_ISREG(mode)
        if os.name == "nt":
            if not regular and not stat.S_ISFIFO(mode):
                raise io.UnsupportedOperation("not a Windows file or pipe")
        elif not regular:
            selector = selectors.DefaultSelector()
            selector.register(descriptor, selectors.EVENT_READ)
    except (AttributeError, OSError, ValueError):
        if selector is not None:
            selector.close()
        if mode is not None and stat.S_ISFIFO(mode):
            # If native pipe readiness cannot be established, fail and clean up
            # rather than silently restore the uninterruptible read we avoid.
            raise
        iterator = iter(source)
        while not closed.is_set():
            try:
                yield next(iterator)
            except StopIteration:
                return
        return

    # Match TextIOWrapper's encoding/error policy and universal newlines. Do
    # not call readline() after readiness: a partial JSON line would block again.
    parts = []  # One input line; its size policy remains the legacy transport's.
    try:
        decoder = io.IncrementalNewlineDecoder(
            codecs.getincrementaldecoder(getattr(source, "encoding", None) or "utf-8")(
                getattr(source, "errors", None) or "strict"
            ),
            translate=True,
        )
        while not closed.is_set():
            size = _INPUT_CHUNK_BYTES
            if not regular:
                if os.name == "nt":
                    from usagetrim.core.execution_process import pipe_available

                    available = pipe_available(source)
                    if available == 0:
                        closed.wait(_INPUT_POLL_SECONDS)
                        continue
                    size = min(size, available) if available > 0 else 0
                elif not selector.select(_INPUT_POLL_SECONDS):
                    continue
            if closed.is_set():
                return
            data = os.read(descriptor, size) if size else b""
            segments = decoder.decode(data, final=not data).split("\n")
            for segment in segments[:-1]:
                if closed.is_set():
                    return
                parts.append(segment)
                yield "".join(parts) + "\n"
                parts.clear()
            parts.append(segments[-1])
            if not data:
                tail = "".join(parts)
                if tail and not closed.is_set():
                    yield tail
                return
    finally:
        if selector is not None:
            selector.close()


def _key(value):
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return "number", value
    if type(value) in {str, type(None)}:
        return type(value), value
    return None


def _error(identifier, code, message):
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def _tool_failure(identifier):
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "result": {
            "content": [
                {"type": "text", "text": "[bounded request failed; recovery state unknown]"}
            ],
            "isError": True,
            "structuredContent": {"isError": True},
        },
    }


def _bounded(request):
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or "id" not in request:
        return False
    params = request.get("params")
    if request.get("method") != "tools/call" or not isinstance(params, dict):
        return False
    arguments = params.get("arguments")
    return (
        params.get("name") == "usagetrim_exec"
        and isinstance(arguments, dict)
        and arguments.get("bounded") is True
    )


@dataclass
class _Job:
    request: dict = field(repr=False)
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    publishing: bool = False


class _Dispatcher:
    def __init__(self, output, errors, respond):
        self.output, self.errors, self.respond = output, errors, respond
        self.lock = threading.Lock()
        self.output_lock = threading.Lock()
        self.closed = threading.Event()
        self.active = {}
        self.workers = []
        self.jobs = queue.Queue(maxsize=MAX_ACTIVE_EXECUTIONS)

    def _cancel_all(self):
        with self.lock:
            self.closed.set()
            for job in self.active.values():
                job.cancel.set()

    def write(self, response):
        if response is None or self.closed.is_set():
            return
        try:
            wire = json.dumps(response) + "\n"
        except (TypeError, ValueError):
            wire = json.dumps(_error(None, -32603, "Response serialization failed")) + "\n"
        try:
            with self.output_lock:
                self.output.write(wire)
                self.output.flush()
        except (OSError, ValueError):
            # Broken stdout is a disconnected transport, not a child success.
            self._cancel_all()

    def duplicate(self, identifier):
        with self.lock:
            return _key(identifier) in self.active

    def cancel(self, identifier):
        key = _key(identifier)
        if key is None:
            return
        with self.lock:
            job = self.active.get(key)
            # Once publication starts, cancellation is too late. The state lock
            # is never held during potentially backpressured stdout writes.
            if job is not None and not job.publishing:
                job.cancel.set()

    def submit(self, request):
        key = _key(request["id"])
        rejected = None
        with self.lock:
            if self.closed.is_set():
                return
            if key in self.active:
                rejected = _error(None, -32600, "Duplicate active request ID")
            elif len(self.active) >= MAX_ACTIVE_EXECUTIONS:
                rejected = _error(
                    request["id"], -32000, "Bounded execution limit reached; retry later"
                )
            else:
                try:
                    if not self.workers:
                        for index in range(MAX_ACTIVE_EXECUTIONS):
                            worker = threading.Thread(
                                target=self._worker,
                                name=f"usagetrim-mcp-bounded-{index}",
                                daemon=True,
                            )
                            worker.start()
                            self.workers.append(worker)
                    job = _Job(request)
                    self.active[key] = job
                    self.jobs.put_nowait((key, job))
                except (RuntimeError, queue.Full):
                    self.active.pop(key, None)
                    rejected = _error(request["id"], -32000, "Bounded worker unavailable")
        if rejected is not None:
            self.write(rejected)

    def _worker(self):
        while True:
            try:
                key, job = self.jobs.get(timeout=0.1)
            except queue.Empty:
                if self.closed.is_set():
                    return
                continue
            response = None
            try:
                if not job.cancel.is_set():
                    try:
                        response = self.respond(job.request, job.cancel)
                    except BaseException:
                        # Always release the admission slot, even after an
                        # unexpected handler failure. No exception body is sent.
                        response = _tool_failure(job.request["id"])
                with self.lock:
                    publish = not self.closed.is_set() and not job.cancel.is_set()
                    job.publishing = publish
                if publish:
                    self.write(response)
            except BaseException:
                self._cancel_all()
                try:
                    self.errors.write("MCP bounded worker failed; owned cleanup was requested.\n")
                    self.errors.flush()
                except (OSError, ValueError):
                    pass
            finally:
                with self.lock:
                    self.active.pop(key, None)
                self.jobs.task_done()
                # Do not keep the last request/preview alive while waiting.
                del response, key, job

    def close(self):
        self._cancel_all()
        deadline = time.monotonic() + SHUTDOWN_SECONDS
        for worker in self.workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in self.workers):
            # Python threads/native SQLite I/O cannot safely be forcibly stopped.
            # Reader ownership and actual process cleanup remain M3's job.
            try:
                self.errors.write("MCP bounded shutdown incomplete; cleanup is still pending.\n")
                self.errors.flush()
            except (OSError, ValueError):
                pass
            return False
        return True


def serve(source, output, errors, respond):
    dispatcher = _Dispatcher(output, errors, respond)
    try:
        for line in _input_lines(source, dispatcher.closed):
            if dispatcher.closed.is_set():
                break
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                dispatcher.write(_error(None, -32700, "Parse error"))
                continue
            if (
                isinstance(request, dict)
                and request.get("jsonrpc") == "2.0"
                and request.get("method") == "notifications/cancelled"
                and "id" not in request
            ):
                params = request.get("params")
                if isinstance(params, dict) and "requestId" in params:
                    dispatcher.cancel(params["requestId"])
                continue
            if isinstance(request, dict) and "id" in request:
                if _key(request["id"]) is None:
                    dispatcher.write(_error(None, -32600, "Invalid request ID"))
                    continue
                if dispatcher.duplicate(request["id"]):
                    # id=null avoids impersonating the still-running original.
                    dispatcher.write(_error(None, -32600, "Duplicate active request ID"))
                    continue
            if _bounded(request):
                dispatcher.submit(request)
            else:
                dispatcher.write(respond(request))
    finally:
        if not dispatcher.close():  # EOF, interrupt and reader errors cancel owned work.
            raise SystemExit(1)
