import json
import os
import sys

import pytest
from typer.testing import CliRunner

from usagetrim.cli import app
from usagetrim.core.cache import ContextCache

runner = CliRunner()


def test_cli_help():
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    assert "Context compression engine" in res.output


def test_cli_demo():
    res = runner.invoke(app, ["demo"])
    assert res.exit_code == 0
    assert "verify your installation" in res.output
    assert "PASS" in res.output
    assert "No model calls" in res.output
    assert "cargo test" in res.output
    assert "go test" in res.output
    assert "nextest" in res.output
    assert "pytest noisy failure" in res.output
    assert "pyright" in res.output
    assert "vitest" in res.output
    assert "mypy" in res.output
    assert "npm test" in res.output


def test_demo_measures_recovery_and_preserves_user_cache(tmp_path, monkeypatch):
    cache_path = os.environ["USAGETRIM_CACHE_DIR"]
    cache = ContextCache()
    ref = cache.store("existing user output", source="test")
    before = cache.get_stats()["count"]
    empty_directory = tmp_path / "empty-project"
    empty_directory.mkdir()
    monkeypatch.chdir(empty_directory)
    res = runner.invoke(app, ["demo", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["passed"] and all(data["checks"].values())
    assert data["model_calls"] == 0
    assert 0 < data["output_tokens"] < data["raw_tokens"]
    assert (
        data["specialized"]["git_diff"]["output_tokens"]
        < data["specialized"]["git_diff"]["raw_tokens"]
    )
    assert data["specialized"]["ruff"]["output_tokens"] < data["specialized"]["ruff"]["raw_tokens"]
    assert (
        data["specialized"]["docker_build"]["output_tokens"]
        < data["specialized"]["docker_build"]["raw_tokens"]
    )
    assert data["specialized"]["tsc"]["output_tokens"] < data["specialized"]["tsc"]["raw_tokens"]
    assert (
        data["specialized"]["eslint"]["output_tokens"] < data["specialized"]["eslint"]["raw_tokens"]
    )
    # Measured o200k_base counts — keep README in lockstep with usagetrim demo.
    assert data["specialized"]["mypy"] == {
        "raw_tokens": 522,
        "output_tokens": 259,
        "reduction_pct": 50.4,
    }
    assert data["specialized"]["npm_test"] == {
        "raw_tokens": 1588,
        "output_tokens": 156,
        "reduction_pct": 90.2,
    }
    assert data["specialized"]["cargo_test"] == {
        "raw_tokens": 3795,
        "output_tokens": 185,
        "reduction_pct": 95.1,
    }
    assert data["specialized"]["go_test"] == {
        "raw_tokens": 3281,
        "output_tokens": 64,
        "reduction_pct": 98.0,
    }
    assert data["specialized"]["nextest"] == {
        "raw_tokens": 464,
        "output_tokens": 46,
        "reduction_pct": 90.1,
    }
    assert data["specialized"]["pytest_noise"] == {
        "raw_tokens": 5111,
        "output_tokens": 349,
        "reduction_pct": 93.2,
    }
    assert data["specialized"]["pyright"] == {
        "raw_tokens": 4244,
        "output_tokens": 2302,
        "reduction_pct": 45.8,
    }
    assert data["specialized"]["vitest"] == {
        "raw_tokens": 3816,
        "output_tokens": 44,
        "reduction_pct": 98.8,
    }
    assert data["specialized"]["kubectl_describe"] == {
        "raw_tokens": 8127,
        "output_tokens": 595,
        "reduction_pct": 92.7,
    }
    assert data["specialized"]["terraform_plan"] == {
        "raw_tokens": 6409,
        "output_tokens": 276,
        "reduction_pct": 95.7,
    }
    assert data["checks"]["cargo_keeps_failure_drops_passes"]
    assert data["checks"]["go_keeps_failure_drops_passes"]
    assert data["checks"]["nextest_collapses_passes"]
    assert data["checks"]["pytest_noise_keeps_failure_drops_io"]
    assert data["checks"]["pyright_keeps_diagnostics_drops_frames"]
    assert data["checks"]["vitest_collapses_passing_runs"]
    assert data["checks"]["mypy_keeps_codes_drops_frames"]
    assert data["checks"]["npm_test_keeps_failure_drops_console"]
    assert data["checks"]["kubectl_keeps_crash_drops_annotations"]
    assert data["checks"]["terraform_keeps_plan_drops_refresh"]
    assert os.environ["USAGETRIM_CACHE_DIR"] == cache_path
    assert cache.get_stats()["count"] == before
    assert cache.retrieve(ref) == "existing user output"


def test_demo_fails_if_compaction_drops_diagnostics(monkeypatch):
    monkeypatch.setattr("usagetrim.cli.safe_compact_output", lambda text, **kw: "everything passed")
    res = runner.invoke(app, ["demo", "--json"])
    assert res.exit_code == 1
    data = json.loads(res.stdout)
    assert not data["passed"]
    assert not data["checks"]["complete_failure_tail_preserved"]
    assert not data["checks"]["original_recovered_exactly"]


def test_invalid_run_budget_is_rejected_before_execution(tmp_path):
    marker = tmp_path / "must-not-execute"
    script = f"from pathlib import Path; Path({str(marker)!r}).touch()"
    for budget in ("0", "-1"):
        res = runner.invoke(app, ["run", "--budget", budget, "--", sys.executable, "-c", script])
        assert res.exit_code == 2
        assert not marker.exists()


def test_cli_run():
    res = runner.invoke(app, ["run", "echo", "testing 1 2 3"])
    assert res.exit_code == 0
    assert "testing 1 2 3" in res.output


def test_run_preserves_literal_arguments_and_markup(tmp_path):
    unwanted = tmp_path / "should-not-exist"
    literal = f"[bold]two words[/bold] ; touch {unwanted} $(echo expanded)"
    res = runner.invoke(
        app, ["run", "--", sys.executable, "-c", "import sys; print(sys.argv[1])", literal]
    )
    assert res.exit_code == 0
    assert res.stdout == literal + "\n"
    assert not unwanted.exists()


def test_run_preserves_complete_failed_output_and_exit_status():
    output = (
        "Traceback (most recent call last):\n"
        + "detail\n" * 200
        + "AssertionError: original failure\n"
    )
    script = f"import sys; print({output!r}, end='', file=sys.stderr); sys.exit(7)"
    res = runner.invoke(app, ["run", "--", sys.executable, "-c", script])
    assert res.exit_code == 7
    assert res.stdout == output


def test_retrieve_preserves_original_format_and_charges_readback():
    from usagetrim.core.telemetry import TelemetryStore

    raw = "[bold]literal[/bold] " + "long-line " * 40
    ref = ContextCache().store(raw, source="test")
    res = runner.invoke(app, ["retrieve", ref])
    assert res.exit_code == 0
    assert res.stdout == raw + "\n"
    assert TelemetryStore().get_stats().saved_openai < 0


def test_cli_cat(tmp_path):
    f = tmp_path / "hello.py"
    f.write_text("class Foo:\n    def bar(self):\n        pass\n")
    res = runner.invoke(app, ["cat", str(f), "--skeleton"])
    assert res.exit_code == 0
    assert "class Foo:" in res.output


def test_cli_tree(tmp_path):
    f = tmp_path / "test.py"
    f.write_text("def test(): pass\n")
    res = runner.invoke(app, ["tree", str(tmp_path)])
    assert res.exit_code == 0
    assert "test.py" in res.output


def test_cli_retrieve(tmp_path):
    cache = ContextCache(db_path=tmp_path / "c.db")
    ref_id = cache.store("Hello raw world!", source="test")

    # Monkeypatch default cache in cli if needed or invoke
    res = runner.invoke(app, ["retrieve", ref_id])
    assert res.exit_code == 0


def test_cli_pipe():
    res = runner.invoke(app, ["pipe"], input="Line 1\nLine 2\n")
    assert res.exit_code == 0
    assert "Line 1" in res.output


def test_cli_diff():
    res = runner.invoke(app, ["diff"])
    assert res.exit_code == 0

    res_ignore = runner.invoke(app, ["diff", "--ignore", r"\.tmp$"])
    assert res_ignore.exit_code == 0

    res_budget = runner.invoke(app, ["diff", "--budget", "1000"])
    assert res_budget.exit_code == 0


def test_cli_lint(tmp_path):
    f = tmp_path / "CLAUDE.md"
    f.write_text("# Instructions\n- Be concise\n")
    res = runner.invoke(app, ["lint", str(f)])
    assert res.exit_code == 0
    assert "Rule Audit" in res.output


def test_cli_json_string():
    raw = '[{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}]'
    res = runner.invoke(app, ["json", raw])
    assert res.exit_code == 0
    assert "omitted by usagetrim" in res.output


def test_cli_json_stdin():
    raw = '{"items": [1, 2, 3, 4, 5, 6, 7]}'
    res = runner.invoke(app, ["json"], input=raw)
    assert res.exit_code == 0
    assert "omitted by usagetrim" in res.output


def test_cli_doctor():
    res = runner.invoke(app, ["doctor"])
    assert res.exit_code == 0
    assert "usagetrim System & Integration Diagnostics" in res.output


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point every client config at tmp_path and stub out the slow or global parts of --fix."""
    from pathlib import Path

    from rich.console import Console

    import usagetrim.core.doctor as doc

    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("APPDATA", str(home / "AppData"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr("usagetrim.cli.console", Console(width=250))
    monkeypatch.setattr("usagetrim.cli._codex_available", lambda: False)
    monkeypatch.setattr(
        doc,
        "_local_mcp_command",
        lambda profile=None: {"command": "/bin/usagetrim", "args": ["mcp"]},
    )
    monkeypatch.setattr(
        "usagetrim.cli.probe_server_entry",
        lambda entry: (
            doc.HandshakeResult("ok", f"probed {entry['command']}")
            if entry
            else doc.HandshakeResult("skipped", "usagetrim not registered")
        ),
    )
    return home


@pytest.fixture
def isolated_doctor_fix(tmp_path, monkeypatch, isolated_home):
    import usagetrim.core.doctor as doc

    target = tmp_path / "mcp.json"
    target.write_text('{"mcpServers": {"usagetrim": {"command": "/old/venv/bin/usagetrim"}},}')
    for installer in (
        "configure_cursor_mcp",
        "configure_windsurf_mcp",
        "configure_claude_desktop_mcp",
        "configure_shell_alias",
    ):
        monkeypatch.setattr(f"usagetrim.cli.{installer}", lambda: (False, "skipped in test"))
    monkeypatch.setattr(
        "usagetrim.cli.MCP_CLIENTS", (doc.McpClient("Cursor", lambda: target, "json"),)
    )
    return target


def test_cli_doctor_fix_yes_repairs_and_probes(isolated_doctor_fix):
    result = runner.invoke(app, ["doctor", "--fix", "--yes"])
    assert result.exit_code == 0, result.output
    assert "MCP Client Repair & Handshake" in result.output
    assert "probed /bin/usagetrim" in result.output
    assert json.loads(isolated_doctor_fix.read_text())["mcpServers"]["usagetrim"]["command"] == (
        "/bin/usagetrim"
    )
    assert list(isolated_doctor_fix.parent.glob("mcp.json.pre-usagetrim-*"))


def test_cli_doctor_fix_without_tty_or_yes_changes_nothing(isolated_doctor_fix):
    original = isolated_doctor_fix.read_text()
    result = runner.invoke(app, ["doctor", "--fix"])
    assert result.exit_code == 0, result.output
    assert "re-run with --yes" in result.output
    assert "repairs not applied" in result.output
    assert isolated_doctor_fix.read_text() == original
    assert not list(isolated_doctor_fix.parent.glob("mcp.json.pre-usagetrim-*"))


@pytest.mark.parametrize("answer, applied", [("y\n", True), ("n\n", False), ("", False)])
def test_cli_doctor_fix_asks_before_repairing(isolated_doctor_fix, monkeypatch, answer, applied):
    monkeypatch.setattr("usagetrim.cli._stdin_is_interactive", lambda: True)
    original = isolated_doctor_fix.read_text()
    result = runner.invoke(app, ["doctor", "--fix"], input=answer)
    assert result.exit_code == 0, result.output
    assert "Apply these repairs?" in result.output
    assert "MCP Client Repair & Handshake" in result.output
    assert (isolated_doctor_fix.read_text() != original) is applied


def test_cli_doctor_fix_leaves_existing_entries_to_confirmed_repairs(isolated_home):
    cursor_config = isolated_home / ".cursor" / "mcp.json"
    cursor_config.parent.mkdir(parents=True)
    original = json.dumps(
        {
            "mcpServers": {
                "usagetrim": {
                    "command": "/old/venv/bin/usagetrim",
                    "args": ["mcp", "--profile", "full"],
                }
            }
        }
    )
    cursor_config.write_text(original)
    result = runner.invoke(app, ["doctor", "--fix"])
    assert result.exit_code == 0, result.output
    assert "pending: replaced stale command" in result.output
    assert cursor_config.read_text() == original
    windsurf_config = isolated_home / ".codeium" / "windsurf" / "mcp_config.json"
    assert json.loads(windsurf_config.read_text())["mcpServers"]["usagetrim"]["command"] == (
        "/bin/usagetrim"
    )


def test_cli_doctor_warns_that_yes_needs_fix():
    result = runner.invoke(app, ["doctor", "--yes"])
    assert result.exit_code == 0
    assert "--yes only applies together with --fix" in result.output


def test_cli_install_desktop_honors_installer_failure(monkeypatch):
    monkeypatch.setattr(
        "usagetrim.cli.configure_claude_desktop_mcp",
        lambda: (False, "Malformed existing config; unchanged"),
    )
    result = runner.invoke(app, ["install", "--claude-desktop"])
    assert result.exit_code == 1
    assert "unchanged" in result.output
    assert "configured in" not in result.output


def test_cli_install_desktop_selects_only_desktop(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "usagetrim.cli.configure_claude_desktop_mcp",
        lambda: (calls.append("desktop") or True, "/tmp/claude.json"),
    )
    monkeypatch.setattr("usagetrim.cli.configure_cursor_mcp", lambda: calls.append("cursor"))
    monkeypatch.setattr("usagetrim.cli.configure_shell_alias", lambda: calls.append("alias"))
    result = runner.invoke(app, ["install", "--claude-desktop"])
    assert result.exit_code == 0
    assert calls == ["desktop"]


def test_cli_install_windsurf_selects_only_windsurf(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "usagetrim.cli.configure_windsurf_mcp",
        lambda: (calls.append("windsurf") or True, "/tmp/windsurf.json"),
    )
    monkeypatch.setattr("usagetrim.cli.configure_cursor_mcp", lambda: calls.append("cursor"))
    monkeypatch.setattr("usagetrim.cli.configure_shell_alias", lambda: calls.append("alias"))
    monkeypatch.setattr(
        "usagetrim.cli.configure_claude_desktop_mcp", lambda: calls.append("desktop")
    )
    result = runner.invoke(app, ["install", "--windsurf"])
    assert result.exit_code == 0
    assert calls == ["windsurf"]


def test_cli_stats_formats():
    res_table = runner.invoke(app, ["stats", "--format", "table"])
    assert res_table.exit_code == 0
    assert "local output estimates" in res_table.output

    res_json = runner.invoke(app, ["stats", "--format", "json"])
    assert res_json.exit_code == 0
    assert "total_runs" in res_json.output

    res_md = runner.invoke(app, ["stats", "--format", "markdown"])
    assert res_md.exit_code == 0
    assert "| **Total Executions** |" in res_md.output


def test_cli_cat_strip_comments(tmp_path):
    f = tmp_path / "app.py"
    f.write_text(
        "# Copyright header\n# Another comment\ndef run():\n    # Inline comment\n    return 42\n"
    )
    res = runner.invoke(app, ["cat", str(f), "--strip-comments"])
    assert res.exit_code == 0
    assert "# Copyright header" not in res.output
    assert "def run():" in res.output


def test_cli_cat_if_modified_since_and_hash(tmp_path):
    import hashlib

    f = tmp_path / "app.py"
    f.write_text("var = 'constant'\n")
    content_hash = hashlib.sha256(f.read_bytes()).hexdigest()

    # When unchanged, returns 304 Not Modified notice
    res_cached = runner.invoke(app, ["cat", str(f), "--if-modified-since", content_hash])
    assert res_cached.exit_code == 0
    assert "304 Not Modified" in res_cached.output
    assert "app.py" in res_cached.output

    # When changed / mismatched hash, returns full content
    res_fresh = runner.invoke(app, ["cat", str(f), "--if-modified-since", "wronghash123"])
    assert res_fresh.exit_code == 0
    assert "304 Not Modified" not in res_fresh.output
    assert "var = 'constant'" in res_fresh.output

    # With --include-hash
    res_hash = runner.invoke(app, ["cat", str(f), "--include-hash"])
    assert res_hash.exit_code == 0
    assert "# [sha256:" in res_hash.output
    assert "var = 'constant'" in res_hash.output


def test_cli_cat_types(tmp_path):
    f = tmp_path / "service.py"
    f.write_text(
        "class MyService:\n    timeout: int = 30\n    def execute(self):\n        for i in range(100): pass\n"
    )
    res = runner.invoke(app, ["cat", str(f), "--types"])
    assert res.exit_code == 0
    assert "class MyService:" in res.output
    assert "timeout: int = 30" in res.output
    assert "def execute(self): ..." in res.output
    assert "for i in range" not in res.output


def test_cli_types_command(tmp_path):
    f = tmp_path / "types_test.py"
    f.write_text("class Config:\n    debug: bool = False\n    def run(self):\n        pass\n")
    res = runner.invoke(app, ["types", str(f)])
    assert res.exit_code == 0
    assert "class Config:" in res.output
    assert "debug: bool = False" in res.output
    assert "def run(self): ..." in res.output

    # Test output to file
    out_file = tmp_path / "types_test.pyi"
    res_out = runner.invoke(app, ["types", str(f), "-o", str(out_file)])
    assert res_out.exit_code == 0
    assert out_file.exists()
    assert "class Config:" in out_file.read_text()


def test_cli_cache():
    res_stats = runner.invoke(app, ["cache", "stats"])
    assert res_stats.exit_code == 0
    assert "CCR Cache Store" in res_stats.output

    res_clear = runner.invoke(app, ["cache", "clear"])
    assert res_clear.exit_code == 0
    assert "Cleared" in res_clear.output


def test_cli_pr():
    res = runner.invoke(app, ["pr"])
    assert res.exit_code == 0
    assert "Token Delta" in res.output

    res_md = runner.invoke(app, ["pr", "--markdown"])
    assert res_md.exit_code == 0
    assert "Token Impact Report" in res_md.output


def test_cli_clip_file(tmp_path):
    f = tmp_path / "noisy.log"
    f.write_text("INFO line\n" * 30 + "DONE\n")
    res = runner.invoke(app, ["clip", "--file", str(f), "--stats"])
    assert res.exit_code == 0
    assert "DONE" in res.output
    assert "preceding line repeated" in res.output


def test_cli_clip_stdin():
    tb = "Traceback (most recent call last):\n" + (
        "  File 'a.py', line 1, in foo\n    foo()\n" * 10
    )
    res = runner.invoke(
        app,
        ["clip", "--budget", "500"],
        input=tb,
    )
    assert res.exit_code == 0
    assert "identical recursive frame repeated" in res.output


def test_cli_pack_json(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "test.py").write_text("def foo():\n    return 'bar'\n")
    res = runner.invoke(app, ["pack", "--root", str(tmp_path), "--json"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["file_count"] >= 1
    assert any(f["path"] == "src/test.py" for f in data["files"])


def test_cli_pack_output(tmp_path):
    (tmp_path / "hello.txt").write_text("hello world")
    out = tmp_path / "bundle.md"
    res = runner.invoke(app, ["pack", "--root", str(tmp_path), "--output", str(out)])
    assert res.exit_code == 0
    assert out.exists()
    assert "hello world" in out.read_text()


def test_cli_pack_modes_formats_focus_and_budget(tmp_path, monkeypatch):
    import xml.etree.ElementTree as ET

    from usagetrim.metrics.tokenizer import count_tokens

    monkeypatch.setenv("USAGETRIM_CACHE_DIR", str(tmp_path / "cache"))
    core = tmp_path / "src" / "core"
    core.mkdir(parents=True)
    (core / "service.py").write_text("def service():\n" + "    return 1\n" * 80)
    (tmp_path / "api.py").write_text("def api():\n" + "    return 2\n" * 80)
    xml = runner.invoke(
        app,
        [
            "pack",
            "--root",
            str(tmp_path),
            "--mode",
            "hybrid",
            "--focus",
            "src/core/",
            "--budget",
            "800",
        ],
    )
    assert xml.exit_code == 0, xml.output
    document = ET.fromstring(xml.stdout)
    assert document.find("file_summary").attrib["mode"] == "hybrid"
    assert count_tokens(xml.stdout).openai <= 800
    markdown = runner.invoke(
        app, ["pack", "--root", str(tmp_path), "--format", "markdown", "--mode", "full"]
    )
    assert markdown.exit_code == 0 and "## Directory Structure" in markdown.stdout
    assert "### File: api.py (full)" in markdown.stdout


def test_cli_pack_rejects_invalid_modes_and_legacy_conflict(tmp_path):
    (tmp_path / "a.py").write_text("def a(): pass\n")
    for options in (
        ["--mode", "unknown"],
        ["--format", "json"],
        ["--skeleton", "--mode", "full"],
        ["--focus", "a.py"],
    ):
        result = runner.invoke(app, ["pack", "--root", str(tmp_path), *options])
        assert result.exit_code == 2


def test_cli_pack_repeatable_focus_positional_paths_and_strip_comments(tmp_path):
    (tmp_path / "first.py").write_text("# first comment\ndef first(): return 1\n")
    (tmp_path / "second.py").write_text("# second comment\ndef second(): return 2\n")
    (tmp_path / "other.py").write_text("def other(): return 3\n")
    result = runner.invoke(
        app,
        [
            "pack",
            "first.py",
            "second.py",
            "--root",
            str(tmp_path),
            "--mode",
            "hybrid",
            "--focus",
            "first.py",
            "--focus",
            "second.py",
            "--strip-comments",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert {file["path"] for file in payload["files"]} == {"first.py", "second.py"}
    assert all(not file["is_skeleton"] for file in payload["files"])
    assert "first comment" not in payload["bundle_text"]
    assert "second comment" not in payload["bundle_text"]


def test_cli_distill(tmp_path):
    f = tmp_path / "chat.txt"
    f.write_text("User: Build feature in src/app.py\n\nAssistant: We decided to use SQLite.\n")
    res = runner.invoke(app, ["distill", "--file", str(f), "--stats"])
    assert res.exit_code == 0
    assert "Distilled Conversation Context" in res.output
    assert "src/app.py" in res.output


def test_cli_table(tmp_path):
    f = tmp_path / "data.json"
    f.write_text(json.dumps([{"id": 1, "name": "foo"}, {"id": 2, "name": "bar"}]))
    res = runner.invoke(app, ["table", "--file", str(f), "--stats"])
    assert res.exit_code == 0
    assert "[id | name]" in res.output
    assert "1 | foo" in res.output


def test_cli_prompt_lint_and_align(tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Current time: 2026-09-21\nYou are an AI assistant.\n")
    res_lint = runner.invoke(app, ["prompt", "lint", str(prompt_file)])
    assert res_lint.exit_code == 0
    assert "Cacheability Score" in res_lint.output

    out_file = tmp_path / "aligned.txt"
    res_align = runner.invoke(app, ["prompt", "align", str(prompt_file), "--output", str(out_file)])
    assert res_align.exit_code == 0
    assert out_file.exists()
    assert "DYNAMIC RUNTIME CONTEXT" in out_file.read_text()


def test_cli_prompt_minify(tmp_path):
    f = tmp_path / "instructions.md"
    f.write_text("<!-- comment -->\nPlease make sure to always write tests.\n")
    res = runner.invoke(app, ["prompt", "minify", str(f), "--stats"])
    assert res.exit_code == 0
    assert "Always write tests." in res.output
    assert "comment" not in res.output


def test_cli_prompt_cache_inject(tmp_path):
    payload_file = tmp_path / "payload.json"
    out_file = tmp_path / "cached_payload.json"
    payload = {
        "tools": [
            {"name": "zebra_tool", "description": "Zebra tool description"},
            {"name": "alpha_tool", "description": "Alpha tool description"},
        ],
        "messages": [
            {"role": "user", "content": "Instructions and prompt content. " * 60},
        ],
    }
    payload_file.write_text(json.dumps(payload), encoding="utf-8")

    res = runner.invoke(
        app,
        [
            "prompt",
            "cache-inject",
            str(payload_file),
            "--output",
            str(out_file),
            "--min-tokens",
            "100",
        ],
    )
    assert res.exit_code == 0
    assert out_file.exists()
    cached = json.loads(out_file.read_text(encoding="utf-8"))
    assert [t["name"] for t in cached["tools"]] == ["alpha_tool", "zebra_tool"]
    assert "cache_control" in cached["messages"][-1]["content"][-1]


def test_cli_optimize_file(tmp_path):
    f = tmp_path / "prompt.md"
    f.write_text("""Review this table:
```json
[{"id": 1, "status": "active"}, {"id": 2, "status": "idle"}]
```
Any observations?
""")
    res = runner.invoke(app, ["optimize", str(f), "--stats"])
    assert res.exit_code == 0
    assert "Review this table:" in res.output
    assert "[id | status]" in res.output
    assert "1 | active" in res.output


def test_cli_optimize_json(tmp_path):
    f = tmp_path / "input.txt"
    f.write_text("User: Hello\nAssistant: Hi there!")
    res = runner.invoke(app, ["optimize", str(f), "--json"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert "original_tokens" in data
    assert "optimized_tokens" in data
    assert "ref_id" in data


def test_cli_share():
    res = runner.invoke(app, ["share"])
    assert res.exit_code == 0
    assert "Share UsageTrim & Add Badge" in res.output
    assert "shields.io/badge/usagetrim" in res.output
    assert "github.com/00200200/usagetrim" in res.output


def test_cli_share_badge():
    res = runner.invoke(app, ["share", "--badge"])
    assert res.exit_code == 0
    assert res.output.strip().startswith(
        "[![UsageTrim Context](https://img.shields.io/badge/usagetrim-"
    )


def test_cli_rules_init(tmp_path):
    target = tmp_path / "CLAUDE.md"
    res = runner.invoke(app, ["rules", "--init", "--file", str(target), "--client", "claude"])
    assert res.exit_code == 0
    assert target.exists()
    assert "UsageTrim Claude Rules" in target.read_text()


def test_cli_rules_optimize(tmp_path):
    target = tmp_path / "CLAUDE.md"
    target.write_text(
        "# Guidelines\n\n"
        "You are an expert AI assistant. Please make sure to always remember to never forget this.\n\n"
        "Current Date: 2026-09-22\n\n"
        "- Run pytest before pushing code.\n"
    )
    # Test --optimize --stats (dry run)
    res_dry = runner.invoke(app, ["rules", "--optimize", "--file", str(target), "--stats"])
    assert res_dry.exit_code == 0
    assert "UsageTrim Rules Optimization:" in res_dry.output
    assert "Run pytest before pushing code" in res_dry.output
    assert "You are an expert" not in res_dry.output

    # Test --optimize --write
    res_write = runner.invoke(app, ["rules", "--optimize", "--file", str(target), "--write"])
    assert res_write.exit_code == 0
    assert "Optimized" in res_write.output
    bak_file = tmp_path / "CLAUDE.md.bak"
    assert bak_file.exists()
    assert "You are an expert" in bak_file.read_text()
    assert "You are an expert" not in target.read_text()
    assert "Current Date: 2026-09-22" in target.read_text()
    # The date belongs to Guidelines and must not be lifted into a global suffix.
    assert "Dynamic Runtime Context" not in target.read_text()


def test_cli_install_codex_and_profile(tmp_path, monkeypatch):
    import usagetrim.core.doctor as doc

    fake_codex_cfg = tmp_path / "config.toml"
    fake_claude_cfg = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(doc, "get_codex_config_path", lambda: fake_codex_cfg)
    monkeypatch.setattr(doc, "get_claude_desktop_config_path", lambda: fake_claude_cfg)
    monkeypatch.setattr(
        doc,
        "_local_mcp_command",
        lambda profile=None: {
            "command": "/bin/usagetrim",
            "args": ["mcp", "--profile", profile] if profile else ["mcp"],
        },
    )

    res_codex = runner.invoke(app, ["install", "--codex", "--profile", "desktop"])
    assert res_codex.exit_code == 0
    assert "Codex MCP configured" in res_codex.output
    assert fake_codex_cfg.exists()
    assert '"desktop"' in fake_codex_cfg.read_text()

    res_claude = runner.invoke(app, ["install", "--claude-desktop", "--profile", "desktop"])
    assert res_claude.exit_code == 0
    assert "Claude Desktop MCP configured" in res_claude.output
    assert fake_claude_cfg.exists()
    assert "desktop" in fake_claude_cfg.read_text()


def test_cli_prompt_trim(tmp_path):
    import json

    messages = [
        {"role": "system", "content": "You are an assistant."},
        {"role": "user", "content": "Fix bug in app."},
    ]
    for i in range(10):
        messages.append({"role": "assistant", "content": f"Step {i} investigating"})
        messages.append({"role": "tool", "name": "bash", "content": "pytest passed\n" * 50})

    input_file = tmp_path / "chat.json"
    output_file = tmp_path / "chat_trimmed.json"
    input_file.write_text(json.dumps(messages), encoding="utf-8")

    res = runner.invoke(
        app,
        [
            "prompt",
            "trim",
            str(input_file),
            "--max-tokens",
            "8000",
            "--keep-recent",
            "4",
            "-o",
            str(output_file),
        ],
    )
    assert res.exit_code == 0
    assert output_file.exists()
    trimmed_data = json.loads(output_file.read_text(encoding="utf-8"))
    assert len(trimmed_data) == len(messages)
    assert "omitted by usagetrim" in trimmed_data[3]["content"]


@pytest.fixture
def structural_cli_repo(tmp_path, monkeypatch):
    import subprocess

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            cwd=tmp_path,
        ).decode("utf-8")

    git("init", "-q", "--initial-branch=main")
    (tmp_path / "formatted.py").write_text("def check(x):\n    return x > 5\n")
    (tmp_path / "logic.py").write_text("value = 1\n")
    git("add", ".")
    git("commit", "-qm", "Initial sources")
    monkeypatch.chdir(tmp_path)
    return tmp_path, git


@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("logic", [False, True])
def test_cli_diff_structural_and_genuine_changes(structural_cli_repo, staged, logic):
    root, git = structural_cli_repo
    (root / "formatted.py").write_text(
        "def check( x ):\n    return x" + (">=5" if logic else ">5") + "\n"
    )
    if staged:
        git("add", "formatted.py")
    result = runner.invoke(app, ["diff"] + (["--staged"] if staged else []))
    assert result.exit_code == 0, result.output
    if logic:
        assert "formatting-only" not in result.stdout
        assert "+    return x>=5" in result.stdout
    else:
        assert "formatting-only" in result.stdout
        assert "+def check( x ):" not in result.stdout


def test_cli_structural_diff_preserves_mixed_logic_and_raw_telemetry(
    structural_cli_repo, monkeypatch
):
    import usagetrim.cli as cli

    root, git = structural_cli_repo
    (root / "formatted.py").write_text("def check( x ):\n    return x>5\n")
    (root / "logic.py").write_text("value = 2\n")
    raw = git("diff", "--no-ext-diff", "--no-textconv", "--no-color")
    records = []
    monkeypatch.setattr(cli, "record_text", lambda *args, **kwargs: records.append((args, kwargs)))
    result = runner.invoke(app, ["diff"])
    assert result.exit_code == 0
    assert "formatting-only" in result.stdout
    assert "-value = 1" in result.stdout and "+value = 2" in result.stdout
    assert records[0][0] == (raw, result.stdout)
    assert records[0][1]["engine"] == "usagetrim"


def test_cli_structural_diff_budget_and_ignore(structural_cli_repo):
    from usagetrim.metrics.tokenizer import count_tokens

    root, _ = structural_cli_repo
    (root / "formatted.py").write_text("def check( x ):\n    return x>5\n")
    result = runner.invoke(app, ["diff", "--budget", "300"])
    assert result.exit_code == 0 and "formatting-only" in result.stdout
    assert count_tokens(result.stdout).openai <= 300
    ignored = runner.invoke(app, ["diff", "--ignore", r"formatted\.py$"])
    assert ignored.exit_code == 0
    assert "formatting-only" not in ignored.stdout and "lockfile/generated" in ignored.stdout


def test_cli_diff_pause_skips_classifier_and_budget(structural_cli_repo, monkeypatch):
    import usagetrim.cli as cli

    root, git = structural_cli_repo
    (root / "formatted.py").write_text("def check( x ):\n    return x>5\n")
    raw = git("diff", "--no-ext-diff", "--no-textconv", "--no-color")

    def forbidden(*args, **kwargs):
        raise AssertionError("Pause must bypass structural classification and slimming")

    monkeypatch.setattr(cli, "paused", lambda: True)
    monkeypatch.setattr(cli, "classify_git_diff", forbidden)
    monkeypatch.setattr(cli, "budgeted_diff_slice", forbidden)
    records = []
    monkeypatch.setattr(cli, "record_text", lambda *args, **kwargs: records.append((args, kwargs)))
    result = runner.invoke(app, ["diff", "--budget", "64"])
    assert result.exit_code == 0 and result.stdout == raw
    assert records[0][0] == (raw, raw) and records[0][1]["engine"] == "none"


def test_cli_malformed_source_keeps_existing_diff_behavior(structural_cli_repo):
    from usagetrim.core.diff_slimmer import slim_git_diff

    root, git = structural_cli_repo
    (root / "formatted.py").write_text("def check(:\n pass\n")
    raw = git("diff", "--no-ext-diff", "--no-textconv", "--no-color")
    result = runner.invoke(app, ["diff"])
    assert result.exit_code == 0
    assert result.stdout.rstrip("\n") == slim_git_diff(raw).rstrip("\n")


@pytest.mark.parametrize(
    "client,relative,title",
    [
        ("claude", "CLAUDE.md", "Claude"),
        ("codex", "AGENTS.md", "Codex"),
        ("cursor", ".cursorrules", "Cursor"),
        ("copilot", ".github/copilot-instructions.md", "Copilot"),
    ],
)
def test_rules_init_client_defaults_and_refuses_overwrite(
    tmp_path, monkeypatch, client, relative, title
):
    monkeypatch.chdir(tmp_path)
    first = runner.invoke(app, ["rules", "--init", "--client", client])
    assert first.exit_code == 0
    target = tmp_path / relative
    original = target.read_bytes()
    assert original.startswith(f"# UsageTrim {title} Rules".encode())
    second = runner.invoke(app, ["rules", "--init", "--client", client])
    assert second.exit_code == 2
    assert target.read_bytes() == original


@pytest.mark.parametrize(
    "options",
    [
        ["--init", "--optimize"],
        ["--init", "--check"],
        ["--optimize", "--check"],
        ["--write"],
        ["--init", "--write"],
        ["--check", "--write"],
        ["--client", "unknown"],
        ["--stats"],
    ],
)
def test_rules_cli_rejects_invalid_options_without_writing(tmp_path, options):
    target = tmp_path / "rules.md"
    result = runner.invoke(app, ["rules", *options, "-f", str(target)])
    assert result.exit_code == 2
    assert not target.exists()
    assert not list(tmp_path.iterdir())


def test_rules_check_exit_codes_no_payload_and_advisory_stability(tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_bytes(b"# Rules\r\n- If current time is unknown, never deploy.\r\n")
    original = target.read_bytes()
    needed = runner.invoke(app, ["rules", "--check", "-f", str(target)])
    assert needed.exit_code == 1
    assert "Optimization needed." in needed.output
    assert "never deploy" not in needed.output
    assert target.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]
    target.write_bytes(original.replace(b"\r\n", b"\n"))
    canonical = runner.invoke(app, ["rules", "--check", "--stats", "-f", str(target)])
    assert canonical.exit_code == 0
    assert "Canonical." in canonical.output
    assert "delta (before-after) +0" in canonical.output
    missing = runner.invoke(app, ["rules", "--check", "-f", str(tmp_path / "missing")])
    assert missing.exit_code == 2


@pytest.mark.parametrize("options", [["--check"], ["--optimize"], ["--optimize", "--write"]])
def test_rules_invalid_utf8_never_changes_storage(tmp_path, options):
    target = tmp_path / "rules.md"
    original = b"# Rules\n\xff NEVER remove\n"
    target.write_bytes(original)
    result = runner.invoke(app, ["rules", *options, "-f", str(target)])
    assert result.exit_code == 2
    assert target.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]


def test_rules_preview_stdout_only_document_and_stats_on_stderr(tmp_path):
    target = tmp_path / "rules.md"
    original = "Hello Claude!\n- Run `uv run pytest`.\n"
    target.write_text(original, encoding="utf-8")
    result = runner.invoke(app, ["rules", "--optimize", "--stats", "-f", str(target)])
    assert result.exit_code == 0
    assert result.stdout == "- Run `uv run pytest`.\n"
    assert "local OpenAI tokens" in result.stderr
    assert "delta (before-after)" in result.stderr
    assert "estimated baseline input-cost delta" in result.stderr
    assert target.read_text() == original


def test_rules_write_backup_exact_bytes_modes_and_no_overwrite(tmp_path):
    target = tmp_path / "rules.md"
    original = "Hello Claude!\r\n- Preserve Żółć.  \r\n".encode()
    target.write_bytes(original)
    target.chmod(0o640)
    result = runner.invoke(app, ["rules", "--optimize", "--write", "-f", str(target)])
    assert result.exit_code == 0
    assert target.read_bytes() == "- Preserve Żółć.  \n".encode()
    assert target.stat().st_mode & 0o777 == 0o640
    backup = tmp_path / "rules.md.bak"
    assert backup.read_bytes() == original
    optimized = target.read_bytes()
    again = runner.invoke(app, ["rules", "--optimize", "--write", "-f", str(target)])
    assert again.exit_code == 2
    assert target.read_bytes() == optimized
    assert backup.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["rules.md", "rules.md.bak"]


def test_rules_atomic_replace_failure_cleans_backup_and_allows_retry(tmp_path, monkeypatch):
    import usagetrim.cli as cli

    target = tmp_path / "rules.md"
    original = b"Hello Claude!\n- Never deploy.\n"
    target.write_bytes(original)
    backup = tmp_path / "rules.md.bak"

    def unavailable(*args):
        raise OSError("simulated replace failure")

    with monkeypatch.context() as patch:
        patch.setattr(cli.os, "replace", unavailable)
        result = runner.invoke(app, ["rules", "--optimize", "--write", "-f", str(target)])
    assert result.exit_code == 2
    assert "simulated replace failure" in result.output
    assert target.read_bytes() == original
    assert not backup.exists()
    assert not list(tmp_path.glob(".usagetrim-rules-*"))
    retry = runner.invoke(app, ["rules", "--optimize", "--write", "-f", str(target)])
    assert retry.exit_code == 0
    assert target.read_bytes() == b"- Never deploy.\n"
    assert backup.read_bytes() == original
    assert not list(tmp_path.glob(".usagetrim-rules-*"))


def test_rules_write_and_init_reject_symlink(tmp_path):
    original = tmp_path / "original.md"
    original.write_bytes(b"Hello Claude!\n- Run tests.\n")
    link = tmp_path / "rules.md"
    try:
        link.symlink_to(original)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    for options in (["--optimize", "--write"], ["--init"]):
        result = runner.invoke(app, ["rules", *options, "-f", str(link)])
        assert result.exit_code == 2
        assert link.is_symlink()
        assert original.read_bytes() == b"Hello Claude!\n- Run tests.\n"
    assert not (tmp_path / "rules.md.bak").exists()


@pytest.mark.parametrize("failure", ["write", "fsync"])
def test_rules_failed_backup_write_cleans_transaction_and_can_retry(tmp_path, monkeypatch, failure):
    import usagetrim.cli as cli

    target = tmp_path / "rules.md"
    original = b"Hello Claude!\n- Never deploy.\n"
    target.write_bytes(original)
    backup = tmp_path / "rules.md.bak"
    real_fdopen = cli.os.fdopen
    real_fsync = cli.os.fsync

    class BrokenBackup:
        def __init__(self, descriptor, mode):
            self.stream = real_fdopen(descriptor, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, data):
            self.stream.write(data[:1])
            raise OSError("backup write failed")

    calls = 0

    def failing_fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("backup fsync failed")
        return real_fsync(descriptor)

    with monkeypatch.context() as patch:
        if failure == "write":
            patch.setattr(cli.os, "fdopen", BrokenBackup)
        else:
            patch.setattr(cli.os, "fsync", failing_fsync)
        result = runner.invoke(app, ["rules", "--optimize", "--write", "-f", str(target)])
    assert result.exit_code == 2
    assert f"backup {failure} failed" in result.output
    assert target.read_bytes() == original
    assert not backup.exists()
    assert not list(tmp_path.glob(".usagetrim-rules-*"))
    retry = runner.invoke(app, ["rules", "--optimize", "--write", "-f", str(target)])
    assert retry.exit_code == 0
    assert target.read_bytes() == b"- Never deploy.\n"
    assert backup.read_bytes() == original


def test_rules_cleanup_failure_does_not_mask_original_exception(tmp_path, monkeypatch):
    from pathlib import Path

    import usagetrim.cli as cli

    target = tmp_path / "rules.md"
    original = b"Hello Claude!\n- Never deploy.\n"
    target.write_bytes(original)
    real_unlink = Path.unlink

    def unavailable(*args):
        raise OSError("original replace failure")

    def failed_backup_unlink(path, *args, **kwargs):
        if path.name == "rules.md.bak":
            raise OSError("secondary cleanup failure")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(cli.os, "replace", unavailable)
        patch.setattr(Path, "unlink", failed_backup_unlink)
        result = runner.invoke(app, ["rules", "--optimize", "--write", "-f", str(target)])
    assert result.exit_code == 2
    assert "original replace failure" in result.output
    assert "secondary cleanup failure" not in result.output
    assert target.read_bytes() == original
