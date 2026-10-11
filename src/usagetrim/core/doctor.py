from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePath
from typing import Literal

from usagetrim.core.cache import DEFAULT_CACHE_DB


@dataclass
class DiagnosticItem:
    name: str
    status: Literal["ok", "warning", "missing"]
    message: str
    remedy: str | None = None


def check_python() -> DiagnosticItem:
    ver = sys.version_info
    if ver.major == 3 and ver.minor >= 11:
        return DiagnosticItem(
            name="Python Environment",
            status="ok",
            message=f"Python {ver.major}.{ver.minor}.{ver.micro} (>= 3.11 supported)",
        )
    return DiagnosticItem(
        name="Python Environment",
        status="warning",
        message=f"Python {ver.major}.{ver.minor}.{ver.micro} (Recommended >= 3.11)",
        remedy="Install Python 3.11 or higher",
    )


def check_cache_db() -> DiagnosticItem:
    cache_dir = os.environ.get("USAGETRIM_CACHE_DIR")
    db_path = Path(cache_dir) / "cache.db" if cache_dir else DEFAULT_CACHE_DB
    if not db_path.exists():
        return DiagnosticItem(
            name="CCR Cache Store",
            status="ok",
            message=f"{db_path} (Ready, initialised on first run)",
        )

    try:
        size_kb = db_path.stat().st_size / 1024
        with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            count = conn.execute("SELECT COUNT(*) FROM output_cache").fetchone()[0]
        return DiagnosticItem(
            name="CCR Cache Store",
            status="ok",
            message=f"{db_path} ({count} cached entries, {size_kb:.1f} KB)",
        )
    except Exception as e:
        return DiagnosticItem(
            name="CCR Cache Store",
            status="warning",
            message=f"Error reading {db_path}: {e}",
            remedy="Run `usagetrim cache clear` to reset the database",
        )


def check_claude_cli() -> DiagnosticItem:
    claude_bin = shutil.which("claude")
    if claude_bin:
        return DiagnosticItem(
            name="Claude Code CLI",
            status="ok",
            message=f"Found at {claude_bin}",
        )
    return DiagnosticItem(
        name="Claude Code CLI",
        status="warning",
        message="Not found in PATH",
        remedy="Install via `npm install -g @anthropic-ai/claude-code` if using Claude Code",
    )


def get_cursor_mcp_config_path() -> Path:
    return Path.home() / ".cursor" / "mcp.json"


def check_cursor_mcp() -> DiagnosticItem:
    cursor_mcp_path = get_cursor_mcp_config_path()
    if not cursor_mcp_path.exists():
        return DiagnosticItem(
            name="Cursor MCP Config",
            status="missing",
            message="No ~/.cursor/mcp.json found",
            remedy="Run `usagetrim install --cursor` to configure Cursor MCP",
        )

    try:
        data = json.loads(cursor_mcp_path.read_text(encoding="utf-8"))
        servers = data.get("mcpServers", {})
        if "usagetrim" in servers:
            return DiagnosticItem(
                name="Cursor MCP Config",
                status="ok",
                message="usagetrim registered in ~/.cursor/mcp.json",
            )
        return DiagnosticItem(
            name="Cursor MCP Config",
            status="missing",
            message="usagetrim not configured in ~/.cursor/mcp.json",
            remedy="Run `usagetrim install --cursor` to add usagetrim to Cursor",
        )
    except Exception as e:
        return DiagnosticItem(
            name="Cursor MCP Config",
            status="warning",
            message=f"Invalid JSON in ~/.cursor/mcp.json: {e}",
            remedy="Fix syntax in ~/.cursor/mcp.json before retrying installation",
        )


def get_windsurf_mcp_config_path() -> Path:
    return Path.home() / ".codeium" / "windsurf" / "mcp_config.json"


