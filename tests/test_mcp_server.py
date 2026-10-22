import io
import json
import re
import sys

import pytest
from typer.testing import CliRunner

from usagetrim.core.cache import ContextCache
from usagetrim.core.telemetry import TelemetryStore
from usagetrim.mcp import server
from usagetrim.mcp.server import (
    handle_usagetrim_diff,
    handle_usagetrim_exec,
    handle_usagetrim_gain,
    handle_usagetrim_json,
    handle_usagetrim_read,
    handle_usagetrim_retrieve,
    handle_usagetrim_stats,
    handle_usagetrim_tree,
    run_mcp_stdio_server,
)
from usagetrim.metrics.tokenizer import count_tokens


def test_coding_profile_keeps_coding_and_recovery_tools_without_hidden_dispatch(monkeypatch):
    listed = server._respond({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, profile="coding")
    assert {tool["name"] for tool in listed["result"]["tools"]} == server.CODING_TOOLS
    assert len(server.TOOLS_DEFINITIONS) > len(server.CODING_TOOLS)
    monkeypatch.setattr(
        server, "handle_usagetrim_clip", lambda _: pytest.fail("Hidden tool executed")
    )
    response = server._respond(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "usagetrim_clip", "arguments": {"text": "input"}},
        },
        profile="coding",
    )
    assert response["error"]["code"] == -32602
    # Coding tools use compact descriptions, but must accept exactly the same arguments.
    for name in {"usagetrim_code", "usagetrim_edit_symbol", "usagetrim_read", "usagetrim_retrieve"}:
        compact = next(tool for tool in listed["result"]["tools"] if tool["name"] == name)
        full = next(tool for tool in server.TOOLS_DEFINITIONS if tool["name"] == name)
        assert (
            compact["inputSchema"]["properties"].keys() == full["inputSchema"]["properties"].keys()
        )
        assert compact["inputSchema"].get("required") == full["inputSchema"].get("required")
        assert compact.get("annotations") == full.get("annotations")
        assert len(compact["description"]) <= len(full["description"])


def test_profile_stdio_and_environment_selection(monkeypatch):
    monkeypatch.delenv("USAGETRIM_MCP_PROFILE", raising=False)
    from usagetrim.cli import app

    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
    runner = CliRunner()
    for args, env, expected in (
        ([], {}, 9),
        (["--profile", "coding"], {}, 9),
        ([], {"USAGETRIM_MCP_PROFILE": "coding"}, 9),
        (["--profile", "desktop"], {}, 11),
        ([], {"USAGETRIM_MCP_PROFILE": "desktop"}, 11),
        (["--profile", "full"], {"USAGETRIM_MCP_PROFILE": "coding"}, len(server.TOOLS_DEFINITIONS)),
    ):
        result = runner.invoke(app, ["mcp", *args], input=request, env=env)
        assert result.exit_code == 0, result.output
        assert len(json.loads(result.stdout)["result"]["tools"]) == expected
    invalid = runner.invoke(app, ["mcp", "--profile", "typo"], input=request)
    assert invalid.exit_code != 0
    assert '"result"' not in invalid.stdout


def test_coding_profile_reduces_schema_text_and_does_not_advertise_hidden_tools():
    full = count_tokens(json.dumps(server.tool_definitions())).openai
    coding = count_tokens(json.dumps(server.tool_definitions("coding"))).openai
    assert coding < full * 0.75
    assert "usagetrim_pack" not in server.server_instructions("coding")
    assert "usagetrim_pack" in server.server_instructions("full")


def test_pack_schema_exposes_modes_focus_and_format_only_in_full_profile():
    tool = next(
        item for item in server.tool_definitions("full") if item["name"] == "usagetrim_pack"
    )
    props = tool["inputSchema"]["properties"]
    assert props["mode"]["enum"] == ["full", "skeleton", "hybrid"]
    assert props["format"]["enum"] == ["xml", "markdown"]
    assert props["focus"]["items"]["type"] == "string"
    assert "strip_comments" in props
    assert "usagetrim_pack" not in {item["name"] for item in server.tool_definitions("coding")}


