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
        return urlopen(Request(base + path, data=data, headers=headers), timeout=5)

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
            assert stream.readline() == b"event: message\n"
            wire_response = stream.readline().removeprefix(b"data: ")
            response = json.loads(wire_response)
            assert stream.readline() == b"\n"
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
        }
        task = asyncio.create_task(application(scope, received.get, send))
        await sent.get()
        endpoint = (await sent.get())["body"].decode().split("data: ")[1].strip()

        async def post(body, headers=()):
            events = []

            async def receive():
                return {"type": "http.request", "body": body}

            async def collect(event):
                events.append(event)

            await application(
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

        await application({**scope, "headers": [(b"host", b"evil.example")]}, received.get, collect)
        assert events[0]["status"] == 403

    asyncio.run(scenario())


@asynccontextmanager
async def asgi_session(application):
    received, sent = asyncio.Queue(), asyncio.Queue()
    scope = {"type": "http", "method": "GET", "path": "/sse", "headers": [(b"host", b"localhost")]}

    async def send(event):
        await sent.put(event)

    task = asyncio.create_task(application(scope, received.get, send))
    await asyncio.wait_for(sent.get(), 2)
    endpoint = (await sent.get())["body"].decode().split("data: ")[1].strip()

    async def post(message):
        events = []

        async def receive():
            return {"type": "http.request", "body": json.dumps(message).encode()}

        async def collect(event):
            events.append(event)

        await application(
            {
                **scope,
                "method": "POST",
                "path": "/messages",
                "query_string": endpoint.split("?")[1].encode(),
                "headers": [*scope["headers"], (b"content-type", b"application/json")],
            },
            receive,
            collect,
        )
        return events[0]["status"]

    try:
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
    assert result.exit_code != 0
    assert "requires --auth-token" in result.output
