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


@dataclass
class _Session:
    incoming: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))
    outgoing: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))


class SSEApplication:
    def __init__(self, profile="coding", host="127.0.0.1", auth_token=None):
        tool_definitions(profile)
        if auth_token is not None and (not auth_token or any(c.isspace() for c in auth_token)):
            raise ValueError("auth-token must be nonempty and contain no whitespace")
        if not _loopback(host) and not auth_token:
            raise ValueError("Listening outside localhost requires --auth-token")
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

    async def _execute(self, body):
        # Own the lock and reservation until the synchronous call actually ends.
        # The HTTP handler may leave immediately without cancelling this task.
        try:
            return await asyncio.to_thread(_respond, json.loads(body), profile=self.profile)
        finally:
            self.pending_request_bytes -= MAX_REQUEST_BYTES
            self.execution_lock.release()

    def _execution_done(self, task):
        self.executions.discard(task)
        if not task.cancelled():
            task.exception()  # Retrieve exceptions even when the client has disconnected.

    async def _worker(self, session):
        while True:
            body = await session.incoming.get()
            transferred = False
            try:
                await self.execution_lock.acquire()
                task = asyncio.create_task(self._execute(body))
                body = None  # Do not retain the last request in an idle worker.
                self.executions.add(task)
                task.add_done_callback(self._execution_done)
                transferred = True
                response = await asyncio.shield(task)
            finally:
                if not transferred:
                    self.pending_request_bytes -= MAX_REQUEST_BYTES
            if response is not None:
                await session.outgoing.put(json.dumps(response).encode())

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
                    data = await asyncio.wait_for(session.outgoing.get(), timeout=15)
                    body = b"event: message\ndata: " + data + b"\n\n"
                except TimeoutError:
                    body = b": keepalive\n\n"
                await send({"type": "http.response.body", "body": body, "more_body": True})

        tasks = []
        try:
            tasks = [
                asyncio.create_task(coro)
                for coro in (disconnected(), events(), self._worker(session))
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.sessions.pop(session_id, None)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            while not session.incoming.empty():
                session.incoming.get_nowait()
                self.pending_request_bytes -= MAX_REQUEST_BYTES


def run_mcp_sse_server(profile="coding", host="127.0.0.1", port=8080, auth_token=None):
    app = SSEApplication(profile, host, auth_token)
    import uvicorn

    uvicorn.run(app, host=host, port=port, lifespan="off", access_log=False)
