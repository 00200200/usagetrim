"""MCP/native inspection records facts without changing tool text or counters."""

import os
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys

import pytest

from usagetrim.core import native_hooks
from usagetrim.core.cache import ContextCache, decompress_payload
from usagetrim.core.telemetry import TelemetryStore
from usagetrim.mcp import server
from usagetrim.metrics import tokenizer


@pytest.fixture(autouse=True)
def local_environment(monkeypatch):
    class Encoding:
        name = "o200k_base"

        def encode(self, text, *, disallowed_special=()):
            return list(text.encode("utf-8"))

    encoding = Encoding()
    monkeypatch.setattr(tokenizer, "get_o200k", lambda: encoding)
    monkeypatch.setattr(tokenizer, "get_cl100k", lambda: encoding)
    monkeypatch.setattr(tokenizer, "_O200K_ENC", encoding)
    monkeypatch.setattr(server, "paused", lambda: False)
    monkeypatch.setattr(native_hooks, "paused", lambda: False)
    for name in ("_SESSION_SAVED_CLAUDE", "_SESSION_SAVED_OPENAI", "_SESSION_SAVED_GEMINI"):
        monkeypatch.setattr(server, name, 0)
    monkeypatch.delenv("USAGETRIM_EVENT_REPLAY", raising=False)
    monkeypatch.delenv("USAGETRIM_SEMANTIC_MODEL_PATH", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("Network access attempted")

    monkeypatch.setattr(socket, "create_connection", forbidden)


def python_command(script):
    argv = [sys.executable, "-c", script]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def call(name, arguments):
    return server._respond(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )["result"]


def recorded():
    store = TelemetryStore()
    with store.connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM events ORDER BY sequence").fetchall()
    return [(dict(row), store.get_inspection(row["event_id"])) for row in rows]


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("code,text", [(0, "FAILED text"), (7, "PASSED text")])
def test_mcp_exec_records_actual_exit_and_snapshot_pair(monkeypatch, replay, code, text):
    if replay:
        monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", "1")
    command = python_command(f"print({text!r}); raise SystemExit({code})")
    response = call("usagetrim_exec", {"command": command})
    output = text + f"\n\n[exit code: {code}]"
    assert response == {"content": [{"type": "text", "text": output}], "isError": False}
    [(event, details)] = recorded()
    assert details.child_returncode == code and details.termination_reason == "exited"
    assert details.snapshot_state == ("stored" if replay else "disabled")
    assert event["delivery"] == "returned"
    if replay:
        assert ContextCache().retrieve(details.before_ref) == text + "\n"
        assert ContextCache().retrieve(details.after_ref) == output
    else:
        assert details.before_ref is None and details.after_ref is None
    assert server._SESSION_SAVED_OPENAI == len((text + "\n").encode()) - len(output.encode())


@pytest.mark.skipif(os.name != "posix", reason="Negative subprocess return codes require POSIX")
def test_mcp_exec_records_signal_from_actual_subprocess():
    command = "exec " + python_command("import os, signal; os.kill(os.getpid(), signal.SIGTERM)")
    output = server.handle_usagetrim_exec({"command": command})
    assert output == f"\n[exit code: {-signal.SIGTERM}]"
    [(_, details)] = recorded()
    assert details.child_returncode == -signal.SIGTERM
    assert details.termination_reason == "signaled"


def test_mcp_timeout_keeps_partial_response_without_complete_snapshots(monkeypatch):
    monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", "1")

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 120, output=b"partial diagnostic\n")

    monkeypatch.setattr(server.subprocess, "run", timeout)
    response = call("usagetrim_exec", {"command": "authored-timeout-command"})
    assert response["content"][0]["text"] == (
        "partial diagnostic\n\n[command timed out after 120s; output may be partial]"
    )
    [(_, details)] = recorded()
    assert details.termination_reason == "timed_out" and details.child_returncode is None
    assert details.snapshot_state == "cache_disabled"
    assert details.before_ref is None and details.after_ref is None


def test_mcp_pause_preserves_result_and_blocks_replay(monkeypatch):
    monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", "1")
    monkeypatch.setattr(server, "paused", lambda: True)
    output = server.handle_usagetrim_exec({"command": python_command("print('ok')")})
    assert output == "ok\n\n[exit code: 0]"
    [(event, details)] = recorded()
    assert event["engine"] == "none"
    assert details.child_returncode == 0 and details.snapshot_state == "cache_disabled"


def test_mcp_missing_cache_consent_blocks_synthetic_generated_pair(monkeypatch):
    monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", "1")
    assert server._record("", "generated text", operation="pack") == "generated text"
    [(_, details)] = recorded()
    assert details.snapshot_state == "cache_disabled"
    assert details.before_ref is None and details.after_ref is None