def test_mcp_pack_hybrid_xml_and_invalid_arguments(tmp_path, monkeypatch):
    import xml.etree.ElementTree as ET

    monkeypatch.setenv("USAGETRIM_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "focus.py").write_text("def focus():\n" + "    return 1\n" * 80)
    (tmp_path / "other.py").write_text("def other():\n" + "    return 2\n" * 80)
    output = server.handle_usagetrim_pack(
        {"root": str(tmp_path), "mode": "hybrid", "focus": ["focus.py"], "budget": 500}
    )
    document = ET.fromstring(output)
    assert document.find("file_summary").attrib["mode"] == "hybrid"
    assert count_tokens(output).openai <= 500
    markdown = server.handle_usagetrim_pack(
        {"root": str(tmp_path), "mode": "full", "format": "markdown", "budget": 2000}
    )
    assert "## Directory Structure" in markdown
    assert "### File: focus.py (full)" in markdown
    for arguments in (
        {"mode": "unknown"},
        {"mode": []},
        {"format": "json"},
        {"focus": "focus.py"},
        {"mode": "full", "skeleton": True},
    ):
        with pytest.raises(ValueError):
            server.handle_usagetrim_pack({"root": str(tmp_path), **arguments})


def test_desktop_profile_reduces_schema_text_and_sets_instructions():
    full = count_tokens(json.dumps(server.tool_definitions())).openai
    desktop = count_tokens(json.dumps(server.tool_definitions("desktop"))).openai
    assert desktop < full * 0.70
    assert len(server.tool_definitions("desktop")) == 11
    instructions = server.server_instructions("desktop")
    assert "Desktop Profile" in instructions
    assert "Claude Desktop & Codex Desktop" in instructions


def test_handle_usagetrim_exec():
    res = handle_usagetrim_exec({"command": "echo 'Hello usagetrim!'"})
    assert "Hello usagetrim!" in res
    assert "exit code: 0" in res


def test_handle_usagetrim_exec_budget():
    res = handle_usagetrim_exec({"command": "printf '%10000s' x; exit 7", "budget": 100})
    assert count_tokens(res).claude <= 100
    assert "exit code: 7" in res
    ref = re.search(r"tc_[a-f0-9]+", res).group()
    assert ContextCache().retrieve(ref).endswith("x")


def test_handle_usagetrim_read(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text("def foo():\n    return 42\n")
    res = handle_usagetrim_read({"path": str(f), "skeleton": True})
    assert "def foo():" in res
    assert "..." in res


def test_handle_usagetrim_read_if_modified_since(tmp_path):
    import hashlib

    f = tmp_path / "data.py"
    f.write_text("x = 100\n")
    content_hash = hashlib.sha256(f.read_bytes()).hexdigest()

    # When hash matches, returns 304 Not Modified notice
    cached_res = handle_usagetrim_read({"path": str(f), "if_modified_since_hash": content_hash})
    assert "304 Not Modified" in cached_res
    assert "data.py" in cached_res

    # When hash does not match, returns fresh file content
    diff_hash = "0" * 64
    fresh_res = handle_usagetrim_read({"path": str(f), "if_modified_since_hash": diff_hash})
    assert "304 Not Modified" not in fresh_res
    assert "x = 100" in fresh_res


def test_handle_usagetrim_read_include_hash_and_strip_comments(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("# verbose commentary\nval = 42\n# trailing note\n")

    # With include_hash
    hash_res = handle_usagetrim_read({"path": str(f), "include_hash": True})
    assert "# [sha256:" in hash_res
    assert "val = 42" in hash_res

    # With strip_comments
    clean_res = handle_usagetrim_read({"path": str(f), "strip_comments": True})
    assert "# verbose commentary" not in clean_res
    assert "val = 42" in clean_res


def test_handle_usagetrim_read_auto_skeleton(tmp_path):
    f = tmp_path / "large_module.py"
    lines = ["import os", "import sys", "class Worker:"]
    for i in range(15):
        lines.append(f"    def task_{i}(self, x: int) -> int:")
        lines.append(f'        """Docstring for task {i}."""')
        lines.append("        val = x * 2")
        lines.append("        res = val + 10")
        lines.append("        return res\n")
    f.write_text("\n".join(lines), encoding="utf-8")

    # Regular read with low budget
    raw_res = handle_usagetrim_read({"path": str(f), "budget": 80, "auto_skeleton": False})
    assert "lines omitted" in raw_res  # truncated

    # Auto skeleton read
    skel_res = handle_usagetrim_read({"path": str(f), "budget": 300, "auto_skeleton": True})
    assert "structural outline" in skel_res
    assert "class Worker:" in skel_res
    assert "def task_0" in skel_res
    assert "def task_14" in skel_res


def test_handle_usagetrim_read_session_dedups_identical_content(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGETRIM_CACHE_DIR", str(tmp_path))
    path = tmp_path / "shared.py"
    body = "class Shared:\n" + ("    member = 1\n" * 100)
    path.write_text(body)
    first = handle_usagetrim_read({"path": str(path)})
    second = handle_usagetrim_read({"path": str(path)})
    assert "class Shared:" in first
    assert "class Shared:" not in second
    assert "identical" in second.lower() or "retrieve" in second.lower()
    assert "tc_" in second
    assert count_tokens(second).openai < count_tokens(first).openai // 5
    ref = re.search(r"tc_[a-f0-9]+", second).group()
    assert ContextCache().retrieve(ref) == body


def test_handle_usagetrim_retrieve():
    res = handle_usagetrim_retrieve({"ref_id": "tc_nonexistent"})
    assert "not found" in res


def test_handle_usagetrim_diff():
    res = handle_usagetrim_diff({})
    assert isinstance(res, str)

    res_ignore = handle_usagetrim_diff({"ignore_patterns": [r"\.tmp$"]})
    assert isinstance(res_ignore, str)

    res_budget = handle_usagetrim_diff({"budget": 1500})
    assert isinstance(res_budget, str)


def test_handle_usagetrim_tree(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.py").write_text("print(1)")
    res = handle_usagetrim_tree({"path": str(tmp_path), "max_depth": 2})
    assert "a.py" in res


def test_handle_usagetrim_json():
    raw = '[{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]'
    res = handle_usagetrim_json({"json_str": raw, "max_items": 2})
    assert "omitted by usagetrim" in res


def test_json_small_lossy_preview_still_has_recoverable_original():
    raw = '[{"id":1},{"id":2},{"unique_schema":true}]'
    output = handle_usagetrim_json({"json_str": raw, "max_items": 2})
    ref = re.search(r"tc_[a-f0-9]+", output).group()
    assert ContextCache().retrieve(ref) == raw


def test_json_protocol_bounds_redacts_and_accounts_for_returned_text(tmp_path, monkeypatch):
    raw = json.dumps(
        {
            "token": "ghp_" + "a" * 36,
            "entries": [{"id": i, "details": "diagnostic " * 50} for i in range(20)],
        }
    )
    path = tmp_path / "response.json"
    path.write_text(raw)
    monkeypatch.setattr(server, "_SESSION_SAVED_OPENAI", 0)
    response = server._respond(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "usagetrim_json",
                "arguments": {"path": str(path), "max_tokens": 100},
            },
        }
    )
    assert not response["result"]["isError"]
    output = response["result"]["content"][0]["text"]
    assert count_tokens(output).claude <= 100
    ref = re.search(r"tc_[a-f0-9]+", output).group()
    recovered = ContextCache().retrieve(ref)
    assert "ghp_" + "a" * 36 not in output + recovered
    assert '"id": 19' in recovered
    assert server._SESSION_SAVED_OPENAI == count_tokens(raw).openai - count_tokens(output).openai


def test_tree_protocol_budget_and_recovery_do_not_claim_file_content_savings(tmp_path, monkeypatch):
    for i in range(30):
        (tmp_path / f"source_module_{i:02d}.py").write_text("print('hello')\n")
    root, _ = server.scan_directory(tmp_path, max_depth=2)
    raw = server.format_tree_as_text(root, root.tokens)
    monkeypatch.setattr(server, "_SESSION_SAVED_OPENAI", 0)
    response = server._respond(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "usagetrim_tree",
                "arguments": {
                    "path": str(tmp_path),
                    "max_depth": 2,
                    "max_tokens": 100,
                },
            },
        }
    )
    assert not response["result"]["isError"]
    output = response["result"]["content"][0]["text"]
    assert count_tokens(output).claude <= 100
    ref = re.search(r"tc_[a-f0-9]+", output).group()
    assert ContextCache().retrieve(ref) == raw
    assert server._SESSION_SAVED_OPENAI == count_tokens(raw).openai - count_tokens(output).openai


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("usagetrim_json", {"json_str": "[1,2,3]", "max_items": -1}),
        ("usagetrim_json", {"json_str": "[1,2,3]", "max_tokens": 0}),
        ("usagetrim_tree", {"max_depth": -1}),
        ("usagetrim_tree", {"max_depth": True}),
        ("usagetrim_tree", {"max_tokens": 0}),
    ],
)
def test_new_tools_reject_invalid_limits(name, arguments):
    response = server._respond(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert response["result"]["isError"]


def test_handle_usagetrim_stats():
    stats = handle_usagetrim_stats()
    assert "usagetrim Session Savings:" in stats
    assert "Estimated net text reduction" in stats


def test_coding_profile_includes_gain_tool():
    listed = server._respond({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, profile="coding")
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert "usagetrim_gain" in names
    assert "usagetrim_gain" in server.CODING_TOOLS
    gain_def = next(t for t in listed["result"]["tools"] if t["name"] == "usagetrim_gain")
    assert (
        "billing" in gain_def["description"].lower() or "quota" in gain_def["description"].lower()
    )
    assert gain_def["annotations"]["readOnlyHint"] is True


def test_handle_usagetrim_gain_json(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGETRIM_CACHE_DIR", str(tmp_path))
    store = TelemetryStore(db_path=tmp_path / "telemetry.db")
    store.record(1000, 200, 1000, 200, 1000, 200, operation="exec:pytest")
    store.record(400, 400, 400, 400, 400, 400, operation="exec:echo")

    output = handle_usagetrim_gain({"history": True, "limit": 10})
    data = json.loads(output)
    assert data["total_events"] == 2
    assert data["saved_openai"] == 800
    assert any(row["operation"] == "exec:pytest" for row in data["by_operation"])
    assert any(row["passthrough"] for row in data["passthrough"])
    assert "billing" in data["measurement"].lower() or "quota" in data["measurement"].lower()
    assert len(data["history"]) == 2

    via_rpc = server._respond(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "usagetrim_gain", "arguments": {"passthrough": True}},
        },
        profile="coding",
    )
    assert via_rpc["result"]["isError"] is False
    rpc_data = json.loads(via_rpc["result"]["content"][0]["text"])
    assert any(row["operation"] == "exec:echo" for row in rpc_data["passthrough"])


def test_mcp_stdio_protocol_loop(monkeypatch):
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "usagetrim_stats", "arguments": {}},
        },
        {"jsonrpc": "2.0", "id": 4, "method": "ping", "params": {}},
    ]
    input_stream = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
    output_stream = io.StringIO()

    monkeypatch.setattr(sys, "stdin", input_stream)
    monkeypatch.setattr(sys, "stdout", output_stream)

    run_mcp_stdio_server()

    responses = [json.loads(line) for line in output_stream.getvalue().splitlines() if line.strip()]
    assert len(responses) == 4
    assert responses[0]["id"] == 1
    assert responses[0]["result"]["serverInfo"]["name"] == "usagetrim"
    assert responses[1]["id"] == 2
    assert {tool["name"] for tool in responses[1]["result"]["tools"]} == {
        "usagetrim_context",
        "usagetrim_code",
        "usagetrim_edit_symbol",
        "usagetrim_exec",
        "usagetrim_read",
        "usagetrim_retrieve",
        "usagetrim_diff",
        "usagetrim_tree",
        "usagetrim_json",
        "usagetrim_clip",
        "usagetrim_pack",
        "usagetrim_distill",
        "usagetrim_table",
        "usagetrim_optimize",
        "usagetrim_stats",
        "usagetrim_gain",
    }
    assert responses[2]["id"] == 3
    assert "usagetrim Session Savings" in responses[2]["result"]["content"][0]["text"]


