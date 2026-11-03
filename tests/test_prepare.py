import io
import json
import os
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from usagetrim.cli import app
from usagetrim.core.cache import ContextCache
from usagetrim.core.monitor import Monitor
from usagetrim.core.prepare import MAX_INPUT_BYTES, prepare_text
from usagetrim.metrics.tokenizer import count_tokens


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Keep this rule.\n\nDo not change public APIs.\n",
        "```python\nx = '  '\n```\n",
        "Zażółć 🧪\n",
    ],
)
def test_conservative_preserves_unfamiliar_input(text):
    result = prepare_text(text)
    assert result["text"] == text
    assert result["difference"] == 0
    assert result["delivery"] == "preview"


def test_preview_preserves_diagnostics_recovers_original_and_does_not_record_savings(tmp_path):
    original = (
        "Downloading unchanged dependency\n" * 250
        + "ERROR: installation failed\n"
        + "detail\n" * 60
    )
    service = Monitor([Path(os.environ["USAGETRIM_CACHE_DIR"])], tmp_path / "collector")
    before = service.snapshot()["today"]
    result = service.dispatch({"method": "prepare", "text": original})
    assert result["difference"] > 0
    assert "ERROR: installation failed\n" + "detail\n" * 60 in result["text"]
    ref = re.search(r"usagetrim retrieve (\S+)", result["text"])[1].rstrip("]")
    assert ContextCache().retrieve(ref) == original
    assert result["after"] == count_tokens(result["text"]).openai
    assert service.snapshot()["today"] == before
    assert "Downloading unchanged" not in json.dumps(service.dispatch({"method": "export"}))


def test_summary_is_opt_in_and_small_input_does_not_grow():
    short = "User: Keep the public API unchanged."
    assert prepare_text(short, mode="summary")["text"] == short
    text = "User: Build a parser.\nAssistant: " + "Exploring possible approaches. " * 500
    default = prepare_text(text)
    summary = prepare_text(text, mode="summary", budget=800)
    assert default["text"] == text
    assert summary["after"] < summary["before"]
    assert summary["after"] <= 800
    assert "Build a parser" in summary["text"]


@pytest.mark.parametrize(
    "kwargs", [{"mode": "invalid"}, {"budget": True}, {"budget": 127}, {"budget": 8001}]
)
def test_invalid_options_fail_before_transform(kwargs):
    with pytest.raises(ValueError):
        prepare_text("draft", **kwargs)


