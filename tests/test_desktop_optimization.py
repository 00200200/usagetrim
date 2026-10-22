import json
import tomllib
from pathlib import Path

from typer.testing import CliRunner

from usagetrim.cli import app
from usagetrim.core import native_hooks
from usagetrim.core.doctor import (
    configure_codex_mcp,
)
from usagetrim.core.prepare import prepare_text
from usagetrim.core.rules_linter import generate_desktop_rules
from usagetrim.core.specialized import (
    auto_specialize_command_output,
    filter_dev_server_logs,
    filter_traceback,
)
from usagetrim.mcp import server
from usagetrim.metrics.tokenizer import count_tokens


def test_desktop_mcp_profile_tools_and_schema_reduction():
    full_tools = server.tool_definitions("full")
    desktop_tools = server.tool_definitions("desktop")

    # Desktop profile must expose exactly the DESKTOP_TOOLS set
    tool_names = {t["name"] for t in desktop_tools}
    assert tool_names == server.DESKTOP_TOOLS
    assert "usagetrim_code" in tool_names
    assert "usagetrim_read" in tool_names
    assert "usagetrim_edit_symbol" in tool_names
    assert "usagetrim_exec" in tool_names
    assert "usagetrim_optimize" in tool_names
    assert "usagetrim_clip" in tool_names
    assert "usagetrim_retrieve" in tool_names
    assert "usagetrim_gain" in tool_names

    # Schema descriptions should be minimized for desktop context
    full_tokens = count_tokens(json.dumps(full_tools)).openai
    desktop_tokens = count_tokens(json.dumps(desktop_tools)).openai
    assert desktop_tokens < full_tokens * 0.70  # at least 30% reduction vs full

    # Instructions contain Desktop label
    instructions = server.server_instructions("desktop")
    assert "Desktop Profile" in instructions


def test_configure_codex_mcp_new_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        "usagetrim.core.doctor.shutil.which", lambda _: str(tmp_path / "bin" / "usagetrim")
    )

    target_cfg = tmp_path / ".codex" / "config.toml"
    ok, path_str = configure_codex_mcp(target_file=target_cfg, profile="desktop")
    assert ok is True
    assert Path(path_str) == target_cfg
    assert target_cfg.exists()

    content = target_cfg.read_text(encoding="utf-8")
    parsed = tomllib.loads(content)
    assert "mcp_servers" in parsed
    assert "usagetrim" in parsed["mcp_servers"]
    usagetrim_cfg = parsed["mcp_servers"]["usagetrim"]
    assert usagetrim_cfg["command"] == str(tmp_path / "bin" / "usagetrim")
    assert usagetrim_cfg["args"] == ["mcp", "--profile", "desktop"]


