import asyncio
import json
import socket
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from typer.testing import CliRunner

from usagetrim.cli import app
from usagetrim.mcp import sse
from usagetrim.mcp.server import tool_definitions


def test_startup(monkeypatch):
    monkeypatch.delenv("USAGETRIM_MCP_AUTH_TOKEN", raising=False)
    calls = []
    monkeypatch.setattr(sse, "run_mcp_sse_server", lambda *args: calls.append(args))
    result = CliRunner().invoke(app, ["mcp", "--transport", "sse"])
    assert result.exit_code == 0, result.output
    assert calls == [("coding", "127.0.0.1", 8080, None)]
    assert CliRunner().invoke(app, ["mcp", "--transport", "bad"]).exit_code != 0
    with pytest.raises(ValueError, match="auth-token"):
        sse.SSEApplication(host="0.0.0.0")
    with pytest.raises(ValueError):
        sse.SSEApplication(auth_token="")
    sse.SSEApplication(host="0.0.0.0", auth_token="secret")


def read_message(stream, timeout=10):
    """Bound the entire response wait, even if keepalives or partial lines arrive."""
    connection = stream.fp.raw._sock

    def expire():
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # The test may already have closed the connection.

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        while True:
            line = stream.readline()
            if line == b"event: message\n":
                break
            assert line in {b": keepalive\n", b"\n"}, line
        payload = stream.readline()
        assert payload.startswith(b"data: "), payload
        assert stream.readline() == b"\n"
        return payload.removeprefix(b"data: ")
    finally:
        timer.cancel()
        timer.join(timeout=2)
        assert not timer.is_alive()