def test_mcp_read_bounds_single_huge_line_and_recovers(tmp_path):
    path = tmp_path / "huge.txt"
    raw = "payload with details " * 4000
    path.write_text(raw)
    output = handle_usagetrim_read({"path": str(path), "max_tokens": 100})
    assert count_tokens(output).claude <= 100
    import re

    ref = re.search(r"tc_[a-f0-9]+", output).group()
    assert ContextCache().retrieve(ref) == raw


def test_exec_budget_includes_exit_status_and_recovers_deduplication():
    output = handle_usagetrim_exec(
        {
            "command": "printf 'same line\\nsame line\\nsame line\\nsame line\\n'; exit 7",
            "max_tokens": 64,
        }
    )
    assert count_tokens(output).claude <= 64
    assert "exit code: 7" in output
    assert "Ref: tc_" in output


def test_exec_explicit_cwd(tmp_path):
    (tmp_path / "project.txt").write_text("correct project")
    output = handle_usagetrim_exec({"command": "cat project.txt", "cwd": str(tmp_path)})
    assert "correct project" in output


def test_exec_default_preserves_unknown_long_output_and_full_diagnostics(monkeypatch):
    from subprocess import CompletedProcess

    raw = "".join(f"unique record {i}\n" for i in range(600))
    raw += (
        "Traceback (most recent call last):\n" + "stack frame\n" * 300 + "AssertionError: failure\n"
    )
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **kw: CompletedProcess(a, 9, raw))
    output = handle_usagetrim_exec({"command": "custom-command"})
    assert output == raw + "\n[exit code: 9]"