def test_configure_codex_mcp_preserves_existing_tables_and_comments(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "usagetrim.core.doctor.shutil.which", lambda _: str(tmp_path / "bin" / "usagetrim")
    )

    target_cfg = tmp_path / "config.toml"
    initial_toml = (
        "# Top-level comment\n"
        'model = "gpt-4o"\n\n'
        "[mcp_servers.other]\n"
        'command = "other-tool"\n'
        'args = ["serve"]\n\n'
        "[mcp_servers.usagetrim]\n"
        'command = "old-path"\n'
        'args = ["mcp", "--profile", "coding"]\n\n'
        "[mcp_servers.usagetrim.env]\n"
        'CUSTOM_KEY = "custom_val"\n'
    )
    target_cfg.write_text(initial_toml, encoding="utf-8")

    ok, path_str = configure_codex_mcp(target_file=target_cfg, profile="desktop")
    assert ok is True

    updated_content = target_cfg.read_text(encoding="utf-8")
    assert 'model = "gpt-4o"' in updated_content
    assert "[mcp_servers.other]" in updated_content

    parsed = tomllib.loads(updated_content)
    assert parsed["model"] == "gpt-4o"
    assert parsed["mcp_servers"]["other"]["command"] == "other-tool"
    assert parsed["mcp_servers"]["usagetrim"]["command"] == str(tmp_path / "bin" / "usagetrim")
    assert parsed["mcp_servers"]["usagetrim"]["args"] == ["mcp", "--profile", "desktop"]
    # Existing env table should be preserved
    assert parsed["mcp_servers"]["usagetrim"]["env"]["CUSTOM_KEY"] == "custom_val"

    # Verify backup was created
    backups = list(tmp_path.glob("*.pre-usagetrim-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == initial_toml

    # Idempotent call should not create second backup
    ok2, _ = configure_codex_mcp(target_file=target_cfg, profile="desktop")
    assert ok2 is True
    assert len(list(tmp_path.glob("*.pre-usagetrim-*"))) == 1


def test_filter_python_traceback_collapses_library_frames():
    raw_tb = (
        "Traceback (most recent call last):\n"
        '  File "/Users/alice/projects/myapp/main.py", line 45, in run\n'
        "    result = process_data(payload)\n"
        '  File "/Users/alice/projects/myapp/services/processor.py", line 12, in process_data\n'
        "    return validate_model(payload)\n"
        '  File "/Users/alice/.venv/lib/python3.11/site-packages/pydantic/main.py", line 341, in validate_model\n'
        "    values, fields_set, validation_error = validate_model(\n"
        '  File "/Users/alice/.venv/lib/python3.11/site-packages/pydantic/fields.py", line 110, in validate\n'
        "    v, errors = self._validate_sequence_like(v, values, loc, cls)\n"
        '  File "/Users/alice/.venv/lib/python3.11/site-packages/pydantic/validators.py", line 285, in dict_validator\n'
        '    raise ValidationError("not a valid dict")\n'
        '  File "/Users/alice/.venv/lib/python3.11/site-packages/pydantic/errors.py", line 12, in error\n'
        "    return str(val)\n"
        "pydantic.error_wrappers.ValidationError: 1 validation error for Model\n"
    )

    filtered = filter_traceback(raw_tb)
    assert "main.py" in filtered
    assert "processor.py" in filtered
    assert "pydantic/errors.py" in filtered
    assert "library frames in site-packages/ omitted" in filtered
    assert "ValidationError: 1 validation error for Model" in filtered

    # Verify token reduction
    before_tokens = count_tokens(raw_tb).openai
    after_tokens = count_tokens(filtered).openai
    assert after_tokens < before_tokens


def test_filter_node_traceback_collapses_internal_frames():
    raw_node_tb = (
        "TypeError: Cannot read properties of undefined (reading 'map')\n"
        "    at renderItems (/Users/alice/projects/web/src/components/List.tsx:28:14)\n"
        "    at Object.render (/Users/alice/projects/web/src/App.tsx:15:9)\n"
        "    at node_modules/react-dom/cjs/react-dom.development.js:1234:20\n"
        "    at mountIndeterminateComponent (node_modules/react-dom/cjs/react-dom.development.js:20103:13)\n"
        "    at beginWork (node_modules/react-dom/cjs/react-dom.development.js:21601:16)\n"
        "    at HTMLUnknownElement.callCallback (node_modules/react-dom/cjs/react-dom.development.js:4164:14)\n"
        "    at processTicksAndRejections (node:internal/process/task_queues:95:5)\n"
    )

    filtered = filter_traceback(raw_node_tb)
    assert "List.tsx" in filtered
    assert "App.tsx" in filtered
    assert "internal/node_modules frames omitted" in filtered
    assert "TypeError: Cannot read properties of undefined" in filtered


def test_filter_dev_server_logs():
    raw_logs = (
        "[vite] page reload src/App.tsx\n"
        "[vite] hmr update /src/index.css\n"
        "[vite] hmr update /src/components/Header.tsx\n"
        "[vite] hmr update /src/components/Footer.tsx\n"
        "[vite] hmr update /src/components/Sidebar.tsx\n"
        "GET /api/user 200 OK - 12ms\n"
        "GET /api/user 200 OK - 8ms\n"
        "GET /api/user 200 OK - 10ms\n"
        "GET /api/user 200 OK - 7ms\n"
        "GET /api/user 200 OK - 9ms\n"
        "GET /_next/static/chunks/main.js 200 in 2ms\n"
        "GET /_next/static/chunks/app.js 304 in 1ms\n"
        "GET /_next/static/chunks/style.css 200 in 3ms\n"
        "POST /api/action 500 Internal Server Error in 45ms\n"
    )

    filtered = filter_dev_server_logs(raw_logs)
    assert "page reload src/App.tsx" in filtered
    assert "Vite HMR updates collapsed" in filtered
    assert "repeated 5x" in filtered
    assert "static asset requests (200/304 OK) collapsed" in filtered
    # Error line must be preserved
    assert "500 Internal Server Error" in filtered


def test_auto_specialize_routes_dev_server_and_traceback():
    tb_cmd_out = auto_specialize_command_output(
        "python main.py",
        "Traceback (most recent call last):\n"
        '  File "app.py", line 1, in <module>\n'
        '  File "site-packages/a.py", line 2, in f1\n'
        '  File "site-packages/b.py", line 3, in f2\n'
        '  File "site-packages/c.py", line 4, in f3\n'
        "ValueError: bad value",
    )
    assert tb_cmd_out is not None
    assert "site-packages/ omitted" in tb_cmd_out

    dev_cmd_out = auto_specialize_command_output(
        "npm run dev",
        "[vite] hmr update /a.js\n[vite] hmr update /b.js\n[vite] hmr update /c.js\n",
    )
    assert dev_cmd_out is not None
    assert "HMR updates collapsed" in dev_cmd_out


def test_prepare_text_desktop_mode():
    sample = (
        "Traceback (most recent call last):\n"
        '  File "/Users/alice/myapp/app.py", line 10, in run\n'
        '  File "/Users/alice/.venv/lib/python3.11/site-packages/pkg/a.py", line 1, in a\n'
        '  File "/Users/alice/.venv/lib/python3.11/site-packages/pkg/b.py", line 2, in b\n'
        '  File "/Users/alice/.venv/lib/python3.11/site-packages/pkg/c.py", line 3, in c\n'
        "ZeroDivisionError: division by zero\n"
    )

    res = prepare_text(sample, mode="desktop")
    assert res["mode"] == "desktop"
    assert "site-packages/" in res["text"]
    assert "ZeroDivisionError" in res["text"]


def test_generate_desktop_rules():
    claude_rules = generate_desktop_rules("claude")
    assert "UsageTrim Claude Rules" in claude_rules
    assert "usagetrim_code" in claude_rules
    assert "usagetrim_read" in claude_rules
    assert "usagetrim_edit_symbol" in claude_rules

    codex_rules = generate_desktop_rules("codex")
    assert "UsageTrim Codex Rules" in codex_rules
    assert "usagetrim_code" in codex_rules
    assert "usagetrim_read" in codex_rules


def test_cli_rules_command(tmp_path):
    runner = CliRunner()
    target_claude = tmp_path / "CLAUDE.md"
    res = runner.invoke(
        app, ["rules", "--init", "--client", "claude", "--file", str(target_claude)]
    )
    assert res.exit_code == 0
    assert target_claude.exists()
    assert "UsageTrim Claude Rules" in target_claude.read_text(encoding="utf-8")

    target_codex = tmp_path / "AGENTS.md"
    res_codex = runner.invoke(
        app, ["rules", "--init", "--client", "codex", "--file", str(target_codex)]
    )
    assert res_codex.exit_code == 0
    assert target_codex.exists()
    assert "UsageTrim Codex Rules" in target_codex.read_text(encoding="utf-8")


def test_cli_install_codex(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        "usagetrim.core.doctor.shutil.which", lambda _: str(tmp_path / "bin" / "usagetrim")
    )

    runner = CliRunner()
    res = runner.invoke(app, ["install", "--codex"])
    assert res.exit_code == 0
    codex_file = tmp_path / ".codex" / "config.toml"
    assert codex_file.exists()
    assert "[mcp_servers.usagetrim]" in codex_file.read_text()


def test_prompt_cache_threshold_and_lint(tmp_path):
    from usagetrim.core.rules_linter import lint_rule_content, optimize_rules

    # Test small rules (under 1024 tokens)
    short_content = "# Short Project Rules\n- Follow standard style.\n"
    res_short = lint_rule_content(short_content)
    assert res_short.exceeds_cache_threshold is False
    assert res_short.tokens_to_cache_threshold > 0
    assert "usagetrim MCP desktop profile" in res_short.cache_advice

    # Test large rules (over 1024 tokens)
    long_content = "# Big Rules\n" + ("- Rule item with details and explanations.\n" * 250)
    res_long = lint_rule_content(long_content)
    assert res_long.exceeds_cache_threshold is True
    assert res_long.tokens_to_cache_threshold == 0
    assert "standalone" in res_long.cache_advice.lower()

    # Test optimize_rules returns cache threshold data
    opt_res = optimize_rules(short_content)
    assert "exceeds_cache_threshold" in opt_res
    assert "cache_advice" in opt_res

    # Test CLI lint output
    test_file = tmp_path / "CLAUDE.md"
    test_file.write_text(short_content, encoding="utf-8")
    runner = CliRunner()
    lint_cli = runner.invoke(app, ["lint", str(test_file)])
    assert lint_cli.exit_code == 0
    assert "Cache Threshold" in lint_cli.stdout

    # Test CLI rules --optimize --stats output
    rules_cli = runner.invoke(app, ["rules", "--optimize", "--stats", "--file", str(test_file)])
    assert rules_cli.exit_code == 0
    assert "UsageTrim Rules Optimization:" in rules_cli.stderr


def test_prepare_cli_clipboard_and_copy(tmp_path, monkeypatch):
    mock_clipboard = {
        "content": "Traceback (most recent call last):\n  File 'app.py', line 10, in run\nValueError: test error\n"
    }
    copied = []

    monkeypatch.setattr("usagetrim.core.clip.get_clipboard", lambda: mock_clipboard["content"])
    monkeypatch.setattr("usagetrim.core.clip.set_clipboard", lambda text: copied.append(text))

    runner = CliRunner()
    res = runner.invoke(app, ["prepare", "--desktop", "--clipboard", "--copy"])
    assert res.exit_code == 0
    assert len(copied) == 1
    assert "ValueError: test error" in copied[0]


def test_distill_conversation_desktop_roles():
    from usagetrim.core.distill import _parse_messages

    chat = """You:
Can you check the database schema?

Codex:
I inspected the SQLite schema and found 3 migrations.

ChatGPT:
All tables have primary keys.

Gemini:
Index on user_id looks optimal.
"""
    messages = _parse_messages(chat)
    roles = [role for role, _ in messages]
    assert "User" in roles
    assert "Codex" in roles
    assert "Chatgpt" in roles
    assert "Gemini" in roles


def test_usagetrim_diff_with_path(monkeypatch):
    from usagetrim.mcp.server import handle_usagetrim_diff

    executed_cmd = []

    def mock_run(cmd, **kwargs):
        executed_cmd.extend(cmd)

        class MockRes:
            returncode = 0
            stdout = "diff --git a/foo.py b/foo.py\n+print('hello')\n"
            stderr = ""

        return MockRes()

    monkeypatch.setattr("subprocess.run", mock_run)

    out = handle_usagetrim_diff({"path": "src/usagetrim/core", "staged": True})
    assert "git" in executed_cmd
    assert "--cached" in executed_cmd
    assert "--" in executed_cmd
    assert "src/usagetrim/core" in executed_cmd
    assert "+print('hello')" in out


def test_version_flag_matches_package_and_release_metadata():
    from usagetrim import __version__

    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"usagetrim {__version__}"
    root = Path(__file__).resolve().parents[1]

    def load(path):
        return json.loads((root / path).read_text())

    manifest = load("extensions/claude-desktop/manifest.json")
    registry = load("server.json")
    claude_plugin = load("plugins/usagetrim/.claude-plugin/plugin.json")
    codex_plugin = load("plugins/usagetrim/.codex-plugin/plugin.json")
    claude_market = load(".claude-plugin/marketplace.json")
    # Registry, extension and plugin metadata must not drift from the published package.
    assert manifest["version"] == registry["version"] == __version__
    assert {p["version"] for p in registry["packages"]} == {__version__}
    # The registry installs the released .mcpb, so it needs no PyPI account.
    assert [p["identifier"] for p in registry["packages"]] == [
        _release_wheel().replace("-py3-none-any.whl", ".mcpb")
    ]
    assert claude_plugin["version"] == codex_plugin["version"] == __version__
    assert {p["version"] for p in claude_market["plugins"]} == {__version__}
    bundle = tomllib.loads((root / "extensions/claude-desktop/pyproject.toml").read_text())
    assert bundle["project"]["dependencies"] == [f"usagetrim @ {_release_wheel()}"]


def _release_wheel():
    from usagetrim import __version__

    # Pinned GitHub release wheel: no PyPI account or git checkout needed, cached by uv.
    return (
        "https://github.com/00200200/usagetrim/releases/download/"
        f"v{__version__}/usagetrim-{__version__}-py3-none-any.whl"
    )


def test_app_plugins_launch_the_released_mcp_server():
    root = Path(__file__).resolve().parents[1]
    plugin = root / "plugins/usagetrim"
    servers = json.loads((plugin / ".mcp.json").read_text())["mcpServers"]
    launch = ["--from", _release_wheel(), "usagetrim", "mcp"]
    assert servers == {"usagetrim": {"command": "uvx", "args": launch}}
    codex = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
    assert (plugin / codex["interface"]["logo"]).is_file()
    for market in (".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json"):
        entries = json.loads((root / market).read_text())["plugins"]
        paths = [
            e["source"] if isinstance(e["source"], str) else e["source"]["path"] for e in entries
        ]
        assert all((root / path).resolve() == plugin for path in paths)
    hooks = json.loads((plugin / ".claude-plugin/plugin.json").read_text())["hooks"]
    expected = f"uvx --from {_release_wheel()} usagetrim hook-filter --client claude"
    assert [e["matcher"] for e in hooks["PostToolUse"]] == list(native_hooks.HOOK_MATCHERS)
    assert {e["hooks"][0]["command"] for e in hooks["PostToolUse"]} == {expected}


def test_desktop_extension_lists_the_desktop_profile_tools():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "extensions/claude-desktop/manifest.json").read_text())
    assert manifest["server"]["type"] == "uv"
    assert (root / "extensions/claude-desktop" / manifest["server"]["entry_point"]).is_file()
    assert (root / "extensions/claude-desktop" / manifest["icon"]).is_file()
    assert [t["name"] for t in manifest["tools"]] == [
        t["name"] for t in server.tool_definitions("desktop")
    ]