def test_utf8_size_limits_and_monitor_protocol(tmp_path):
    with pytest.raises(ValueError):
        prepare_text("🧪" * (MAX_INPUT_BYTES // 4 + 1))
    service = Monitor([], tmp_path / "collector")
    text = "🧪 audit details\n" * 4000
    request = json.dumps({"id": 1, "method": "prepare", "text": text})
    assert len(request) > 65536
    output = io.StringIO()
    service.serve(io.StringIO("x" * (1024 * 1024 + 1) + "\n" + request + "\n"), output)
    error, response = map(json.loads, output.getvalue().splitlines())
    assert error["error"] == "ValueError"
    assert response["id"] == 1
    assert response["result"]["delivery"] == "preview"


def test_cli_file_and_stdin_prepare_without_usage_events(tmp_path):
    runner = CliRunner()
    text = "Keep every requirement: deterministic, local, and reversible."
    file = tmp_path / "draft.txt"
    file.write_text(text)
    for args, stdin in [(["--file", str(file)], None), ([], text)]:
        result = runner.invoke(app, ["prepare", *args, "--json"], input=stdin)
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["text"] == text
    too_large = runner.invoke(app, ["prepare", "--json"], input="a" * (MAX_INPUT_BYTES + 1))
    assert too_large.exit_code != 0
    assert not (Path(os.environ["USAGETRIM_CACHE_DIR"]) / "telemetry.db").exists()


def test_prepare_optimize_mode():
    prompt = """Analyze the following users table:
```json
[
  {"id": 1, "username": "alice", "role": "admin"},
  {"id": 2, "username": "bob", "role": "engineer"},
  {"id": 3, "username": "charlie", "role": "tester"}
]
```
Please let me know if there are any issues.
"""
    result = prepare_text(prompt, mode="optimize", budget=1000)
    assert result["mode"] == "optimize"
    assert result["after"] < result["before"]
    assert "[id | username | role]" in result["text"]
    assert "Analyze the following users table:" in result["text"]
    assert "Please let me know if there are any issues." in result["text"]


def test_cli_prepare_optimize_mode(tmp_path):
    runner = CliRunner()
    file = tmp_path / "prompt.txt"
    file.write_text("""Review this table:
```json
[{"id": 1, "status": "active"}, {"id": 2, "status": "idle"}]
```
Any observations?
""")
    result = runner.invoke(app, ["prepare", "--file", str(file), "--mode", "optimize", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["mode"] == "optimize"
    assert "[id | status]" in payload["text"]


def test_format_prepare_counts_shows_before_after_delta_and_percent():
    from usagetrim.core.prepare import format_prepare_counts

    assert format_prepare_counts(4512, 1203) == "4,512 → 1,203 (−3,309 · −73%)"
    assert format_prepare_counts(100, 100) == "100 → 100 (0 · 0%)"
    assert format_prepare_counts(0, 0) == "0 → 0 (0 · 0%)"
    assert format_prepare_counts(50, 80) == "50 → 80 (+30 · +60%)"


def test_prepare_text_includes_counts_summary_and_percent():
    from usagetrim.core.prepare import format_prepare_counts

    text = "Downloading unchanged dependency\n" * 250 + "ERROR: installation failed\n"
    result = prepare_text(text)
    assert result["difference"] > 0
    assert result["percent"] == round((result["after"] - result["before"]) / result["before"] * 100)
    assert result["counts"] == format_prepare_counts(result["before"], result["after"])


def test_cli_prepare_stderr_shows_arrow_counts(tmp_path):
    from usagetrim.core.prepare import DEFAULT_PREPARE_BUDGET, format_prepare_counts

    runner = CliRunner()
    file = tmp_path / "noisy.txt"
    file.write_text("Downloading unchanged dependency\n" * 250 + "ERROR: keep me\n")
    result = runner.invoke(app, ["prepare", "--file", str(file)])
    assert result.exit_code == 0, result.output
    payload = prepare_text(file.read_text())
    assert format_prepare_counts(payload["before"], payload["after"]) in result.stderr
    assert "o200k" in result.stderr
    assert "usage" in result.stderr.lower()
    assert DEFAULT_PREPARE_BUDGET == 2000


def test_cli_prepare_defaults_use_desktop_budget():
    import inspect

    from usagetrim.cli import prepare_command

    params = inspect.signature(prepare_command).parameters
    assert params["budget"].default == 2000
    assert params["mode"].default == "conservative"


@pytest.fixture
def model_prepare_backend(monkeypatch):
    from types import SimpleNamespace

    from usagetrim.core import adaptive, optimizer, prepare, prompt_optimizer, specialized
    from usagetrim.metrics import tokenizer

    stored = {}
    calls = []

    class MemoryCache:
        def store(self, text, *, source):
            stored["tc_0123456789abcdef"] = text
            return "tc_0123456789abcdef"

    def unavailable(name):
        raise ValueError("No instantiated encoding")

    def forbidden(*args, **kwargs):
        pytest.fail("explicit model must not use legacy prepare counting")

    def optimize(text, *, budget, model):
        calls.append((text, budget, model))
        # Deliberately return oversized text to verify prepare's final budget check.
        return SimpleNamespace(text=text)

    monkeypatch.setattr(tokenizer, "_model_encoding", unavailable)
    monkeypatch.setattr(prepare, "count_tokens", forbidden)
    monkeypatch.setattr(adaptive, "count_tokens", forbidden)
    monkeypatch.setattr(adaptive, "ContextCache", MemoryCache)
    monkeypatch.setattr(adaptive, "compact_terminal_output", lambda text, options: text)
    monkeypatch.setattr(prepare, "safe_compact_output", lambda text: text)
    monkeypatch.setattr(
        prepare, "distill_conversation", lambda text, **kw: SimpleNamespace(text=text)
    )
    monkeypatch.setattr(optimizer, "optimize_context", optimize)
    monkeypatch.setattr(specialized, "filter_traceback", lambda text: text)
    monkeypatch.setattr(specialized, "filter_dev_server_logs", lambda text: text)
    monkeypatch.setattr(prompt_optimizer, "stabilize_cache_prefix", lambda text, **kwargs: text)
    return prepare, tokenizer, stored, calls


@pytest.mark.parametrize("mode", ["conservative", "summary", "optimize", "desktop"])
@pytest.mark.parametrize("model", ["openai", "claude", "gemini", "llama", "generic"])
def test_prepare_model_budget_counts_and_recovery(model_prepare_backend, mode, model):
    from usagetrim.core.prepare import format_prepare_counts
    from usagetrim.core.redactor import redact_secrets

    _, tokenizer, stored, calls = model_prepare_backend
    secret = "sk-proj-" + "a" * 30
    raw = f"token={secret}\n" + "diagnostic 漢字🙂 " * 400
    result = prepare_text(raw, mode=mode, budget=128, model=model)
    measurement = tokenizer.count_model_tokens(result["text"], model)
    before = tokenizer.count_model_tokens(raw, model).count
    assert result["before"] == before
    assert result["after"] == measurement.count
    assert result["difference"] == before - measurement.count
    assert result["percent"] == round((measurement.count - before) / before * 100)
    assert result["counts"] == format_prepare_counts(before, measurement.count)
    assert result["method"] == measurement.method
    assert result["is_estimate"] is True
    assert result["family"] == model
    assert secret not in result["text"]
    if mode == "conservative":
        assert result["text"] == redact_secrets(raw)
        assert result["after"] > 128
        assert stored == {}
    else:
        assert result["after"] <= 128
        assert "tc_0123456789abcdef" in result["text"]
        assert stored["tc_0123456789abcdef"] == redact_secrets(raw)
    if mode in {"optimize", "desktop"}:
        assert calls == [(redact_secrets(raw), 128, model)]


@pytest.mark.parametrize("mode", ["summary", "optimize", "desktop"])
@pytest.mark.parametrize("length", [200, 512])
def test_prepare_fitting_model_candidate_does_not_add_recovery(
    monkeypatch, model_prepare_backend, mode, length
):
    from types import SimpleNamespace

    from usagetrim.core import adaptive, optimizer

    prepare, _, stored, _ = model_prepare_backend
    candidate = "x" * length

    def transform(*args, **kwargs):
        return SimpleNamespace(text=candidate)

    def forbidden(*args, **kwargs):
        pytest.fail("a fitting candidate must not be compressed again")

    monkeypatch.setattr(prepare, "distill_conversation", transform)
    monkeypatch.setattr(optimizer, "optimize_context", transform)
    monkeypatch.setattr(adaptive, "compress_to_budget", forbidden)
    result = prepare_text("source " * 500, mode=mode, budget=128, model="generic")
    assert result["text"] == candidate
    assert result["after"] <= 128
    assert "tc_" not in result["text"]
    assert stored == {}


@pytest.mark.parametrize("mode", ["summary", "optimize", "desktop"])
def test_prepare_oversized_model_candidate_retains_recoverable_original(
    monkeypatch, model_prepare_backend, mode
):
    from types import SimpleNamespace

    from usagetrim.core import optimizer
    from usagetrim.core.redactor import redact_secrets

    prepare, tokenizer, stored, _ = model_prepare_backend
    secret = "sk-proj-" + "a" * 30
    raw = f"token={secret}\n" + "source diagnostic " * 600
    candidate = "retained diagnostic " * 100

    def transform(*args, **kwargs):
        return SimpleNamespace(text=candidate)

    monkeypatch.setattr(prepare, "distill_conversation", transform)
    monkeypatch.setattr(optimizer, "optimize_context", transform)
    result = prepare_text(raw, mode=mode, budget=128, model="generic")
    assert result["text"] != candidate
    assert result["after"] == tokenizer.count_model_tokens(result["text"], "generic").count
    assert result["after"] <= 128
    assert "tc_0123456789abcdef" in result["text"]
    assert stored["tc_0123456789abcdef"] == redact_secrets(raw)
    assert secret not in result["text"]


def test_prepare_model_compares_candidates_with_selected_counter(
    monkeypatch, model_prepare_backend
):
    from types import SimpleNamespace

    prepare, _, _, _ = model_prepare_backend
    raw = "Keep the original instructions."
    monkeypatch.setattr(
        prepare,
        "distill_conversation",
        lambda *args, **kwargs: SimpleNamespace(text="expanded " * 100, distilled_tokens=0),
    )
    assert prepare_text(raw, mode="summary", model="generic")["text"] == raw


def test_prepare_passes_model_to_prefix_stabilization(monkeypatch, model_prepare_backend):
    from usagetrim.core import prompt_optimizer

    calls = []

    def stabilize(text, *, model):
        calls.append(model)
        return text

    monkeypatch.setattr(prompt_optimizer, "stabilize_cache_prefix", stabilize)
    prepare_text("Keep every instruction.", mode="desktop", model="llama")
    assert calls == ["llama"]


def test_prepare_unknown_model_precedes_transforms_and_cache(monkeypatch):
    from usagetrim.core import prepare

    def forbidden(*args, **kwargs):
        pytest.fail("unknown model must be rejected before transforms or persistence")

    monkeypatch.setattr(prepare, "safe_compact_output", forbidden)
    monkeypatch.setattr(ContextCache, "store", forbidden)
    with pytest.raises(ValueError, match="Unknown model"):
        prepare_text("draft", model="unknown")


def test_prepare_none_preserves_legacy_payload(monkeypatch):
    from usagetrim.core import prepare
    from usagetrim.metrics.tokenizer import TokenCount

    def forbidden(*args, **kwargs):
        pytest.fail("legacy prepare must not use model counting")

    monkeypatch.setattr(prepare, "count_model_tokens", forbidden)
    monkeypatch.setattr(prepare, "count_tokens", lambda text: TokenCount(3, 2, 1))
    monkeypatch.setattr(prepare, "safe_compact_output", lambda text: text)
    expected = {
        "text": "draft",
        "before": 2,
        "after": 2,
        "difference": 0,
        "percent": 0,
        "counts": "2 → 2 (0 · 0%)",
        "changed": False,
        "redacted": False,
        "mode": "conservative",
        "method": "o200k_base",
        "delivery": "preview",
    }
    assert prepare_text("draft") == prepare_text("draft", model=None) == expected


@pytest.mark.parametrize("mode", ["conservative", "summary", "optimize", "desktop"])
def test_cli_prepare_explicit_model(model_prepare_backend, mode):
    runner = CliRunner()
    args = ["prepare", "--model", "llama", "--mode", mode]
    result = runner.invoke(app, [*args, "--json"], input="Keep every requirement.")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["model"] == payload["family"] == "llama"
    assert payload["is_estimate"] is True
    assert payload["method"] == "utf8-bytes-div4-ceil"
    plain = runner.invoke(app, args, input="Keep every requirement.")
    assert plain.exit_code == 0, plain.output
    assert "estimate" in plain.stderr
    assert "llama" in plain.stderr
    assert "utf8-bytes-div4-ceil" in plain.stderr


def test_cli_prepare_unknown_model_is_controlled_error():
    result = CliRunner().invoke(app, ["prepare", "--model", "unknown"], input="draft")
    assert result.exit_code == 2
    assert "Unknown model" in result.output
    assert "Traceback" not in result.output