def test_exec_timeout_keeps_all_partial_diagnostics(monkeypatch):
    from subprocess import TimeoutExpired

    raw = ("Error: still waiting\n" * 300).encode()

    def timeout(*args, **kwargs):
        raise TimeoutExpired("custom-command", 120, output=raw)

    monkeypatch.setattr(server.subprocess, "run", timeout)
    output = handle_usagetrim_exec({"command": "custom-command"})
    assert output.startswith(raw.decode())
    assert "timed out" in output


def test_diff_outside_repository_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="git diff failed"):
        handle_usagetrim_diff({"cwd": str(tmp_path)})


def test_short_output_overhead_and_retrieval_are_counted(monkeypatch):
    for name in ("_SESSION_SAVED_CLAUDE", "_SESSION_SAVED_OPENAI", "_SESSION_SAVED_GEMINI"):
        monkeypatch.setattr(server, name, 0)
    output = handle_usagetrim_exec({"command": "printf hi"})
    expected = count_tokens("hi").claude - count_tokens(output).claude
    assert server._SESSION_SAVED_CLAUDE == expected < 0
    ref = ContextCache().store("previous output")
    retrieved = handle_usagetrim_retrieve({"ref_id": ref})
    assert server._SESSION_SAVED_CLAUDE == expected - count_tokens(retrieved).claude


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"command": 42},
        {"command": "echo hi", "max_tokens": 1},
        {"command": "echo hi", "budget": 1},
        {"command": "echo hi", "budget": True},
        {"command": "echo hi", "max_lines": True},
    ],
)
def test_invalid_call_does_not_run_command(arguments, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid arguments must not execute a command")

    monkeypatch.setattr(server.subprocess, "run", forbidden)
    response = server._respond(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "usagetrim_exec", "arguments": arguments},
        }
    )
    assert response["result"]["isError"]
    assert server._respond({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}


def test_protocol_survives_bad_messages(monkeypatch):
    messages = [
        "{",
        "[]",
        '{"jsonrpc":"2.0","method":"notifications/initialized"}',
        '{"jsonrpc":"2.0","id":1,"method":"unknown"}',
        '{"jsonrpc":"2.0","id":2,"method":"ping"}',
    ]
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(messages)))
    monkeypatch.setattr(sys, "stdout", output)
    run_mcp_stdio_server()
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [r.get("error", {}).get("code") for r in responses] == [-32700, -32600, -32601, None]
    assert responses[-1]["id"] == 2