@pytest.mark.parametrize("profile", ["minimal", "coding", "desktop", "full"])
def test_real_http_sse(profile, tmp_path):
    uvicorn = pytest.importorskip("uvicorn")
    application = sse.SSEApplication(profile, auth_token="secret")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(application, lifespan="off", log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"

    def request(path, data=None, token="secret", **headers):
        headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return urlopen(Request(base + path, data=data, headers=headers), timeout=20)

    stream = None
    try:
        # The bound listening socket also provides deterministic startup synchronization.
        import time

        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        for path, data in [("/sse", None), ("/messages?session_id=bad", b"{}")]:
            for token in [None, "wrong"]:
                with pytest.raises(HTTPError) as error:
                    request(path, data, token)
                assert error.value.code == 401
            with pytest.raises(HTTPError) as error:
                request(path, data, Origin="https://evil.example")
            assert error.value.code == 403
        with pytest.raises(HTTPError) as error:
            request("/messages?session_id=bad", b"{}")
        assert error.value.code == 404
        stream = request("/sse")
        assert stream.headers["Content-Type"] == "text/event-stream"
        assert stream.readline() == b"event: endpoint\n"
        endpoint = stream.readline().decode().strip().removeprefix("data: ")
        assert stream.readline() == b"\n"
        source = tmp_path / "sample.txt"
        source.write_text("SSE read integration marker", encoding="utf-8")
        for index, method in enumerate(["initialize", "ping", "tools/list", "tools/call"]):
            message = {"jsonrpc": "2.0", "id": index, "method": method}
            if method == "tools/call":
                message["params"] = {"name": "usagetrim_read", "arguments": {"path": str(source)}}
            payload = json.dumps(message).encode()
            with request(endpoint, payload) as response:
                assert response.status == 202
            wire_response = read_message(stream)
            response = json.loads(wire_response)
            assert response["id"] == index
            if method == "initialize":
                assert response["result"]["protocolVersion"] == "2024-11-05"
            elif method == "tools/list":
                assert response["result"]["tools"] == tool_definitions(profile)
            elif method == "tools/call":
                assert not response["result"]["isError"]
                assert response["result"]["structuredContent"] == {"isError": False}
                assert wire_response.count(b"SSE read integration marker") == 1
                assert "SSE read integration marker" in response["result"]["content"][0]["text"]
            else:
                assert response["result"] == {}
    finally:
        if stream:
            stream.close()
        server.should_exit = True
        server.force_exit = True
        thread.join(timeout=10)
        sock.close()
    assert not thread.is_alive()
    assert not application.sessions


def test_limits_and_disconnect(monkeypatch):
    async def scenario():
        application = sse.SSEApplication()
        received = asyncio.Queue()
        sent = asyncio.Queue()

        async def send(event):
            await sent.put(event)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/sse",
            "headers": [(b"host", b"localhost")],
            "client": ("127.0.0.1", 12345),
        }
        task = asyncio.create_task(application(scope, received.get, send))
        try:
            await asyncio.wait_for(sent.get(), 2)
            endpoint = (
                (await asyncio.wait_for(sent.get(), 2))["body"].decode().split("data: ")[1].strip()
            )

            async def post(body, headers=()):
                events = []

                async def receive():
                    return {"type": "http.request", "body": body}

                async def collect(event):
                    events.append(event)

                await asyncio.wait_for(
                    application(
                        {
                            **scope,
                            "method": "POST",
                            "path": "/messages",
                            "query_string": endpoint.split("?")[1].encode(),
                            "headers": [
                                *scope["headers"],
                                (b"content-type", b"application/json"),
                                *headers,
                            ],
                        },
                        receive,
                        collect,
                    ),
                    2,
                )
                return events[0]["status"]

            monkeypatch.setattr(sse, "MAX_REQUEST_BYTES", 10)
            assert await post(b"x" * 11) == 413
            assert await post(b"{") == 400
            session = next(iter(application.sessions.values()))
            for _ in range(sse.QUEUE_SIZE):
                session.incoming.put_nowait(b"{}")
                application.pending_request_bytes += sse.MAX_REQUEST_BYTES
            assert await post(b"{}") == 429
            await received.put({"type": "http.disconnect"})
            await asyncio.wait_for(task, 2)
            assert not application.sessions
            assert await post(b"{}") == 404
            events = []

            async def collect(event):
                events.append(event)

            await application(
                {**scope, "headers": [(b"host", b"evil.example")]}, received.get, collect
            )
            assert events[0]["status"] == 403
        finally:
            await received.put({"type": "http.disconnect"})
            await asyncio.wait_for(task, 2)

    asyncio.run(scenario())


@asynccontextmanager
async def asgi_session(application, message_send=None):
    received, sent = asyncio.Queue(), asyncio.Queue()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/sse",
        "headers": [(b"host", b"localhost")],
        "client": ("127.0.0.1", 12345),
    }

    async def send(event):
        if message_send is not None and event.get("body", b"").startswith(b"event: message"):
            await message_send(event)
        await sent.put(event)

    task = asyncio.create_task(application(scope, received.get, send))

    async def post(message):
        events = []

        async def receive():
            return {"type": "http.request", "body": json.dumps(message).encode()}

        async def collect(event):
            events.append(event)

        await asyncio.wait_for(
            application(
                {
                    **scope,
                    "method": "POST",
                    "path": "/messages",
                    "query_string": endpoint.split("?")[1].encode(),
                    "headers": [*scope["headers"], (b"content-type", b"application/json")],
                },
                receive,
                collect,
            ),
            2,
        )
        return events[0]["status"]

    try:
        await asyncio.wait_for(sent.get(), 2)
        endpoint = (
            (await asyncio.wait_for(sent.get(), 2))["body"].decode().split("data: ")[1].strip()
        )
        yield post, received, sent, task
    finally:
        await received.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, 2)


