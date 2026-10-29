"""Characterize current entry points, not the proposed execution contract."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PRODUCER = ROOT / "tests/fixtures/execution/producer.py"


@pytest.fixture
def execution_env():
    # Existing autouse fixture provides isolated cache/state for child processes.
    return os.environ | {
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "NO_COLOR": "1",
    }


def producer(*arguments):
    return [sys.executable, str(PRODUCER), *arguments]


def shell_command(argv):
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def cli(argv, env):
    return subprocess.run(
        [sys.executable, "-m", "usagetrim.cli", "run", "--", *argv],
        capture_output=True,
        env=env,
        timeout=15,
    )


def mcp(command, env):
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "usagetrim_exec", "arguments": {"command": command}},
    }
    process = subprocess.run(
        [sys.executable, "-m", "usagetrim.cli", "mcp", "--profile", "full"],
        input=(json.dumps(request) + "\n").encode(),
        capture_output=True,
        env=env,
        timeout=15,
    )
    assert process.returncode == 0, process.stderr.decode(errors="replace")
    assert process.stderr == b""
    return json.loads(process.stdout)["result"]


@pytest.mark.parametrize(
    "arguments,expected",
    [
        (["--mode", "stdout"], b"payload\n"),
        (["--mode", "stderr"], b"payload\n"),
        (["--mode", "mixed"], b"out1\nerr1\nout2\n"),
        (["--mode", "empty"], b""),
        (["--no-newline"], b"payload"),
        (["--mode", "invalid-utf8"], "bad:\ufffd\ufffd\n".encode()),
        (["--mode", "crlf"], b"one\ntwo\n"),
        (["--mode", "cr"], b"work1\nwork2\ndone\n"),
        (["--mode", "ansi"], b"\x1b[31mred\x1b[0m\n"),
        (["--mode", "line", "--bytes", "2048", "--no-newline"], b"x" * 2048),
    ],
)
def test_current_combined_text_and_mcp_footer(execution_env, arguments, expected):
    result = cli(producer(*arguments), execution_env)
    assert result.returncode == 0 and result.stderr == b""
    assert result.stdout == expected + (b"\n" if expected and not expected.endswith(b"\n") else b"")
    tool = mcp(shell_command(producer(*arguments)), execution_env)
    assert tool["isError"] is False
    assert tool["content"] == [{"type": "text", "text": expected.decode() + "\n[exit code: 0]"}]


def test_current_nonzero_exit_is_not_automatically_a_tool_error(execution_env):
    argv = producer("--text", "failed", "--exit-code", "7")
    result = cli(argv, execution_env)
    assert result.returncode == 7 and result.stdout == b"failed\n" and not result.stderr
    tool = mcp(shell_command(argv), execution_env)
    assert tool["isError"] is False
    assert tool["content"][0]["text"] == "failed\n\n[exit code: 7]"


def test_current_tool_error_depends_on_text_prefix_even_for_exit_zero(execution_env):
    tool = mcp(shell_command(producer("--text", "Error: child diagnostic")), execution_env)
    assert tool["isError"] is True
    assert tool["content"][0]["text"] == "Error: child diagnostic\n\n[exit code: 0]"


def test_cli_preserves_literal_argv(execution_env, tmp_path):
    marker = tmp_path / "must-not-exist"
    values = [
        "space name",
        "",
        "a\nb",
        "'\"\\",
        f"; touch {marker}",
        "$(echo expanded)",
        "*.py",
        "-flag",
        "left && right",
    ]
    result = cli(producer("--mode", "argv", "--", *values), execution_env)
    assert result.returncode == 0 and not result.stderr
    assert json.loads(result.stdout) == values
    assert not marker.exists()


def test_mcp_keeps_shell_command_semantics(execution_env):
    command = (
        shell_command(producer("--text", "left", "--no-newline"))
        + " && "
        + shell_command(producer("--mode", "stderr", "--text", "right", "--no-newline"))
    )
    tool = mcp(command, execution_env)
    assert tool["isError"] is False
    assert tool["content"][0]["text"] == "leftright\n[exit code: 0]"


def test_current_failed_start_vs_shell_command_not_found(execution_env, tmp_path):
    missing = str(tmp_path / "executable-that-does-not-exist")
    result = cli([missing], execution_env)
    assert result.returncode == 1 and result.stdout == b""
    assert b"FileNotFoundError" in result.stderr
    tool = mcp(shell_command([missing]), execution_env)
    text = tool["content"][0]["text"]
    assert "[exit code:" in text and "[exit code: 0]" not in text
    if os.name != "nt":
        assert text.endswith("[exit code: 127]") and tool["isError"] is False


def test_producer_byte_count_is_exact_and_deterministic(execution_env):
    arguments = producer("--mode", "records", "--bytes", "4097")
    results = [
        subprocess.run(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=execution_env,
            check=True,
            timeout=15,
        ).stdout
        for _ in range(2)
    ]
    assert results[0] == results[1] and len(results[0]) == 4097