def test_mcp_edit_symbol_preview_and_apply(tmp_path):
    f = tmp_path / "calc.py"
    f.write_text("class Calculator:\n    def add(self, a, b):\n        return a + b\n")
    read_out = server.handle_usagetrim_read({"path": str(f), "symbol": "Calculator.add"})
    assert "sha256=" in read_out
    hash_val = read_out.split("sha256=")[1].strip().split()[0]

    # Preview diff
    preview = server.handle_usagetrim_edit_symbol(
        {
            "path": str(f),
            "selector": "Calculator.add",
            "replacement": "    def add(self, a, b):\n        # updated\n        return a + b",
            "expected_hash": hash_val,
            "apply": False,
        }
    )
    assert "# Preview only" in preview
    assert "+        # updated" in preview
    assert "# updated" not in f.read_text()

    # Apply diff
    applied = server.handle_usagetrim_edit_symbol(
        {
            "path": str(f),
            "selector": "Calculator.add",
            "replacement": "    def add(self, a, b):\n        # updated\n        return a + b",
            "expected_hash": hash_val,
            "apply": True,
        }
    )
    assert "Updated calc.py:" in applied
    assert "# updated" in f.read_text()


def test_mcp_clip():
    noisy = "LOG: starting job\n" * 40 + "LOG: done\n"
    res = server._respond(
        {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "tools/call",
            "params": {
                "name": "usagetrim_clip",
                "arguments": {"text": noisy, "budget": 500},
            },
        }
    )
    assert not res["result"]["isError"]
    text = res["result"]["content"][0]["text"]
    assert "preceding line repeated" in text
    assert "LOG: done" in text