def test_mcp_json_real_transformation_preserves_text_and_counter_formula(monkeypatch):
    source = '[{"id":1},{"id":2},{"id":3}]'
    arguments = {"json_str": source, "max_items": 1, "max_tokens": 1000}
    baseline = server.handle_usagetrim_json(arguments)
    monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", "1")
    output = server.handle_usagetrim_json(arguments)
    assert output == baseline
    _, details = recorded()[-1]
    assert ContextCache().retrieve(details.before_ref) == source
    assert ContextCache().retrieve(details.after_ref) == output
    assert server._SESSION_SAVED_OPENAI == 2 * (len(source.encode()) - len(output.encode()))


def test_mcp_replay_persists_only_redacted_snapshots_and_safe_metadata(monkeypatch):
    monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", "1")
    secret = "ghp_" + "A" * 36
    response = call("usagetrim_exec", {"command": python_command(f"print({secret!r})")})
    assert secret not in response["content"][0]["text"]
    [(_, details)] = recorded()
    assert details.snapshot_state == "stored"
    cache = ContextCache()
    with sqlite3.connect(cache.db_path) as conn:
        payloads = [
            decompress_payload(row[0]) for row in conn.execute("SELECT content FROM output_cache")
        ]
    assert payloads and all(secret not in payload for payload in payloads)
    assert secret.encode() not in TelemetryStore().db_path.read_bytes()


@pytest.mark.parametrize(
    "options,scope",
    [
        ({}, "full"),
        ({"lines": "2"}, "lines"),
        ({"query": "needle"}, "query"),
        ({"query": "needle", "semantic": True}, "query"),
    ],
)
def test_mcp_retrieve_records_scope_without_new_snapshots(monkeypatch, options, scope):
    monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", "1")
    cache = ContextCache()
    ref = cache.store("original\nneedle here\n")
    response = call("usagetrim_retrieve", {"ref_id": ref, "max_tokens": 1000, **options})
    assert not response["isError"]
    assert "needle here" in response["content"][0]["text"]
    [(_, details)] = recorded()
    assert details.recovery_ref == ref and details.recovery_scope == scope
    assert details.before_ref is None and details.after_ref is None
    with sqlite3.connect(cache.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM output_cache").fetchone()[0] == 1


@pytest.mark.parametrize("text", ["", "Error: valid stored diagnostic", "Error: " + "x" * 1000])
def test_mcp_retrieve_empty_or_error_prefixed_payload_is_not_a_tool_error(text):
    ref = ContextCache().store(text)
    response = call("usagetrim_retrieve", {"ref_id": ref, "max_tokens": 80})
    assert response == {"content": [{"type": "text", "text": text}], "isError": False}
    [(_, details)] = recorded()
    assert details.recovery_ref == ref and details.recovery_scope == "full"
    # The per-request flag must not leak into unrelated tools or later requests.
    assert server._RETRIEVE_ERROR.get() is None


@pytest.mark.parametrize("options", [{"query": "absentword"}, {"lines": "99"}, {"lines": "bad"}])
def test_mcp_retrieve_does_not_claim_empty_or_invalid_recovery(options):
    ref = ContextCache().store("original\n")
    response = call("usagetrim_retrieve", {"ref_id": ref, "max_tokens": 1000, **options})
    for _, details in recorded():
        assert details is None or details.recovery_ref is None
    assert response["isError"] == (options.get("lines") == "bad")


def test_mcp_missing_ref_is_still_an_error():
    response = call("usagetrim_retrieve", {"ref_id": "tc_0000000000000000"})
    assert response["isError"] and "not found" in response["content"][0]["text"]
    assert recorded() == []


def test_damaged_ccr_payload_is_not_recorded_as_recovered():
    cache = ContextCache()
    ref = cache.store("original")
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute("UPDATE output_cache SET content=? WHERE ref_id=?", (b"ZLIBbroken", ref))
    response = call("usagetrim_retrieve", {"ref_id": ref})
    assert response["isError"]
    assert recorded() == []
    assert server._RETRIEVE_ERROR.get() is None


def test_mcp_semantic_omission_notice_is_not_recovered_content():
    ref = ContextCache().store("needle " + "x" * 2000)
    response = call(
        "usagetrim_retrieve",
        {"ref_id": ref, "query": "needle", "semantic": True, "max_tokens": 100},
    )
    assert "Omitted" in response["content"][0]["text"]
    assert all(details is None or details.recovery_ref is None for _, details in recorded())


def test_mcp_recompacted_full_retrieval_is_not_claimed_complete():
    ref = ContextCache().store("diagnostic " * 500)
    response = call("usagetrim_retrieve", {"ref_id": ref, "max_tokens": 100})
    assert response["content"][0]["text"] != "diagnostic " * 500
    assert all(details is None or details.recovery_scope != "full" for _, details in recorded())


def test_rtk_retrieval_is_one_rtk_event_without_session_counter_changes():
    ref = ContextCache().store("rtk original", source="rtk")
    assert server.handle_usagetrim_retrieve({"ref_id": ref}) == "rtk original"
    [(event, details)] = recorded()
    assert event["engine"] == "rtk" and details.recovery_ref == ref
    assert server._SESSION_SAVED_OPENAI == 0


def test_recompacting_rtk_retrieval_does_not_change_measurement_owner():
    ref = ContextCache().store("diagnostic " * 500, source="rtk")
    output = server.handle_usagetrim_retrieve({"ref_id": ref, "max_tokens": 100})
    assert output != "diagnostic " * 500
    [(event, details)] = recorded()
    assert event["engine"] == "rtk"
    assert details is None or details.recovery_scope != "full"
    assert server._SESSION_SAVED_OPENAI == 0


@pytest.mark.parametrize("failure", ["telemetry", "ccr"])
def test_mcp_telemetry_and_ccr_failures_do_not_change_success(monkeypatch, failure):
    monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", "1")

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("private error detail")

    if failure == "telemetry":
        monkeypatch.setattr(server, "record_text", unavailable)
    else:
        monkeypatch.setattr(ContextCache, "store", unavailable)
    response = call("usagetrim_exec", {"command": python_command("print('ok')")})
    assert response == {
        "content": [{"type": "text", "text": "ok\n\n[exit code: 0]"}],
        "isError": False,
    }
    if failure == "ccr":
        [(_, details)] = recorded()
        assert details.child_returncode == 0 and details.snapshot_state == "unavailable"


def hook_event(exit_code=None):
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest -vv"},
        "tool_response": {
            "stdout": "tests/test_api.py::test_ok PASSED [100%]\n" * 80,
            "stderr": "",
            "exitCode": exit_code,
        },
    }


