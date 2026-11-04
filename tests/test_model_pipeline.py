"""Real model-aware pipelines must stay offline, consistent, and recoverable."""

import json
import re
import sqlite3

import pytest

from usagetrim.core import (
    adaptive,
    clip,
    distill,
    optimizer,
    prepare,
    prompt_optimizer,
    safe_filter,
    table,
)
from usagetrim.core.cache import ContextCache, decompress_payload
from usagetrim.core.redactor import redact_secrets
from usagetrim.metrics import tokenizer


@pytest.fixture(params=["missing", "cold"])
def offline_pipeline(monkeypatch, tmp_path, request):
    monkeypatch.setenv("USAGETRIM_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("USAGETRIM_CACHE_MAX_BYTES", raising=False)
    monkeypatch.delenv("USAGETRIM_CACHE_MAX_MB", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("Model-aware pipelines must not use legacy counting or vocabulary I/O")

    if tokenizer.tiktoken is not None:
        import tiktoken.load
        import tiktoken.registry

        monkeypatch.setattr(tiktoken.load, "read_file", forbidden)
        monkeypatch.setattr(tiktoken.registry, "ENCODINGS", {})
        monkeypatch.setattr(tokenizer.tiktoken, "get_encoding", forbidden)
    monkeypatch.setattr(tokenizer, "_O200K_ENC", None)
    monkeypatch.setattr(tokenizer, "_CL100K_ENC", None)
    if request.param == "missing":
        monkeypatch.setattr(tokenizer, "tiktoken", None)
    for module in (
        adaptive,
        clip,
        distill,
        optimizer,
        prepare,
        prompt_optimizer,
        safe_filter,
        table,
    ):
        monkeypatch.setattr(module, "count_tokens", forbidden)
    return tmp_path


def stored_payloads(directory):
    path = directory / "cache.db"
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        return [
            decompress_payload(row[0]) for row in conn.execute("SELECT content FROM output_cache")
        ]


def test_unparsable_table_stores_once_and_recovers_redacted_original(offline_pipeline, monkeypatch):
    secret = "ghp_" + "A" * 36
    # A single non-tabular line also contains a misleading user-supplied reference.
    raw = "tc_0000000000000000 " + secret + " diagnostic 漢字🙂" * 600
    stored = []
    original_store = ContextCache.store

    def record_store(self, content, source="exec", *, namespace=""):
        ref_id = original_store(self, content, source, namespace=namespace)
        stored.append((content, source, ref_id))
        return ref_id

    monkeypatch.setattr(ContextCache, "store", record_store)
    result = table.compact_table(raw, budget=128, model="generic")

    assert len(stored) == 1
    content, source, ref_id = stored[0]
    assert content == redact_secrets(raw)
    assert source == "table-compaction"
    assert result.ref_id == ref_id
    assert f"[... Ref: {ref_id}; retrieve omitted lines]" in result.text
    assert result.format_type == "raw"
    assert result.compacted_tokens == tokenizer.count_model_tokens(result.text, "generic").count
    assert result.compacted_tokens <= 128
    assert ContextCache().retrieve(ref_id) == redact_secrets(raw)
    assert stored_payloads(offline_pipeline) == [redact_secrets(raw)]
    assert secret not in result.text and secret not in content


@pytest.mark.parametrize(
    "escape", ["\x1b[31m", "\x1b[?25l", "\x1b]0;title\x07", "\x1b]0;title\x1b\\"]
)
@pytest.mark.parametrize("long", [False, True])
def test_adaptive_redacts_ansi_secrets_on_fit_and_compression_paths(offline_pipeline, escape, long):
    secret = "ghp_" + "A" * 36
    split = "ghp_" + escape + "A" * 36
    raw = "\x1b[32mstatus\x1b[0m\n" + split
    if long:
        raw += "\n" + "diagnostic 漢字🙂 " * 600
    output = adaptive.compress_to_budget(raw, 128, model="generic")
    assert secret not in output and split not in output
    assert "A" * 36 not in output
    assert tokenizer.count_model_tokens(output, "generic").count <= 128
    for payload in stored_payloads(offline_pipeline):
        assert secret not in payload and split not in payload and "A" * 36 not in payload
    if long:
        ref = re.search(r"tc_[0-9a-f]{16}", output).group()
        assert ContextCache().retrieve(ref) == redact_secrets(raw)
    else:
        assert "REDACTED" in output
        assert stored_payloads(offline_pipeline) == []


def test_adaptive_redacts_transformed_original_and_suffix_boundaries(offline_pipeline):
    split = "ghp_\x1b[31m" + "A" * 36
    output = adaptive.compress_to_budget(
        split, 128, original_text=split + "\noriginal", model="generic"
    )
    assert "A" * 36 not in output
    assert all("A" * 36 not in value for value in stored_payloads(offline_pipeline))
    joined = adaptive.compress_to_budget("ghp_", 128, suffix="A" * 36, model="generic")
    assert "A" * 36 not in joined and "REDACTED" in joined


SAMPLES = {
    "plain": "ordinary diagnostic 漢字🙂 " * 300,
    "whitespace": " " * 2048,
    "json": json.dumps(
        [{"id": i, "name": f"service-{i}", "message": "diagnostic" * 8} for i in range(50)]
    ),
    "conversation": "User: Preserve /srv/data and run tests.\nAssistant: " + "diagnostic " * 500,
    "fence": "Rules:\n```json\n"
    + json.dumps([{"id": i, "value": "diagnostic" * 8} for i in range(40)])
    + "\n```\n",
    "pytest": "test session starts\n"
    + "test_api.py::test_ok PASSED\n" * 100
    + "FAILED test_api.py::test_error - diagnostic\n",
    "diff": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
}


@pytest.mark.parametrize("sample", SAMPLES)
@pytest.mark.parametrize("model", ["openai", "claude", "gemini", "llama", "deepseek", "generic"])
def test_real_optimizer_helpers_stay_offline_and_obey_model_budget(offline_pipeline, sample, model):
    result = optimizer.optimize_context(SAMPLES[sample], 256, model=model)
    assert result.optimized_tokens == tokenizer.count_model_tokens(result.text, model).count
    assert result.optimized_tokens <= 256
    assert result.is_estimate is True
    assert "utf8-bytes-div4-ceil" in result.method
    assert result.ref_id
    assert ContextCache().retrieve(result.ref_id) == redact_secrets(SAMPLES[sample])


@pytest.mark.parametrize("mode", ["conservative", "summary", "optimize", "desktop"])
@pytest.mark.parametrize("model", ["openai", "claude", "gemini", "llama", "deepseek", "generic"])
def test_real_prepare_pipeline_stays_offline(offline_pipeline, mode, model):
    split = "ghp_\x1b[31m" + "A" * 36
    raw = "User: Preserve the diagnostic.\n" + split + "\nAssistant: " + "diagnostic " * 300
    result = prepare.prepare_text(raw, mode=mode, budget=256, model=model)
    assert result["after"] == tokenizer.count_model_tokens(result["text"], model).count
    assert result["is_estimate"]
    if mode != "conservative":
        assert result["after"] <= 256
    assert "A" * 36 not in result["text"]
    assert all("A" * 36 not in value for value in stored_payloads(offline_pipeline))


@pytest.mark.parametrize("helper", ["table", "distill", "clip", "safe"])
def test_helpers_accept_model_and_use_real_offline_implementations(offline_pipeline, helper):
    if helper == "table":
        result = table.compact_table(SAMPLES["json"], budget=128, model="generic")
        text, reported = result.text, result.compacted_tokens
    elif helper == "distill":
        result = distill.distill_conversation(SAMPLES["conversation"], budget=128, model="generic")
        text, reported = result.text, result.distilled_tokens
    elif helper == "clip":
        result = clip.compact_text(SAMPLES["plain"], budget=128, model="generic")
        text, reported = result.text, result.compacted_tokens
    else:
        text = safe_filter.safe_compact_output(SAMPLES["pytest"], command="pytest", model="generic")
        assert "passing tests" in text
        return
    assert reported == tokenizer.count_model_tokens(text, "generic").count <= 128


@pytest.mark.parametrize("initially_warm", [False, True])
@pytest.mark.parametrize("budget", [128, 2000])
def test_optimizer_pins_counting_method_across_backend_availability_changes(
    offline_pipeline, monkeypatch, initially_warm, budget
):
    class Encoding:
        name = "o200k_base"

        def encode(self, text, *, disallowed_special):
            return range((len(text) + 1) // 2)

    encoding = Encoding()
    monkeypatch.setattr(tokenizer, "_O200K_ENC", encoding if initially_warm else None)
    original = optimizer.compact_text

    def change_availability(*args, **kwargs):
        monkeypatch.setattr(tokenizer, "_O200K_ENC", None if initially_warm else encoding)
        return original(*args, **kwargs)

    monkeypatch.setattr(optimizer, "compact_text", change_availability)
    raw = "unchanged diagnostic " * 40
    result = optimizer.optimize_context(raw, budget, False, model="openai")
    if budget == 2000:
        assert result.text == raw
        assert result.original_tokens == result.optimized_tokens
        assert result.saved_tokens == 0 and result.reduction_pct == 0
    assert result.method == ("o200k_base" if initially_warm else "utf8-bytes-div4-ceil")
    assert result.is_estimate is (not initially_warm)

    def selected_count(text):
        return (len(text) + (1 if initially_warm else 3)) // (2 if initially_warm else 4)

    assert result.original_tokens == selected_count(raw)
    assert result.optimized_tokens == selected_count(result.text) <= budget
    assert result.saved_tokens == max(0, result.original_tokens - result.optimized_tokens)
    assert result.reduction_pct == round(100 * result.saved_tokens / result.original_tokens, 1)
    assert result.to_dict()["method"] == result.method
    # The outer context must not leak into later, independent measurements.
    assert tokenizer.count_model_tokens(raw, "openai").method != result.method


def test_desktop_recovers_pre_stabilization_original_without_extra_compression(
    offline_pipeline, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("A fitting Desktop result must not be compressed again")

    monkeypatch.setattr(adaptive, "compress_to_budget", forbidden)
    monkeypatch.setattr(optimizer, "compress_to_budget", forbidden)
    raw = (
        "Read /tmp/job-a/input.txt and preserve its contents.\n"
        + "Credential: ghp_\x1b[31m"
        + "A" * 36
        + "\nUse uv run pytest for validation.\n"
    )
    result = prepare.prepare_text(raw, mode="desktop", budget=1000, model="generic")
    assert "<tmpdir>" in result["text"]
    assert "/tmp/job-a/input.txt" not in result["text"]
    assert result["after"] <= 1000
    assert ContextCache().retrieve(result["ref_id"]) == redact_secrets(raw)
    assert "/tmp/job-a/input.txt" in ContextCache().retrieve(result["ref_id"])
    assert all("A" * 36 not in value for value in stored_payloads(offline_pipeline))
    assert len(stored_payloads(offline_pipeline)) == 1


def test_legacy_result_shape_and_backend_choice_are_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("USAGETRIM_CACHE_DIR", str(tmp_path))

    def legacy(text):
        return tokenizer.TokenCount(3, 2, 1)

    def forbidden(*args, **kwargs):
        pytest.fail("Legacy calls must not select a model-aware backend")

    monkeypatch.setattr(tokenizer, "_model_encoding", forbidden)
    for module in (optimizer, prepare, safe_filter):
        monkeypatch.setattr(module, "count_tokens", legacy)
    result = optimizer.optimize_context("unchanged", model=None)
    assert result.text == "unchanged"
    assert result.original_tokens == result.optimized_tokens == 2
    assert "model" not in result.to_dict() and "method" not in result.to_dict()
    prepared = prepare.prepare_text("unchanged", model=None)
    assert "model" not in prepared and "is_estimate" not in prepared
    assert prepared["method"] == "o200k_base"


@pytest.mark.parametrize("json_output", [False, True])
def test_desktop_cli_exposes_original_recovery_without_changing_text_budget(
    offline_pipeline, json_output
):
    from typer.testing import CliRunner

    from usagetrim.cli import app

    raw = "Read /tmp/job-a/input.txt and preserve its contents.\nRun tests.\n"
    args = ["prepare", "--desktop", "--model", "generic"]
    if json_output:
        args.append("--json")
    result = CliRunner().invoke(app, args, input=raw)
    assert result.exit_code == 0, result.output
    if json_output:
        payload = json.loads(result.stdout)
        ref = payload["ref_id"]
        assert payload["after"] <= 2000
    else:
        assert "Original redacted draft: usagetrim retrieve" in result.output
        ref = re.search(
            r"Original redacted draft: usagetrim retrieve (tc_[0-9a-f]{16})", result.output
        ).group(1)
    assert ContextCache().retrieve(ref) == raw


@pytest.mark.parametrize("helper", ["table", "distill", "clip", "safe", "optimizer", "prepare"])
def test_unknown_model_fails_before_any_real_cache_write(monkeypatch, helper):
    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported model must be rejected before persistence")

    monkeypatch.setattr(ContextCache, "store", forbidden)
    function = {
        "table": table.compact_table,
        "distill": distill.distill_conversation,
        "clip": clip.compact_text,
        "safe": safe_filter.safe_compact_output,
        "optimizer": optimizer.optimize_context,
        "prepare": prepare.prepare_text,
    }[helper]
    with pytest.raises(ValueError, match="Unknown model"):
        function("payload", model="unsupported")


def test_legacy_desktop_preserves_counting_and_recovers_pre_stabilization_text(
    monkeypatch, tmp_path
):
    class Encoding:
        name = "o200k_base"

        def encode(self, text, *, disallowed_special):
            return range((len(text.encode("utf-8")) + 3) // 4)

    monkeypatch.setenv("USAGETRIM_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(tokenizer, "_O200K_ENC", Encoding())
    raw = "Read /tmp/job-a/input.txt and preserve its contents.\nRun tests.\n"
    result = prepare.prepare_text(raw, mode="desktop")
    assert "model" not in result and "is_estimate" not in result
    assert result["method"] == "o200k_base"
    assert result["before"] == tokenizer.count_tokens(raw).openai
    assert result["after"] == tokenizer.count_tokens(result["text"]).openai
    assert "<tmpdir>" in result["text"]
    assert ContextCache().retrieve(result["ref_id"]) == raw