def check_windsurf_mcp() -> DiagnosticItem:
    cfg_file = get_windsurf_mcp_config_path()
    if not cfg_file.exists():
        return DiagnosticItem(
            name="Windsurf MCP Config",
            status="missing",
            message=f"No {cfg_file.name} found in ~/.codeium/windsurf",
            remedy="Run `usagetrim install --windsurf` to configure Windsurf MCP",
        )

    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        servers = data.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcpServers must be an object")
        if "usagetrim" in servers:
            entry = servers["usagetrim"]
            if not isinstance(entry, dict):
                raise ValueError("usagetrim entry must be an object")
            if entry.get("enabled") is False or entry.get("disabled") is True:
                return DiagnosticItem(
                    name="Windsurf MCP Config",
                    status="warning",
                    message="usagetrim is configured but disabled",
                    remedy="Enable usagetrim in Windsurf MCP settings",
                )
            if not any(
                isinstance(entry.get(key), str) and entry[key].strip() for key in ("command", "url")
            ):
                raise ValueError("usagetrim entry requires a command or URL")
            args = entry.get("args", [])
            if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
                raise ValueError("usagetrim args must be a list of strings")
            return DiagnosticItem(
                name="Windsurf MCP Config",
                status="ok",
                message=f"usagetrim configured in {cfg_file.name}; connection not tested",
            )
        return DiagnosticItem(
            name="Windsurf MCP Config",
            status="missing",
            message="usagetrim not in mcpServers",
            remedy="Run `usagetrim install --windsurf` to register usagetrim",
        )
    except Exception as e:
        return DiagnosticItem(
            name="Windsurf MCP Config",
            status="warning",
            message=f"Invalid JSON in {cfg_file}: {e}",
            remedy="Run `usagetrim install --windsurf` to repair config",
        )


def _cc_alias(content: str) -> str | None:
    """Read literal alias declarations without evaluating shell configuration."""
    result = None
    for line in content.splitlines():
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            continue
        if not words or words[0] != "alias":
            continue
        for index, word in enumerate(words[1:], 1):
            if word.startswith("cc="):
                result = word.partition("=")[2]
            elif word == "cc" and index + 1 < len(words):
                result = words[index + 1]
    return result


def check_shell_alias() -> DiagnosticItem:
    shell = os.environ.get("SHELL", "")
    if "fish" in shell:
        rc_path = Path.home() / ".config" / "fish" / "config.fish"
        rel_path = "~/.config/fish/config.fish"
    elif "zsh" in shell:
        rc_path = Path.home() / ".zshrc"
        rel_path = "~/.zshrc"
    else:
        rc_path = Path.home() / ".bashrc"
        rel_path = "~/.bashrc"

    if rc_path.exists():
        content = rc_path.read_text(encoding="utf-8", errors="ignore")
        alias = _cc_alias(content)
        try:
            command = shlex.split(alias) if alias else []
        except ValueError:
            command = []
        if command and Path(command[0]).name == "usagetrim":
            return DiagnosticItem(
                name="Shell Alias (`cc`)",
                status="ok",
                message=f"Alias configured in {rel_path}",
            )
        if alias is not None:
            return DiagnosticItem(
                name="Shell Alias (`cc`)",
                status="warning",
                message=f"The existing cc alias in {rel_path} is not a UsageTrim shortcut",
                remedy="Keep your existing alias or choose a different shortcut for UsageTrim",
            )
    return DiagnosticItem(
        name="Shell Alias (`cc`)",
        status="missing",
        message=f"No `cc` alias found in {rel_path}",
        remedy=f"Run `usagetrim install --alias` to add shortcut to {rel_path}",
    )


def get_claude_desktop_config_path() -> Path:
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "Claude" / "claude_desktop_config.json"
    else:
        config_home = os.environ.get("XDG_CONFIG_HOME", "")
        base = Path(config_home) if config_home else Path.home() / ".config"
        return base / "Claude" / "claude_desktop_config.json"


def check_claude_desktop_mcp() -> DiagnosticItem:
    cfg_file = get_claude_desktop_config_path()
    platform_label = (
        "macOS" if sys.platform == "darwin" else ("Windows" if sys.platform == "win32" else "Linux")
    )
    name = f"Claude Desktop ({platform_label})"
    if not cfg_file.exists():
        return DiagnosticItem(
            name=name,
            status="missing",
            message=f"Config file not found in {cfg_file.parent}",
            remedy="Run `usagetrim install --claude-desktop` to configure Claude Desktop",
        )
    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        if "usagetrim" in data.get("mcpServers", {}):
            return DiagnosticItem(
                name=name,
                status="ok",
                message=f"usagetrim registered in {cfg_file.name}",
            )
        return DiagnosticItem(
            name=name,
            status="missing",
            message="usagetrim not in mcpServers",
            remedy="Run `usagetrim install --claude-desktop` to register usagetrim",
        )
    except Exception as e:
        return DiagnosticItem(
            name=name,
            status="warning",
            message=f"Error reading config: {e}",
        )