def test_global_request_budget_across_sessions(monkeypatch):
    monkeypatch.setattr(sse, "MAX_PENDING_REQUEST_BYTES", 2 * sse.MAX_REQUEST_BYTES)

    async def scenario():
        application = sse.SSEApplication()
        # Simulate a busy executor so accepted requests remain pending.
        await application.execution_lock.acquire()
        try:
            async with asgi_session(application) as first, asgi_session(application) as second:
                message = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
                assert await first[0](message) == 202
                assert await second[0](message) == 202
                assert application.pending_request_bytes == sse.MAX_PENDING_REQUEST_BYTES
                assert await first[0](message) == 429
                assert await second[0](message) == 429
                # Remove one session: both queued and lock-waiting requests release reservations.
                await first[1].put({"type": "http.disconnect"})
                await asyncio.wait_for(first[3], 2)
                assert application.pending_request_bytes == sse.MAX_REQUEST_BYTES
                assert await second[0](message) == 202
        finally:
            application.execution_lock.release()
        assert not application.sessions
        assert application.pending_request_bytes == 0

    asyncio.run(scenario())


def test_disconnect_during_sync_tool_does_not_block_http(monkeypatch):
    started, release = threading.Event(), threading.Event()
    original = sse._respond

    def respond(request, **kwargs):
        if request["method"] == "tools/call":
            started.set()
            assert release.wait(10)
            return {"jsonrpc": "2.0", "id": request["id"], "result": {}}
        return original(request, **kwargs)

    monkeypatch.setattr(sse, "_respond", respond)

    async def scenario():
        application = sse.SSEApplication()
        try:
            async with asgi_session(application) as first, asgi_session(application) as second:
                assert await first[0]({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}) == 202
                assert await asyncio.to_thread(started.wait, 2)
                assert await first[0]({"jsonrpc": "2.0", "id": 2, "method": "ping"}) == 202
                await first[1].put({"type": "http.disconnect"})
                await asyncio.wait_for(first[3], 2)
                assert len(application.sessions) == 1
                assert application.execution_lock.locked()
                assert application.pending_request_bytes == sse.MAX_REQUEST_BYTES
                assert await first[0]({}) == 404
                assert await second[0]({"jsonrpc": "2.0", "id": 3, "method": "ping"}) == 202
                assert second[2].empty()  # Execution remains serialized until the tool returns.
                release.set()
                event = await asyncio.wait_for(second[2].get(), 2)
                response = json.loads(event["body"].split(b"data: ")[1])
                assert response["id"] == 3
                assert application.pending_request_bytes == 0
                assert first[2].empty()  # No response delivered after disconnect.
        finally:
            release.set()
            if application.executions:
                await asyncio.gather(*application.executions)
        assert not application.sessions

    asyncio.run(scenario())


