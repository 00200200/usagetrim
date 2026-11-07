from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from usagetrim.core import clip
from usagetrim.core.cache import ContextCache
from usagetrim.core.clip import compact_text, get_clipboard, set_clipboard


def test_compact_text_short_passthrough():
    short = "Just a short message"
    res = compact_text(short)
    assert res.text == short
    assert res.original_tokens == res.compacted_tokens
    assert res.reduction_pct == 0.0
    assert res.content_type == "plain"
    assert res.ref_id is None


def test_compact_text_json():
    data = {
        "items": [{"id": i, "name": f"item_{i}", "details": "x" * 50} for i in range(20)],
        "metadata": {"total": 20, "page": 1},
    }
    raw = json.dumps(data)
    res = compact_text(raw, budget=1000)
    assert res.content_type == "json"
    assert res.compacted_tokens < res.original_tokens
    assert res.saved_tokens > 0
    assert res.ref_id is not None
    cache = ContextCache()
    assert cache.retrieve(res.ref_id) == raw


def test_compact_text_diff():
    diff_lines = [
        "diff --git a/uv.lock b/uv.lock",
        "index 1234567..89abcde 100644",
        "--- a/uv.lock",
        "+++ b/uv.lock",
        "@@ -1,5 +1,6 @@",
    ] + [f"-line_{i} = {i}" for i in range(100)]
    raw = "\n".join(diff_lines)
    res = compact_text(raw, budget=1000)
    assert res.content_type == "diff"
    assert "uv.lock" in res.text
    assert res.ref_id is not None


def test_compact_text_pytest():
    noisy = (
        "============================= test session starts ==============================\n"
        "collected 39 items\n\n"
        + "\n".join(f"tests/test_file_{i}.py::test_pass PASSED [ {i}%]" for i in range(1, 40))
        + "\n=========================== short test summary info ============================\n"
        + "============================== 39 passed in 0.5s ===============================\n"
    )
    res = compact_text(noisy, budget=1000)
    assert res.content_type == "pytest"
    assert res.compacted_tokens < res.original_tokens


def test_compact_text_traceback_recursion():
    lines = [
        "Traceback (most recent call last):\n",
        '  File "runner.py", line 10, in run\n',
        "    recurse(0)\n",
    ]
    for _ in range(20):
        lines.append('  File "runner.py", line 5, in recurse\n')
        lines.append("    return recurse(n + 1)\n")
    lines.append("RecursionError: maximum recursion depth exceeded\n")
    raw = "".join(lines)

    res = compact_text(raw, budget=1000)
    assert res.content_type == "traceback"
    assert "identical recursive frame repeated" in res.text
    assert "RecursionError" in res.text


def test_compact_text_repeated_lines():
    noisy = ("INFO: processing item\n" * 50) + "DONE\n"
    res = compact_text(noisy, budget=1000)
    assert "preceding line repeated" in res.text
    assert "DONE" in res.text


def test_compact_text_secret_scrubbing():
    secret = "ghp_" + "A" * 36
    noisy = f"API_KEY={secret}\n" + ("line of padding text to make it past threshold\n" * 30)
    res = compact_text(noisy, budget=1000)
    assert secret not in res.text
    assert "[REDACTED_API_KEY]" in res.text or "[REDACTED" in res.text


def test_compact_text_budget_truncation():
    huge = "".join(f"Unique diagnostic row {i} with payload data {i * 7}\n" for i in range(400))
    res = compact_text(huge, budget=200)
    assert res.compacted_tokens <= 200
    assert "lines omitted by UsageTrim to fit 200 token budget" in res.text
    assert res.ref_id is not None
    recovered = ContextCache().retrieve(res.ref_id)
    assert "Unique diagnostic row 0" in recovered
    assert "Unique diagnostic row 399" in recovered


def test_clipboard_darwin_mock():
    with patch("platform.system", return_value="Darwin"):
        with patch("subprocess.check_output", return_value="copied content"):
            assert get_clipboard() == "copied content"

        with patch("shutil.which", return_value="/usr/bin/pbcopy"):
            with patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
                assert set_clipboard("new content") is True
                assert run.call_args.args[0] == ["/usr/bin/pbcopy"]
                assert run.call_args.kwargs["input"] == b"new content"


def test_clipboard_headless_linux(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    with patch("platform.system", return_value="Linux"):
        assert get_clipboard() == ""
        assert set_clipboard("test") is False


@pytest.mark.parametrize(
    "system,wayland,display,tools,expected",
    [
        ("Darwin", "", "", {"pbcopy"}, ["pbcopy"]),
        ("Linux", "wayland-0", "", {"wl-copy"}, ["wl-copy"]),
        ("Linux", "wayland-0", ":0", {"wl-copy", "xclip"}, ["wl-copy"]),
        ("Linux", "", ":0", {"xclip"}, ["xclip", "-selection", "clipboard"]),
        ("Linux", "", ":0", {"xsel"}, ["xsel", "--clipboard", "--input"]),
        ("Linux", "wayland-0", ":0", {"xsel"}, ["xsel", "--clipboard", "--input"]),
    ],
)
def test_clipboard_native_argv_and_utf8(monkeypatch, system, wayland, display, tools, expected):
    monkeypatch.setattr(clip.platform, "system", lambda: system)
    monkeypatch.setenv("WAYLAND_DISPLAY", wayland)
    monkeypatch.setenv("DISPLAY", display)
    monkeypatch.setattr(
        clip.shutil, "which", lambda name: "/tools/" + name if name in tools else None
    )
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(clip.subprocess, "run", run)
    text = "żółć\n$(touch PWNED); 'quoted' *"
    assert set_clipboard(text) is True
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["/tools/" + expected[0], *expected[1:]]
    assert kwargs["input"] == text.encode("utf-8")
    assert kwargs["timeout"] == 2 and not kwargs.get("shell", False)
    assert kwargs["stdout"] == subprocess.DEVNULL and kwargs["stderr"] == subprocess.DEVNULL


@pytest.mark.parametrize("failure", ["missing", "permission", "timeout", "nonzero"])
def test_clipboard_linux_helper_failures_fall_back(monkeypatch, failure):
    monkeypatch.setattr(clip.platform, "system", lambda: "Linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(clip.shutil, "which", lambda name: "/tools/" + name)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv[0])
        if argv[0].endswith("wl-copy"):
            if failure == "missing":
                raise FileNotFoundError("tool disappeared")
            if failure == "permission":
                raise PermissionError("denied")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 2)
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(clip.subprocess, "run", run)
    assert set_clipboard("tc_test") is True
    assert calls == ["/tools/wl-copy", "/tools/xclip"]


@pytest.mark.parametrize("system", ["Linux", "Darwin", "Windows"])
def test_clipboard_missing_tools_is_controlled(monkeypatch, system):
    monkeypatch.setattr(clip.platform, "system", lambda: system)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(clip.shutil, "which", lambda name: None)
    monkeypatch.setattr(clip.subprocess, "run", lambda *a, **k: pytest.fail("no tool to execute"))
    assert set_clipboard("tc_test") is False


def test_clipboard_all_helpers_fail_without_reporting_success(monkeypatch):
    monkeypatch.setattr(clip.platform, "system", lambda: "Linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(clip.shutil, "which", lambda name: "/tools/" + name)
    monkeypatch.setattr(clip.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    assert set_clipboard("tc_test") is False
