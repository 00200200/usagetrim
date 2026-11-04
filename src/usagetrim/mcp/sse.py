"""Legacy MCP HTTP+SSE (2024-11-05), for native clients only.

GET /sse advertises a session-specific POST endpoint. No replay or browser
CORS access is provided. Install usagetrim[mcp] to run the ASGI application.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import secrets
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from usagetrim.mcp.server import _respond, tool_definitions

MAX_REQUEST_BYTES = 1024 * 1024
MAX_PENDING_REQUEST_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PENDING_RESPONSE_BYTES = 32 * 1024 * 1024
SSE_MESSAGE_OVERHEAD = len(b"event: message\ndata: \n\n")
QUEUE_SIZE = 32
MAX_SESSIONS = 128
REQUEST_TIMEOUT = 10


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _ResponseLimit(Exception):
    """Close a slow/oversized session without emitting a partial JSON response."""


@dataclass
class _Reservation:
    app: SSEApplication
    size: int

    def resize(self, size):
        self.app.pending_response_bytes += size - self.size
        self.size = size

    def release(self):
        self.resize(0)


@dataclass
class _Session:
    incoming: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))
    outgoing: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))


class SSEApplication:
    def __init__(
        self,
        profile="coding",
        host="127.0.0.1",
        auth_token=None,
        *,
        max_response_bytes=MAX_RESPONSE_BYTES,
        max_pending_response_bytes=MAX_PENDING_RESPONSE_BYTES,
    ):
        tool_definitions(profile)
        if auth_token is not None and (not auth_token or any(c.isspace() for c in auth_token)):
            raise ValueError("auth-token must be nonempty and contain no whitespace")
        if not _loopback(host) and not auth_token:
            raise ValueError("Listening outside localhost requires --auth-token")
        for value in (max_response_bytes, max_pending_response_bytes):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("Response limits must be positive integers")
        if max_pending_response_bytes < 2 * (max_response_bytes + SSE_MESSAGE_OVERHEAD):
            raise ValueError(
                "Global response budget must fit a maximum SSE response and its buffer copy"
            )
        self.max_response_bytes = max_response_bytes
        self.max_pending_response_bytes = max_pending_response_bytes
        self.pending_response_bytes = 0
        self.profile = profile
        self.auth_token = auth_token
        self.sessions: dict[str, _Session] = {}
        # Existing synchronous handlers share process metrics: serialize execution.
        self.execution_lock = asyncio.Lock()
        self.pending_request_bytes = 0
        self.executions: set[asyncio.Task] = set()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            raise ValueError("Only HTTP is supported")
        headers = dict(scope["headers"])

        async def reply(status, text="", extra=()):
            body = text.encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"text/plain"),
                        (b"content-length", str(len(body)).encode()),
                        *extra,
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})

        if b"origin" in headers or headers.get(b"sec-fetch-site", b"none") != b"none":
            return await reply(403, "Browser requests are not supported")
        if self.auth_token is not None:
            expected = ("Bearer " + self.auth_token).encode()
            if not secrets.compare_digest(headers.get(b"authorization", b""), expected):
                return await reply(401, "Unauthorized", [(b"www-authenticate", b"Bearer")])
        else:
            try:
                hostname = urlsplit("//" + headers.get(b"host", b"").decode()).hostname
            except (ValueError, UnicodeError):
                hostname = None
            client = scope.get("client")
            if client is None or not _loopback(client[0]):
                return await reply(403, "Loopback peer or Bearer authentication required")
            if not hostname or not _loopback(hostname):
                return await reply(403, "Invalid Host")
        path, method = scope["path"], scope["method"]
        if path == "/sse" and method == "GET":
            if len(self.sessions) >= MAX_SESSIONS:
                return await reply(503, "Session limit reached")
            return await self._stream(receive, send)
        if path != "/messages":
            return await reply(405 if path == "/sse" else 404)
        if method != "POST":
            return await reply(405)
        query = parse_qs(scope.get("query_string", b"").decode("ascii", errors="replace"))
        ids = query.get("session_id", [])
        session = self.sessions.get(ids[0]) if len(ids) == 1 else None
        if session is None:
            return await reply(404, "Unknown session")
        if (
            headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
            != b"application/json"
        ):
            return await reply(415, "Expected application/json")
        # Reserve a full request before reading. This also bounds concurrent uploads
        # and requests waiting for the execution lock; queues store raw bytes only.
        if self.pending_request_bytes + MAX_REQUEST_BYTES > MAX_PENDING_REQUEST_BYTES:
            return await reply(429, "Global request budget exhausted")
        self.pending_request_bytes += MAX_REQUEST_BYTES
        queued = False
        try:
            body = bytearray()
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    while True:
                        event = await receive()
                        if event["type"] == "http.disconnect":
                            return
                        chunk = event.get("body", b"")
                        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                            return await reply(413, "Request too large")
                        body.extend(chunk)
                        if not event.get("more_body", False):
                            break
            except TimeoutError:
                return await reply(408)
            try:
                json.loads(body)
            except (ValueError, UnicodeError, RecursionError):
                return await reply(400, "Invalid JSON")
            if self.sessions.get(ids[0]) is not session:
                return await reply(404, "Unknown session")
            try:
                session.incoming.put_nowait(bytes(body))
            except asyncio.QueueFull:
                return await reply(429, "Session queue full")
            queued = True
            return await reply(202, "Accepted")
        finally:
            if not queued:
                self.pending_request_bytes -= MAX_REQUEST_BYTES

    def _respond_frame(self, body):
        # Run both the synchronous handler and serialization outside the event loop.
        # iterencode limits retained serialized output, not handler/encoder temporaries.
        response = _respond(json.loads(body), profile=self.profile)
        if response is None:
            return None
        data = bytearray(b"event: message\ndata: ")
        payload_size = 0
        for chunk in json.JSONEncoder().iterencode(response):
            payload_size += len(chunk)  # ensure_ascii=True: one byte per character.
            if payload_size > self.max_response_bytes:
                raise _ResponseLimit("SSE response size limit exceeded")
            data.extend(chunk.encode("ascii"))
        data.extend(b"\n\n")
        return bytes(data)

    async def _execute(self, body, started=None):
        if started is not None:
            started[0] = True
        # Cancellation cannot stop a Python thread. Keep ownership until it ends,
        # including repeated cancellations during application shutdown.
        try:
            work = asyncio.get_running_loop().run_in_executor(None, self._respond_frame, body)
            body = None
            cancelled = False
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break  # Retrieve and propagate the handler exception below.
            if cancelled:
                if not work.cancelled():
                    work.exception()
                raise asyncio.CancelledError
            return work.result()
        finally:
            self.pending_request_bytes -= MAX_REQUEST_BYTES
            self.execution_lock.release()

    def _execution_done(self, task, started):
        # A coroutine cancelled before its first step never enters its finally.
        if not started[0]:
            self.pending_request_bytes -= MAX_REQUEST_BYTES
            self.execution_lock.release()
        self.executions.discard(task)
        if not task.cancelled():
            task.exception()  # Retrieve exceptions even when the client has disconnected.

    async def _worker(self, session):
        while True:
            body = await session.incoming.get()
            transferred = False
            reservation = None
            task = None
            frame = None
            acquired = False
            try:
                await self.execution_lock.acquire()
                acquired = True
                # Account for bytearray -> bytes conversion during serialization.
                size = 2 * (self.max_response_bytes + SSE_MESSAGE_OVERHEAD)
                if self.pending_response_bytes + size > self.max_pending_response_bytes:
                    raise _ResponseLimit("Global SSE response budget exhausted")
                self.pending_response_bytes += size
                reservation = _Reservation(self, size)
                started = [False]
                task = asyncio.create_task(self._execute(body, started))
                body = None
                self.executions.add(task)
                task.add_done_callback(
                    lambda finished, state=started: self._execution_done(finished, state)
                )
                transferred = True
                frame = await asyncio.shield(task)
                task = None  # A completed task also retains its result.
                if frame is not None:
                    reservation.resize(len(frame))
                    await session.outgoing.put((frame, reservation))
                    reservation = None  # The sender/queue now owns the reservation.
            finally:
                body = frame = None
                if not transferred:
                    self.pending_request_bytes -= MAX_REQUEST_BYTES
                    if acquired:
                        self.execution_lock.release()
                if reservation is not None:
                    if task is not None and not task.done():
                        # Disconnect returns immediately; the thread still owns memory.
                        task.add_done_callback(lambda _, held=reservation: held.release())
                    else:
                        reservation.release()
                task = reservation = None

    async def _stream(self, receive, send):
        session_id = secrets.token_urlsafe(32)
        session = self.sessions[session_id] = _Session()

        async def disconnected():
            while (await receive())["type"] != "http.disconnect":
                pass

        async def events():
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/event-stream"),
                        (b"cache-control", b"no-cache"),
                        (b"x-accel-buffering", b"no"),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": (
                        f"event: endpoint\ndata: /messages?session_id={session_id}\n\n".encode()
                    ),
                    "more_body": True,
                }
            )
            while True:
                try:
                    frame, reservation = await asyncio.wait_for(session.outgoing.get(), timeout=15)
                except TimeoutError:
                    await send(
                        {
                            "type": "http.response.body",
                            "body": b": keepalive\n\n",
                            "more_body": True,
                        }
                    )
                    continue
                try:
                    await send({"type": "http.response.body", "body": frame, "more_body": True})
                finally:
                    frame = None
                    reservation.release()
                    reservation = None

        tasks = []
        limited = False
        try:
            tasks = [
                asyncio.create_task(coro)
                for coro in (disconnected(), events(), self._worker(session))
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except _ResponseLimit:
            # Deliberately close this session; never truncate a JSON-RPC envelope.
            limited = True
        finally:
            self.sessions.pop(session_id, None)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            while not session.incoming.empty():
                session.incoming.get_nowait()
                self.pending_request_bytes -= MAX_REQUEST_BYTES
            while not session.outgoing.empty():
                session.outgoing.get_nowait()[1].release()
            if limited:
                await send({"type": "http.response.body", "body": b"", "more_body": False})


def run_mcp_sse_server(profile="coding", host="127.0.0.1", port=8080, auth_token=None):
    app = SSEApplication(profile, host, auth_token)
    import uvicorn

    uvicorn.run(app, host=host, port=port, lifespan="off", access_log=False)