def test_mcp_pack(tmp_path):
    import xml.etree.ElementTree as ET

    (tmp_path / "main.py").write_text("def run():\n    return 42\n")
    res = server._respond(
        {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {
                "name": "usagetrim_pack",
                "arguments": {"root": str(tmp_path), "budget": 1000},
            },
        }
    )
    assert not res["result"]["isError"]
    text = res["result"]["content"][0]["text"]
    document = ET.fromstring(text)
    assert document.find("file_summary").attrib["mode"] == "skeleton"
    assert document.find("./files/file").attrib["path"] == "main.py"


def test_mcp_distill():
    transcript = "User: Build feature in app.py\n\nAssistant: We decided on SQLite.\n"
    res = server._respond(
        {
            "jsonrpc": "2.0",
            "id": 102,
            "method": "tools/call",
            "params": {
                "name": "usagetrim_distill",
                "arguments": {"transcript": transcript, "budget": 500},
            },
        }
    )
    assert not res["result"]["isError"]
    text = res["result"]["content"][0]["text"]
    assert "Distilled Conversation Context" in text
    assert "app.py" in text


def test_mcp_table():
    data = json.dumps([{"id": 1, "status": "ok"}, {"id": 2, "status": "pending"}])
    res = server._respond(
        {
            "jsonrpc": "2.0",
            "id": 103,
            "method": "tools/call",
            "params": {
                "name": "usagetrim_table",
                "arguments": {"data": data, "budget": 500, "format": "toon"},
            },
        }
    )
    assert not res["result"]["isError"]
    text = res["result"]["content"][0]["text"]
    assert "[id | status]" in text
    assert "1 | ok" in text