def check_chatgpt_desktop() -> DiagnosticItem:
    return DiagnosticItem(
        name="ChatGPT Desktop",
        status="warning",
        message="This installer does not support local stdio MCP in the regular ChatGPT app.",
        remedy="Use the separate Codex app or CLI for UsageTrim's local MCP server.",
    )


def _codex_available() -> bool:
    return bool(
        shutil.which("codex")
        or Path("/Applications/Codex.app").exists()
        or (Path.home() / "Applications" / "Codex.app").exists()
    )


def check_codex_mcp(config_file: Path | None = None) -> DiagnosticItem:
    """Inspect local registration only; this does not establish MCP connectivity."""
    installed = _codex_available()
    cfg = config_file or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    prefix = "Codex detected" if installed else "Codex app/CLI not detected"
    try:
        data = tomllib.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
        servers = data.get("mcp_servers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcp_servers must be a table")
        server = servers.get("usagetrim")
        if server is None:
            return DiagnosticItem(
                name="Codex MCP Config",
                status="missing",
                message=f"{prefix}; UsageTrim is not registered in {cfg}.",
                remedy="Run `usagetrim install --codex` to configure Codex Desktop MCP",
            )
        if not isinstance(server, dict) or not isinstance(server.get("command"), str):
            raise ValueError("usagetrim must be a local server table with a command")
        if server.get("enabled") is False:
            return DiagnosticItem(
                name="Codex MCP Config",
                status="warning",
                message=f"{prefix}; UsageTrim is registered but disabled in {cfg}.",
            )
        if PurePath(server["command"]).name == "uvx" and server.get("args", [])[:1] == [
            "usagetrim"
        ]:
            raise ValueError(
                "uvx usagetrim needs a PyPI release, which does not exist yet; use "
                "`uvx --from git+https://github.com/00200200/usagetrim usagetrim` "
                "or your local installation"
            )
        return DiagnosticItem(
            name="Codex MCP Config",
            status="ok" if installed else "warning",
            message=f"{prefix}; UsageTrim is registered in {cfg}. Runtime connectivity is untested.",
        )
    except (OSError, ValueError, TypeError) as exc:
        return DiagnosticItem(
            name="Codex MCP Config",
            status="warning",
            message=f"{prefix}; cannot validate {cfg}: {exc}",
        )


def get_codex_config_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"


def _local_mcp_command(profile: str | None = None) -> dict[str, object]:
    """Choose an installed runtime, never fetch the colliding PyPI project."""
    base_args = ["mcp", "--profile", profile] if profile else ["mcp"]
    executable = shutil.which("usagetrim")
    if executable:
        return {"command": str(Path(executable).absolute()), "args": base_args}
    # Preserve the venv path: resolving a symlink to the base interpreter can
    # lose the installed package. -I proves this works without cwd/PYTHONPATH.
    interpreter = Path(sys.executable).absolute()
    try:
        probe = subprocess.run(
            [
                str(interpreter),
                "-I",
                "-c",
                "from usagetrim.cli import main; from usagetrim.core.cache import ContextCache",
            ],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if probe.returncode == 0:
            return {"command": str(interpreter), "args": ["-m", "usagetrim.cli", *base_args]}
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise ValueError(
        "No usable local UsageTrim installation found. Install this repository into a persistent "
        "environment and add its usagetrim executable to PATH; do not install the unrelated PyPI package."
    )


def _backup_config(cfg_file: Path) -> Path:
    """Copy the file next to itself as ``<name>.pre-usagetrim-<timestamp>-<random>``."""
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    with tempfile.NamedTemporaryFile(
        prefix=f"{cfg_file.name}.pre-usagetrim-{timestamp}-",
        dir=cfg_file.parent,
        delete=False,
    ) as backup:
        backup_path = Path(backup.name)
    shutil.copy2(cfg_file, backup_path)
    return backup_path


def _write_text_atomic(cfg_file: Path, text: str) -> None:
    """Replace the file via a staged copy; an existing file is backed up and keeps its mode."""
    staged: Path | None = None
    try:
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=cfg_file.name + ".tmp-",
            dir=cfg_file.parent,
            delete=False,
        ) as temporary:
            staged = Path(temporary.name)
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
        if cfg_file.exists():
            os.chmod(staged, cfg_file.stat().st_mode & 0o777)
            _backup_config(cfg_file)
        staged.replace(cfg_file)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _configure_local_mcp(cfg_file: Path, profile: str | None = None) -> tuple[bool, str]:
    """Preserve settings, reject malformed files, and back up each actual change."""
    try:
        # Read and validate before creating directories, backups, or replacements.
        data = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else {}
        if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
            raise ValueError("configuration and mcpServers must be JSON objects")
        servers = data.setdefault("mcpServers", {})
        previous = servers.get("usagetrim", {})
        if not isinstance(previous, dict):
            raise ValueError("existing usagetrim server must be a JSON object")
        if "url" in previous or previous.get("type", "stdio") != "stdio":
            raise ValueError(
                "existing usagetrim server uses another transport; update it explicitly"
            )
        updated = {**previous, **_local_mcp_command(profile=profile)}
        if previous == updated:
            return True, str(cfg_file)
        servers["usagetrim"] = updated
        _write_text_atomic(cfg_file, _dump_json_config(data))
        return True, str(cfg_file)
    except (OSError, ValueError, TypeError) as exc:
        return (
            False,
            f"Could not configure {cfg_file}: {exc}. Existing configuration was not overwritten.",
        )