@contextmanager
def running_server(application):
    uvicorn = pytest.importorskip("uvicorn")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(application, lifespan="off", log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


@pytest.mark.parametrize("profile", ["minimal", "coding", "desktop", "full"])
def test_official_mcp_client_two_sessions(tmp_path, profile):
    pytest.importorskip("mcp")
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    first_file, second_file = tmp_path / "first.txt", tmp_path / "second.txt"
    first_file.write_text("FIRST_SESSION_CONTENT", encoding="utf-8")
    second_file.write_text("SECOND_SESSION_CONTENT", encoding="utf-8")
    application = sse.SSEApplication(profile=profile, auth_token="secret")

    async def scenario(base):
        async with sse_client(
            base + "/sse", headers={"Authorization": "Bearer secret"}
        ) as first_streams:
            async with ClientSession(*first_streams) as first:
                await first.initialize()
                async with sse_client(
                    base + "/sse", headers={"Authorization": "Bearer secret"}
                ) as second_streams:
                    async with ClientSession(*second_streams) as second:
                        await second.initialize()
                        assert len(application.sessions) == 2

                        results = await asyncio.gather(
                            first.call_tool("usagetrim_read", {"path": str(first_file)}),
                            second.call_tool("usagetrim_read", {"path": str(second_file)}),
                        )
                        for result, expected, other in zip(
                            results,
                            ["FIRST_SESSION_CONTENT", "SECOND_SESSION_CONTENT"],
                            ["SECOND_SESSION_CONTENT", "FIRST_SESSION_CONTENT"],
                            strict=True,
                        ):
                            payload = result.model_dump(by_alias=True, exclude_unset=True)
                            assert payload["structuredContent"] == {"isError": False}
                            assert json.dumps(payload).count(expected) == 1
                            assert not payload["isError"]
                            assert expected in payload["content"][0]["text"]
                            assert other not in payload["content"][0]["text"]
                await first.send_ping()  # Closing the second session leaves the first usable.

    with running_server(application) as base:
        asyncio.run(asyncio.wait_for(scenario(base), 15))
    assert not application.sessions
    assert application.pending_request_bytes == 0


@pytest.mark.parametrize("explicit", [False, True])
def test_auth_token_environment(monkeypatch, explicit):
    calls = []
    monkeypatch.setenv("USAGETRIM_MCP_AUTH_TOKEN", "environment-secret")
    monkeypatch.setattr(sse, "run_mcp_sse_server", lambda *args: calls.append(args))
    args = ["mcp", "--transport", "sse", "--host", "0.0.0.0"]
    if explicit:
        args.extend(["--auth-token", "explicit-secret"])
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert calls[0][1] == "0.0.0.0"
    assert calls[0][3] == ("explicit-secret" if explicit else "environment-secret")


def test_remote_listen_without_auth_is_rejected(monkeypatch):
    monkeypatch.delenv("USAGETRIM_MCP_AUTH_TOKEN", raising=False)
    result = CliRunner().invoke(app, ["mcp", "--transport", "sse", "--host", "0.0.0.0"])
    with pytest.raises(ValueError, match=r"requires --auth-token"):
        sse.SSEApplication(host="0.0.0.0")
    assert result.exit_code == 2


def test_execute_cancellation_keeps_lock_until_thread_finishes(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def respond(request, **kwargs):
        started.set()
        assert release.wait(5), "Test did not release synchronous handler"
        return {"jsonrpc": "2.0", "id": 1, "result": {}}

    monkeypatch.setattr(sse, "_respond", respond)

    async def scenario():
        application = sse.SSEApplication()
        application.pending_request_bytes = sse.MAX_REQUEST_BYTES
        await application.execution_lock.acquire()
        execution = asyncio.create_task(application._execute(b"{}"))
        attempted, entered = asyncio.Event(), asyncio.Event()

        async def next_handler():
            attempted.set()
            async with application.execution_lock:
                entered.set()

        waiter = None
        try:
            assert await asyncio.to_thread(started.wait, 2)
            execution.cancel()
            waiter = asyncio.create_task(next_handler())
            await asyncio.wait_for(attempted.wait(), 2)
            assert application.execution_lock.locked()
            assert application.pending_request_bytes == sse.MAX_REQUEST_BYTES
            assert not entered.is_set()
            assert not execution.done()
            execution.cancel()  # Repeated cancellation must not release ownership either.
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(execution, return_exceptions=True), 2)
            if waiter is not None:
                await asyncio.wait_for(waiter, 2)
        assert execution.cancelled()
        assert entered.is_set()
        assert application.pending_request_bytes == 0
        assert not application.execution_lock.locked()

    asyncio.run(scenario())


def test_response_limit_closes_session_without_partial_json(monkeypatch):
    def respond(request, **kwargs):
        return {"jsonrpc": "2.0", "id": 1, "result": "x" * 1024}

    monkeypatch.setattr(sse, "_respond", respond)

    async def scenario():
        application = sse.SSEApplication(max_response_bytes=128, max_pending_response_bytes=1024)
        async with asgi_session(application) as session:
            assert await session[0]({"id": 1}) == 202
            await asyncio.wait_for(asyncio.shield(session[3]), 2)
            final = await asyncio.wait_for(session[2].get(), 2)
            assert final == {"type": "http.response.body", "body": b"", "more_body": False}
            assert session[2].empty()
            assert not application.sessions
            assert application.pending_request_bytes == 0
            assert application.pending_response_bytes == 0

    asyncio.run(scenario())


def test_response_budget_counts_sending_and_queued_frames_and_disconnect(monkeypatch):
    monkeypatch.setattr(sse, "_respond", lambda request, **kwargs: {"id": request["id"]})

    async def scenario():
        limit = 128
        maximum = 2 * (limit + sse.SSE_MESSAGE_OVERHEAD)
        application = sse.SSEApplication(
            max_response_bytes=limit, max_pending_response_bytes=maximum
        )
        sending, release = asyncio.Event(), asyncio.Event()

        async def blocked_send(event):
            sending.set()
            await release.wait()

        async with asgi_session(application, blocked_send) as first:
            assert await first[0]({"id": 1}) == 202
            await asyncio.wait_for(sending.wait(), 2)
            assert application.pending_response_bytes > 0
            # A sending frame leaves too little room for the next full reservation.
            async with asgi_session(application) as second:
                assert await second[0]({"id": 2}) == 202
                await asyncio.wait_for(asyncio.shield(second[3]), 2)
                final = await asyncio.wait_for(second[2].get(), 2)
                assert final == {"type": "http.response.body", "body": b"", "more_body": False}
                assert second[2].empty()
                assert 0 < application.pending_response_bytes <= maximum
            await first[1].put({"type": "http.disconnect"})
            await asyncio.wait_for(asyncio.shield(first[3]), 2)
        assert application.pending_response_bytes == 0
        assert application.pending_request_bytes == 0

    asyncio.run(scenario())


def test_response_reservations_release_after_send_and_queue_disconnect(monkeypatch):
    monkeypatch.setattr(sse, "_respond", lambda request, **kwargs: {"id": request["id"]})

    async def scenario():
        application = sse.SSEApplication(max_response_bytes=128, max_pending_response_bytes=2048)
        async with asgi_session(application) as session:
            assert await session[0]({"id": 1}) == 202
            event = await asyncio.wait_for(session[2].get(), 2)
            assert json.loads(event["body"].split(b"data: ")[1]) == {"id": 1}
            assert application.pending_response_bytes == 0

        sending, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def blocked_send(event):
            sending.set()
            await release.wait()

        async with asgi_session(application, blocked_send) as session:
            assert await session[0]({"id": 2}) == 202
            await asyncio.wait_for(sending.wait(), 2)
            stored = next(iter(application.sessions.values()))
            original_put = stored.outgoing.put

            async def record_put(value):
                await original_put(value)
                queued.set()

            monkeypatch.setattr(stored.outgoing, "put", record_put)
            assert await session[0]({"id": 3}) == 202
            await asyncio.wait_for(queued.wait(), 2)
            assert stored.outgoing.qsize() == 1
            assert application.pending_response_bytes > 0
        assert application.pending_response_bytes == 0
        assert application.pending_request_bytes == 0

    asyncio.run(scenario())


def test_serialization_runs_off_loop_and_releases_response_object(monkeypatch):
    import weakref

    class Response(dict):
        pass

    async def scenario():
        loop_thread = threading.get_ident()
        references = []

        def respond(request, **kwargs):
            assert threading.get_ident() != loop_thread
            response = Response(id=1, result="Unicode: żółw")
            references.append(weakref.ref(response))
            return response

        original_iterencode = json.JSONEncoder.iterencode

        serialized = []

        def iterencode(encoder, value, *args, **kwargs):
            if isinstance(value, Response):
                assert threading.get_ident() != loop_thread
                serialized.append(True)
            return original_iterencode(encoder, value, *args, **kwargs)

        monkeypatch.setattr(sse, "_respond", respond)
        application = sse.SSEApplication()
        monkeypatch.setattr(json.JSONEncoder, "iterencode", iterencode)
        async with asgi_session(application) as session:
            assert await session[0]({"id": 1}) == 202
            event = await asyncio.wait_for(session[2].get(), 2)
            assert serialized == [True]
            assert json.loads(event["body"].split(b"data: ")[1])["result"] == "Unicode: żółw"
            assert references[0]() is None
            assert application.pending_response_bytes == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("client", [None, ("203.0.113.7", 1234)])
def test_unauthenticated_asgi_deployment_requires_real_loopback_peer(client):
    async def scenario():
        application = sse.SSEApplication()
        events = []

        async def receive():
            raise AssertionError("Rejected requests must not read a body")

        async def send(event):
            events.append(event)

        await asyncio.wait_for(
            application(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/sse",
                    "headers": [(b"host", b"localhost")],
                    "client": client,
                },
                receive,
                send,
            ),
            2,
        )
        assert events[0]["status"] == 403
        assert not application.sessions

    asyncio.run(scenario())