def test_mcp_optimize():
    prompt = """
Please analyze this user data:
```json
[
  {"id": 1, "name": "Alice", "role": "admin"},
  {"id": 2, "name": "Bob", "role": "user"}
]
```
Let me know if you see any issues.
"""
    res = server._respond(
        {
            "jsonrpc": "2.0",
            "id": 104,
            "method": "tools/call",
            "params": {
                "name": "usagetrim_optimize",
                "arguments": {"content": prompt, "budget": 1000},
            },
        }
    )
    assert not res["result"]["isError"]
    text = res["result"]["content"][0]["text"]
    assert "Please analyze this user data:" in text
    assert "[id | name | role]" in text
    assert "Alice" in text


def test_mcp_registry_metadata_and_output_schemas():
    import json
    from pathlib import Path

    from usagetrim.mcp.server import tool_definitions

    root = Path(__file__).resolve().parents[1]

    # 1. smithery.yaml verification
    smithery_file = root / "smithery.yaml"
    assert smithery_file.is_file()
    content = smithery_file.read_text(encoding="utf-8")
    assert "startCommand:" in content
    assert "usagetrim" in content

    # 2. glama.json verification
    glama_file = root / "glama.json"
    assert glama_file.is_file()
    glama_data = json.loads(glama_file.read_text(encoding="utf-8"))
    assert glama_data["name"] == "usagetrim"
    assert "command" in glama_data
    assert "tools" in glama_data
    assert len(glama_data["tools"]) > 0

    # 3. OutputSchema presence on all tool definitions
    tools = tool_definitions("full")
    assert len(tools) > 0
    for tool in tools:
        assert "name" in tool
        assert "inputSchema" in tool
        assert "outputSchema" in tool, f"Missing outputSchema on tool: {tool['name']}"
        out_schema = tool["outputSchema"]
        assert out_schema.get("type") == "object"
        assert "content" in out_schema.get("properties", {})


@pytest.fixture
def structural_mcp_repo(tmp_path):
    import subprocess

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            cwd=tmp_path,
        ).decode("utf-8")

    git("init", "-q", "--initial-branch=main")
    source = """def check(value):
    if value > 5:
        return value * 2
    return value


def summarize(values):
    total = 0
    for value in values:
        total += check(value)
    return total


def report(values):
    return {"count": len(values), "total": summarize(values)}
"""
    (tmp_path / "formatted.py").write_text(source)
    (tmp_path / "other.py").write_text("value = 1\n")
    git("add", ".")
    git("commit", "-qm", "Initial sources")
    formatted = (
        "\n".join(
            "  " * (len(line) - len(line.lstrip())) + line.lstrip() for line in source.splitlines()
        )
        + "\n"
    )
    (tmp_path / "formatted.py").write_text(formatted)
    return tmp_path, git


@pytest.mark.parametrize("staged", [False, True])
def test_mcp_structural_diff_receipt_recovers_exact_raw_diff(structural_mcp_repo, staged):
    root, git = structural_mcp_repo
    if staged:
        git("add", "formatted.py")
    raw = git(
        "diff", "--no-ext-diff", "--no-textconv", "--no-color", *(["--cached"] if staged else [])
    )
    output = handle_usagetrim_diff({"cwd": str(root), "staged": staged, "max_tokens": 4000})
    assert "formatting-only" in output
    assert "+        return value" not in output
    ref = re.search(r"tc_[a-f0-9]+", output)
    assert ref is not None
    recovered = ContextCache().retrieve(ref.group())
    assert recovered == raw
    assert "-    if value > 5:" in recovered and "+        if value > 5:" in recovered
    assert "formatting-only" not in recovered