def _dump_json_config(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def configure_claude_desktop_mcp(
    target_file: Path | None = None, profile: str | None = None
) -> tuple[bool, str]:
    return _configure_local_mcp(target_file or get_claude_desktop_config_path(), profile=profile)


# Blok kończy się na pierwszej tabeli spoza [mcp_servers.usagetrim] i jej podtabel
# (np. [mcp_servers.usagetrim-dev] to już inny serwer).
_CODEX_BLOCK_PATTERN = re.compile(
    r"(^|\n)\[mcp_servers\.usagetrim\]\n(?:(?!\n\[(?!mcp_servers\.usagetrim[.\]])).)*",
    re.DOTALL,
)
# Komentarze i puste linie na końcu bloku zwykle opisują następną tabelę.
_TRAILING_TRIVIA_PATTERN = re.compile(r"(?:\n[ \t]*(?:#[^\n]*)?)*\Z")
_BARE_TOML_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _toml_key(key: str) -> str:
    return key if _BARE_TOML_KEY.fullmatch(key) else json.dumps(key)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = ", ".join(f"{_toml_key(key)} = {_toml_value(item)}" for key, item in value.items())
        return "{ " + pairs + " }"
    raise ValueError(f"cannot write {type(value).__name__} values into [mcp_servers.usagetrim]")


def _codex_usagetrim_block(entry: dict) -> str:
    lines = ["[mcp_servers.usagetrim]"]
    lines += [
        f"{_toml_key(key)} = {_toml_value(value)}"
        for key, value in entry.items()
        if not isinstance(value, dict)
    ]
    for key, value in entry.items():
        if isinstance(value, dict):
            lines += ["", f"[mcp_servers.usagetrim.{_toml_key(key)}]"]
            lines += [f"{_toml_key(name)} = {_toml_value(item)}" for name, item in value.items()]
    return "\n".join(lines) + "\n"


def _rewrite_codex_entry(raw_text: str, entry: dict) -> str:
    """Swap only UsageTrim's table and prove every other setting parses back unchanged."""
    block = _codex_usagetrim_block(entry)
    match = _CODEX_BLOCK_PATTERN.search(raw_text)
    if match:
        trailing = _TRAILING_TRIVIA_PATTERN.search(match.group(0)).group(0).removeprefix("\n")
        before, after = raw_text[: match.start()] + match.group(1), raw_text[match.end() :]
        updated_text, untouched_text = before + block + trailing + after, before + trailing + after
    else:
        separator = "" if not raw_text else ("\n" if raw_text.endswith("\n") else "\n\n")
        updated_text, untouched_text = raw_text + separator + block, raw_text
    expected = tomllib.loads(untouched_text)
    expected.setdefault("mcp_servers", {})["usagetrim"] = entry
    if tomllib.loads(updated_text) != expected:
        raise ValueError("rewriting [mcp_servers.usagetrim] would change other settings")
    return updated_text


def configure_codex_mcp(
    target_file: Path | None = None, profile: str | None = None
) -> tuple[bool, str]:
    """Configure UsageTrim MCP in Codex config.toml safely and atomically."""
    cfg_file = target_file or get_codex_config_path()
    try:
        raw_text = cfg_file.read_text(encoding="utf-8") if cfg_file.exists() else ""
        data = tomllib.loads(raw_text) if raw_text else {}
        if not isinstance(data, dict):
            raise ValueError("configuration must be a TOML table")
        servers = data.get("mcp_servers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcp_servers must be a table")

        cmd_info = _local_mcp_command(profile=profile)
        previous = servers.get("usagetrim")
        previous = previous if isinstance(previous, dict) else {}
        updated = {**previous, **cmd_info}
        if previous == updated:
            return True, str(cfg_file)

        _write_text_atomic(cfg_file, _rewrite_codex_entry(raw_text, updated))
        return True, str(cfg_file)
    except (OSError, ValueError, TypeError) as exc:
        return (
            False,
            f"Could not configure {cfg_file}: {exc}. Existing configuration was not overwritten.",
        )


EXTENSION_FILES = (
    "manifest.json",
    "pyproject.toml",
    "src/server.py",
    "icon.png",
    "README.md",
    ".mcpbignore",
)


def _resource(wheel_rel: str, repo_rel: str) -> Path | None:
    here = Path(__file__).resolve()
    # Wheel installs carry resources inside the package; a checkout keeps them at the root.
    for candidate in (here.parents[1] / wheel_rel, here.parents[3] / repo_rel):
        if candidate.exists():
            return candidate
    return None


def _extension_source() -> Path | None:
    source = _resource("extensions/claude-desktop", "extensions/claude-desktop")
    return source if source and (source / "manifest.json").is_file() else None


def install_cheap_explore(agents_dir: Path | None = None) -> tuple[bool, str]:
    """Run Claude Code's Explore subagent on Haiku via a user-scope override.

    Since Claude Code v2.1.198 the built-in Explore inherits the main model, so
    exploration on Opus costs Opus usage. A user agent named ``Explore`` takes
    precedence over the built-in; an existing file is never overwritten.
    """
    source = _resource("agents/scout.md", "plugins/usagetrim/agents/scout.md")
    if source is None:
        return False, "The scout agent definition is missing from this install."
    text = source.read_text(encoding="utf-8").replace("name: scout\n", "name: Explore\n", 1)
    dest = (agents_dir or Path.home() / ".claude" / "agents") / "Explore.md"
    try:
        if dest.exists():
            if dest.read_text(encoding="utf-8") == text:
                return True, str(dest)
            return False, f"{dest} already exists; leaving your Explore agent unchanged."
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        return True, str(dest)
    except OSError as exc:
        return False, f"Could not write {dest}: {exc}"


def write_claude_desktop_extension(target_dir: Path | None = None) -> tuple[bool, str]:
    """Copy the Claude Desktop Extension (MCPB) bundle source for ``mcpb pack``.

    Releases attach a ready ``usagetrim-<version>.mcpb``; this keeps a local path
    for users who want to inspect or rebuild it. The bundle uses the ``uv``
    server type, so Claude Desktop installs the pinned package itself.
    """
    source = _extension_source()
    if source is None:
        return False, (
            "Extension bundle not found in this install. Download usagetrim.mcpb from "
            "https://github.com/00200200/usagetrim/releases instead."
        )
    dest = target_dir or (Path.home() / ".usagetrim" / "extensions" / "claude-desktop")
    try:
        for name in EXTENSION_FILES:
            if (source / name).is_file():
                (dest / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / name, dest / name)
        return True, str(dest)
    except OSError as exc:
        return False, f"Could not write Claude Desktop extension: {exc}"


def run_all_diagnostics() -> list[DiagnosticItem]:
    return [
        check_python(),
        check_cache_db(),
        check_claude_cli(),
        check_claude_desktop_mcp(),
        check_chatgpt_desktop(),
        check_codex_mcp(),
        check_cursor_mcp(),
        check_windsurf_mcp(),
        check_shell_alias(),
    ]


def configure_cursor_mcp(
    target_file: Path | None = None, profile: str = "coding"
) -> tuple[bool, str]:
    return _configure_local_mcp(
        target_file or (Path.home() / ".cursor" / "mcp.json"), profile=profile
    )


def configure_windsurf_mcp(
    target_file: Path | None = None, profile: str = "coding"
) -> tuple[bool, str]:
    return _configure_local_mcp(target_file or get_windsurf_mcp_config_path(), profile=profile)


def configure_shell_alias(target_file: Path | None = None) -> tuple[bool, str]:
    if target_file:
        rc_path = target_file
        is_fish = "fish" in str(target_file)
    else:
        shell = os.environ.get("SHELL", "")
        if "fish" in shell:
            rc_path = Path.home() / ".config" / "fish" / "config.fish"
            is_fish = True
        elif "zsh" in shell:
            rc_path = Path.home() / ".zshrc"
            is_fish = False
        else:
            rc_path = Path.home() / ".bashrc"
            is_fish = False

    rc_path.parent.mkdir(parents=True, exist_ok=True)
    alias_line = "\nalias cc 'usagetrim run --'\n" if is_fish else '\nalias cc="usagetrim run --"\n'

    if rc_path.exists():
        content = rc_path.read_text(encoding="utf-8", errors="ignore")
        if _cc_alias(content) is not None:
            return False, f"Alias already present in {rc_path}"
        with open(rc_path, "a", encoding="utf-8") as f:
            f.write(alias_line)
    else:
        rc_path.write_text(alias_line, encoding="utf-8")

    return True, str(rc_path)


HANDSHAKE_TIMEOUT_SECONDS = 2.0


@dataclass
class HandshakeResult:
    status: Literal["ok", "failed", "skipped"]
    message: str


@dataclass
class McpClient:
    name: str
    get_config_path: Callable[[], Path]
    config_format: Literal["json", "toml"]


@dataclass
class ClientRepairReport:
    client: str
    config_path: Path
    applied: bool
    actions: list[str] = field(default_factory=list)
    server_entry: dict | None = None
    error: str | None = None


# Lambdy zamiast referencji do funkcji, żeby podmiana get_*_config_path (np. w testach) była widoczna.
MCP_CLIENTS = (
    McpClient("Claude Desktop", lambda: get_claude_desktop_config_path(), "json"),
    McpClient("Cursor", lambda: get_cursor_mcp_config_path(), "json"),
    McpClient("Windsurf", lambda: get_windsurf_mcp_config_path(), "json"),
    McpClient("Codex", lambda: get_codex_config_path(), "toml"),
)


def _skip_json_trivia(text: str, index: int) -> int:
    while index < len(text):
        if text[index].isspace():
            index += 1
        elif text.startswith("//", index):
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = len(text) if end == -1 else end + 2
        else:
            break
    return index


@dataclass
class JsonRepair:
    text: str
    removed_comments: bool


def repair_json_text(raw: str) -> JsonRepair | None:
    """Fix only unambiguous damage: BOM, comments, and trailing commas.

    Returns ``None`` when the result is still not a JSON object, so nothing is guessed.
    """
    removed_comments = False
    text = raw.lstrip("\ufeff")
    output: list[str] = []
    index = 0
    in_string = False
    while index < len(text):
        char = text[index]
        if in_string:
            if char == "\\":
                output.append(text[index : index + 2])
                index += 2
                continue
            in_string = char != '"'
            output.append(char)
            index += 1
            continue
        if char == '"':
            in_string = True
        elif text.startswith("//", index) or text.startswith("/*", index):
            if text.startswith("/*", index) and text.find("*/", index + 2) == -1:
                return None
            removed_comments = True
            index = _skip_json_trivia(text, index)
            continue
        elif char == ",":
            next_index = _skip_json_trivia(text, index + 1)
            if next_index < len(text) and text[next_index] in "}]":
                index += 1
                continue
        output.append(char)
        index += 1
    repaired = "".join(output)
    try:
        data = json.loads(repaired)
    except ValueError:
        return None
    return JsonRepair(repaired, removed_comments) if isinstance(data, dict) else None


def _is_local_stdio(entry: dict) -> bool:
    return "url" not in entry and entry.get("type", "stdio") == "stdio"


def _is_stale_command(entry: dict) -> bool:
    command, args = entry.get("command"), entry.get("args", [])
    if not isinstance(command, str) or not command.strip():
        return True
    # uvx pobrałby niepowiązany pakiet z PyPI zamiast tego projektu.
    if PurePath(command).name == "uvx" and isinstance(args, list) and args[:1] == ["usagetrim"]:
        return True
    # Zmienne rozwijane przez klienta (~, ${userHome}, ${env:X}) są poza naszą oceną.
    if command.startswith("~") or "${" in command:
        return False
    if PurePath(command).name != command:
        return not _command_path_exists(command)
    return shutil.which(command) is None


def _command_path_exists(command: str) -> bool:
    if Path(command).exists():
        return True
    if os.name != "nt":
        return False
    extensions = os.environ.get("PATHEXT", ".EXE").split(os.pathsep)
    return any(Path(command + extension).exists() for extension in extensions if extension)


def _profile_from_args(args: object) -> str | None:
    if isinstance(args, list) and "--profile" in args:
        position = args.index("--profile") + 1
        if position < len(args) and isinstance(args[position], str):
            return args[position]
    return None


def _replacement_for_stale(entry: dict | None) -> dict[str, object] | None:
    """Return the active command for a stale local entry, or ``None`` when nothing would change."""
    if entry is None or not _is_local_stdio(entry) or not _is_stale_command(entry):
        return None
    cmd_info = _local_mcp_command(profile=_profile_from_args(entry.get("args")))
    if all(entry.get(key) == value for key, value in cmd_info.items()):
        return None
    return cmd_info


def _repair_json_config(raw: str, actions: list[str]) -> tuple[str, dict | None]:
    try:
        data = json.loads(raw)
        syntax_action = None
    except ValueError:
        repair = repair_json_text(raw)
        if repair is None:
            raise ValueError("JSON syntax cannot be repaired automatically") from None
        data = json.loads(repair.text)
        syntax_action = "repaired JSON syntax" + (
            " (comments removed)" if repair.removed_comments else ""
        )
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise ValueError("configuration and mcpServers must be JSON objects")
    entry = data.get("mcpServers", {}).get("usagetrim")
    # Plików bez wpisu UsageTrim nie przepisujemy — to nie nasza konfiguracja.
    if entry is None:
        return raw, None
    if not isinstance(entry, dict):
        raise ValueError("usagetrim server must be a JSON object")
    if syntax_action:
        actions.append(syntax_action)
    cmd_info = _replacement_for_stale(entry)
    if cmd_info is not None:
        actions.append(f"replaced stale command {entry.get('command')!r}")
        entry.update(cmd_info)
    if syntax_action or cmd_info is not None:
        return _dump_json_config(data), entry
    return raw, entry


def _repair_toml_config(raw: str, actions: list[str]) -> tuple[str, dict | None]:
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        return _rebuild_broken_codex_table(raw, exc, actions)
    servers = data.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcp_servers must be a table")
    entry = servers.get("usagetrim")
    if entry is not None and not isinstance(entry, dict):
        raise ValueError("usagetrim must be a server table")
    cmd_info = _replacement_for_stale(entry)
    if cmd_info is None:
        return raw, entry
    repaired_entry = {**entry, **cmd_info}
    updated = _rewrite_codex_entry(raw, repaired_entry)
    actions.append(f"replaced stale command {entry.get('command')!r}")
    return updated, repaired_entry


def _rebuild_broken_codex_table(
    raw: str, error: tomllib.TOMLDecodeError, actions: list[str]
) -> tuple[str, dict]:
    """Rebuild an unparsable UsageTrim table only when nothing but command/args would be lost."""
    outside_error = ValueError(f"TOML syntax error outside UsageTrim's table: {error}")
    usagetrim_block = _CODEX_BLOCK_PATTERN.search(raw)
    if not usagetrim_block:
        raise outside_error
    block_text = usagetrim_block.group(0)
    other_keys = set(re.findall(r"(?m)^[ \t]*([^\s#=\[\]]+)[ \t]*=", block_text)) - {
        "command",
        "args",
    }
    if other_keys or len(re.findall(r"(?m)^[ \t]*\[", block_text)) > 1:
        raise ValueError(
            "broken [mcp_servers.usagetrim] table holds other settings (e.g. env); fix it manually"
        )
    profile_match = re.search(r"""["']--profile["']\s*,\s*["']([\w-]+)["']""", block_text)
    entry = dict(_local_mcp_command(profile=profile_match.group(1) if profile_match else None))
    try:
        updated = _rewrite_codex_entry(raw, entry)
    except tomllib.TOMLDecodeError:
        raise outside_error from None
    actions.append("rebuilt invalid [mcp_servers.usagetrim] table")
    return updated, entry


def _repair_permissions(entry: dict | None, actions: list[str], apply: bool) -> None:
    if os.name == "nt":
        return
    command = entry.get("command") if entry else None
    if not isinstance(command, str):
        return
    executable = Path(command)
    if executable.is_absolute() and executable.is_file() and not os.access(executable, os.X_OK):
        actions.append(f"made {executable.name} executable (0o755)")
        if apply:
            os.chmod(executable, (executable.stat().st_mode & 0o777) | 0o755)


def repair_client(client: McpClient, *, apply: bool) -> ClientRepairReport:
    """Plan (``apply=False``) or apply repairs; every content change is backed up first."""
    cfg_file = client.get_config_path()
    report = ClientRepairReport(client=client.name, config_path=cfg_file, applied=apply)
    if not cfg_file.exists():
        return report
    try:
        raw = cfg_file.read_text(encoding="utf-8")
        repair = _repair_json_config if client.config_format == "json" else _repair_toml_config
        updated, entry = repair(raw, report.actions)
        # chmod przed zapisem: gdy się nie uda, treść configu na pewno pozostaje nietknięta.
        _repair_permissions(entry, report.actions, apply)
        if apply and updated != raw:
            _write_text_atomic(cfg_file, updated)
        report.server_entry = entry
    except (OSError, ValueError, TypeError) as exc:
        report.actions.clear()
        report.error = f"{exc}; configuration was not changed"
    return report


def probe_mcp_handshake(
    command: str,
    args: list[str],
    env: dict | None = None,
    timeout: float = HANDSHAKE_TIMEOUT_SECONDS,
) -> HandshakeResult:
    """Start the server, send ``initialize`` and close stdin; never waits past ``timeout``."""
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "usagetrim-doctor", "version": "1"},
        },
    }
    process_env = {**os.environ, **{key: str(value) for key, value in env.items()}} if env else None
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            [shutil.which(command) or command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=process_env,
            # Własna grupa procesów pozwala na POSIX zabić też procesy potomne wrappera.
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        return HandshakeResult("failed", f"cannot start server: {exc}")
    try:
        stdout, stderr = process.communicate(json.dumps(request) + "\n", timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        return HandshakeResult("failed", f"no initialize response within {timeout:g}s")
    elapsed_ms = (time.monotonic() - started) * 1000
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or message.get("id") != 1:
            continue
        result = message.get("result")
        server_info = result.get("serverInfo") if isinstance(result, dict) else None
        if isinstance(server_info, dict) and server_info.get("name") == "usagetrim":
            return HandshakeResult("ok", f"handshake completed in {elapsed_ms:.0f} ms")
        return HandshakeResult("failed", "unexpected initialize response")
    stderr_lines = stderr.strip().splitlines()
    detail = f": {stderr_lines[-1]}" if stderr_lines else ""
    return HandshakeResult(
        "failed", f"no initialize response (exit code {process.returncode}){detail}"
    )


def _kill_process_tree(process: subprocess.Popen) -> None:
    """Kill the server and its children without waiting on pipes they may still hold open."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                timeout=5,
                check=False,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def probe_server_entry(
    entry: dict | None, timeout: float = HANDSHAKE_TIMEOUT_SECONDS
) -> HandshakeResult:
    """Probe only UsageTrim's own entry; other configured servers are never started."""
    if entry is None:
        return HandshakeResult("skipped", "usagetrim not registered")
    if not _is_local_stdio(entry):
        return HandshakeResult("skipped", "remote transport")
    if entry.get("disabled") is True or entry.get("enabled") is False:
        return HandshakeResult("skipped", "disabled")
    command, args = entry.get("command"), entry.get("args", [])
    if not isinstance(command, str) or not command.strip():
        return HandshakeResult("failed", "missing command")
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return HandshakeResult("failed", "args must be a list of strings")
    env = entry.get("env")
    return probe_mcp_handshake(command, args, env if isinstance(env, dict) else None, timeout)