@pytest.mark.parametrize("exit_code", [None, True, "0", -1, 0, 1])
def test_hook_metadata_remains_prepared_and_snapshots_disabled(monkeypatch, exit_code):
    monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", "1")
    response = native_hooks.claude_post_tool_use(hook_event(exit_code))
    assert response["hookSpecificOutput"]["updatedToolOutput"]["exitCode"] == exit_code
    [(event, details)] = recorded()
    assert event["delivery"] == "prepared"
    assert TelemetryStore().get_stats().total_runs == 0
    assert details.before_ref is None and details.after_ref is None
    expected = exit_code if type(exit_code) is int and exit_code >= 0 else None
    assert details.child_returncode == expected
    assert details.termination_reason == ("exited" if expected is not None else None)


def test_hook_telemetry_failure_does_not_discard_prepared_response(monkeypatch):
    event = hook_event(1)
    baseline = native_hooks.claude_post_tool_use(event)

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("private exception")

    monkeypatch.setattr(native_hooks, "record_text", unavailable)
    assert native_hooks.claude_post_tool_use(event) == baseline


def test_native_mcp_hook_does_not_infer_execution_from_is_error(monkeypatch):
    monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", "1")
    original = {"content": [{"type": "text", "text": "original text"}], "isError": False}
    prepared = {"content": [{"type": "text", "text": "compact text"}], "isError": False}
    monkeypatch.setattr(native_hooks, "compact_mcp_response", lambda response: prepared)
    response = native_hooks.claude_post_tool_use(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__example__query",
            "tool_response": original,
        }
    )
    assert response["hookSpecificOutput"]["updatedToolOutput"] == prepared
    [(event, details)] = recorded()
    assert event["delivery"] == "prepared"
    assert details.child_returncode is None and details.termination_reason is None
    assert details.before_ref is None and details.after_ref is None


def test_paused_hook_does_not_prepare_or_record_response(monkeypatch):
    monkeypatch.setattr(native_hooks, "paused", lambda: True)
    assert native_hooks.claude_post_tool_use(hook_event(0)) == {}
    assert recorded() == []


def test_mcp_retrieve_error_marker_does_not_leak_between_requests():
    missing = call("usagetrim_retrieve", {"ref_id": "tc_0000000000000000"})
    assert missing["isError"]
    ref = ContextCache().store("Error: real diagnostic")
    valid = call("usagetrim_retrieve", {"ref_id": ref})
    assert not valid["isError"]
    assert server._RETRIEVE_ERROR.get() is None