def test_install_mcpb_copies_a_packable_bundle(tmp_path):
    from usagetrim.core.doctor import EXTENSION_FILES, write_claude_desktop_extension

    ok, msg = write_claude_desktop_extension(tmp_path)
    assert ok, msg
    assert all((tmp_path / name).is_file() for name in EXTENSION_FILES)


def test_cheap_explore_overrides_explore_once_and_never_clobbers(tmp_path):
    from usagetrim.core.doctor import install_cheap_explore

    ok, path = install_cheap_explore(tmp_path)
    assert ok
    text = Path(path).read_text()
    assert "name: Explore\n" in text and "model: haiku\n" in text
    assert install_cheap_explore(tmp_path) == (True, path)  # idempotent
    Path(path).write_text("---\nname: Explore\nmodel: sonnet\n---\nmine\n")
    ok, msg = install_cheap_explore(tmp_path)
    assert not ok and "leaving your Explore agent unchanged" in msg
    assert Path(path).read_text().endswith("mine\n")


def test_plugin_agents_run_on_haiku_and_stay_read_only():
    root = Path(__file__).resolve().parents[1] / "plugins/usagetrim"
    for name in ("scout", "runner"):
        front = (root / f"agents/{name}.md").read_text().split("---")[1]
        assert f"name: {name}" in front and "model: haiku" in front
        assert "Edit" not in front and "Write" not in front
    style = (root / "output-styles/lean.md").read_text()
    assert "keep-coding-instructions: true" in style
