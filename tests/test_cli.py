import json
import os
import sys

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
        "raw_tokens": 8049,
        "output_tokens": 601,
        "reduction_pct": 92.5,
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
    assert "Dynamic Runtime Context" in target.read_text()


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