def test_disconnect_and_cancelled_execution_hold_response_reservation(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def respond(request, **kwargs):
        started.set()
        assert release.wait(5)
        return {"id": 1}

    monkeypatch.setattr(sse, "_respond", respond)

    async def scenario():
        application = sse.SSEApplication(max_response_bytes=128, max_pending_response_bytes=1024)
        try:
            async with asgi_session(application) as session:
                assert await session[0]({"id": 1}) == 202
                assert await asyncio.to_thread(started.wait, 2)
                execution = next(iter(application.executions))
                execution.cancel()
                await session[1].put({"type": "http.disconnect"})
                await asyncio.wait_for(asyncio.shield(session[3]), 2)
                assert application.execution_lock.locked()
                assert application.pending_response_bytes > 0
                assert application.pending_request_bytes == sse.MAX_REQUEST_BYTES
                assert session[2].empty()
        finally:
            release.set()
            await asyncio.wait_for(
                asyncio.gather(*application.executions, return_exceptions=True), 2
            )
        assert application.pending_response_bytes == 0
        assert application.pending_request_bytes == 0
        assert not application.execution_lock.locked()

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_response_limits_require_positive_integers(value):
    with pytest.raises(ValueError, match="positive integers"):
        sse.SSEApplication(max_response_bytes=value)
    with pytest.raises(ValueError, match="positive integers"):
        sse.SSEApplication(max_pending_response_bytes=value)


def test_global_response_limit_must_allow_serialization_copy():
    with pytest.raises(ValueError, match="buffer copy"):
        sse.SSEApplication(max_response_bytes=128, max_pending_response_bytes=128)


def test_execution_cancelled_before_start_releases_request_and_lock(monkeypatch):
    def respond(*args, **kwargs):
        raise AssertionError("A cancelled-before-start execution must not run")

    monkeypatch.setattr(sse, "_respond", respond)

    async def scenario():
        application = sse.SSEApplication()
        application.pending_request_bytes = sse.MAX_REQUEST_BYTES
        await application.execution_lock.acquire()
        started = [False]
        execution = asyncio.create_task(application._execute(b"{}", started))
        application.executions.add(execution)
        execution.add_done_callback(lambda task: application._execution_done(task, started))
        execution.cancel()
        await asyncio.wait_for(asyncio.gather(execution, return_exceptions=True), 2)
        assert not started[0]
        assert not application.executions
        assert not application.execution_lock.locked()
        assert application.pending_request_bytes == 0

    asyncio.run(scenario())


def test_response_limit_exact_boundary(monkeypatch):
    response = {"id": 1, "result": "Unicode: żółw"}
    monkeypatch.setattr(sse, "_respond", lambda request, **kwargs: response)
    size = len(json.dumps(response).encode("ascii"))

    async def scenario():
        application = sse.SSEApplication(max_response_bytes=size, max_pending_response_bytes=1024)
        async with asgi_session(application) as session:
            assert await session[0]({"id": 1}) == 202
            event = await asyncio.wait_for(session[2].get(), 2)
            assert json.loads(event["body"].split(b"data: ")[1]) == response
            assert application.pending_response_bytes == 0

    asyncio.run(scenario())