@pytest.mark.parametrize("staged", [False, True])
def test_mcp_genuine_diff_remains_visible(structural_mcp_repo, staged):
    root, git = structural_mcp_repo
    path = root / "formatted.py"
    path.write_text(path.read_text().replace("value > 5", "value >= 5"))
    if staged:
        git("add", "formatted.py")
    output = handle_usagetrim_diff({"cwd": str(root), "staged": staged, "max_tokens": 10000})
    assert "formatting-only" not in output
    assert "+        if value >= 5:" in output


def test_mcp_structural_diff_path_scope_and_cwd(structural_mcp_repo, monkeypatch):
    root, _ = structural_mcp_repo
    (root / "other.py").write_text("value = 2\n")
    elsewhere = root / "sub"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    output = handle_usagetrim_diff({"cwd": str(root), "path": "formatted.py", "max_tokens": 4000})
    assert "formatting-only" in output
    assert "other.py" not in output
    assert "a/formatted.py" in output


def test_mcp_structural_diff_redacts_output_and_recovered_raw(structural_mcp_repo):
    root, git = structural_mcp_repo
    secret = "ghp_" + "a" * 36
    path = root / "formatted.py"
    source = path.read_text() + f'\ncredential = "{secret}"\n'
    path.write_text(source)
    git("add", "formatted.py")
    git("commit", "-qm", "Add controlled redaction fixture")
    path.write_text(source.replace("credential = ", "credential="))
    output = handle_usagetrim_diff({"cwd": str(root), "max_tokens": 4000})
    assert "formatting-only" in output and secret not in output
    ref = re.search(r"tc_[a-f0-9]+", output)
    assert ref is not None
    recovered = ContextCache().retrieve(ref.group())
    assert secret not in recovered and "REDACTED" in recovered
    assert '-credential = "' in recovered and '+credential="' in recovered


def test_mcp_diff_pause_skips_classifier_budget_and_recovery(structural_mcp_repo, monkeypatch):
    root, git = structural_mcp_repo
    raw = git("diff", "--no-ext-diff", "--no-textconv", "--no-color")
    before = ContextCache().get_stats()["count"]

    def forbidden(*args, **kwargs):
        raise AssertionError("Pause must bypass structural classification and slimming")

    monkeypatch.setattr(server, "paused", lambda: True)
    monkeypatch.setattr(server, "classify_git_diff", forbidden)
    monkeypatch.setattr(server, "budgeted_diff_slice", forbidden)
    monkeypatch.setattr(server, "_compress", forbidden)
    output = handle_usagetrim_diff({"cwd": str(root), "max_tokens": 64})
    assert output == raw
    assert ContextCache().get_stats()["count"] == before


def test_mcp_structural_diff_with_process_cwd_and_ignore(structural_mcp_repo, monkeypatch):
    root, _ = structural_mcp_repo
    monkeypatch.chdir(root)
    output = handle_usagetrim_diff({"max_tokens": 4000})
    assert "formatting-only" in output
    ignored = handle_usagetrim_diff({"ignore_patterns": [r"formatted\.py$"], "max_tokens": 4000})
    assert "formatting-only" not in ignored and "lockfile/generated" in ignored


def test_mcp_structural_diff_empty_keeps_message(structural_mcp_repo, monkeypatch):
    root, git = structural_mcp_repo
    git("add", "formatted.py")
    monkeypatch.setattr(
        server, "classify_git_diff", lambda *a, **kw: pytest.fail("Empty diff must skip classifier")
    )
    assert handle_usagetrim_diff({"cwd": str(root)}) == "No git changes detected."
