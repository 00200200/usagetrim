from __future__ import annotations

import copy
import json
import re

import pytest

from usagetrim.core.cache import ContextCache
from usagetrim.core.optimizer import mask_completed_observations, optimize_context
from usagetrim.metrics.tokenizer import count_tokens


@pytest.fixture
def model_optimizer(monkeypatch):
    from types import SimpleNamespace

    from usagetrim.core import adaptive, optimizer, specialized
    from usagetrim.metrics import tokenizer

    stored = {}

    class MemoryCache:
        def store(self, text, *, source):
            stored["tc_0123456789abcdef"] = text
            return "tc_0123456789abcdef"

    def unavailable(name):
        raise ValueError("No instantiated encoding")

    def forbidden(*args, **kwargs):
        pytest.fail("model path must not use legacy counting")

    monkeypatch.setattr(optimizer, "ContextCache", MemoryCache)
    monkeypatch.setattr(adaptive, "ContextCache", MemoryCache)
    monkeypatch.setattr(tokenizer, "_model_encoding", unavailable)
    monkeypatch.setattr(optimizer, "count_tokens", forbidden)
    monkeypatch.setattr(adaptive, "count_tokens", forbidden)
    monkeypatch.setattr(adaptive, "compact_terminal_output", lambda text, options: text)
    monkeypatch.setattr(specialized, "filter_traceback", lambda text: text)
    monkeypatch.setattr(specialized, "filter_dev_server_logs", lambda text: text)
    monkeypatch.setattr(
        optimizer,
        "compact_text",
        lambda text, **kwargs: SimpleNamespace(
            text=text, compacted_tokens=0, original_tokens=99999, content_type="text"
        ),
    )
    return optimizer, tokenizer, stored


@pytest.mark.parametrize("model", ["openai", "claude", "gemini", "llama", "generic"])
def test_optimizer_model_budget_statistics_and_redacted_recovery(model_optimizer, model):
    from usagetrim.core.redactor import redact_secrets

    optimizer, tokenizer, stored = model_optimizer
    secret = "sk-proj-" + "a" * 30
    raw = f"token={secret}\n" + "diagnostic 漢字🙂 " * 500
    result = optimizer.optimize_context(raw, 140, False, model=model)
    before = tokenizer.count_model_tokens(redact_secrets(raw), model).count
    after = tokenizer.count_model_tokens(result.text, model).count
    assert result.original_tokens == before
    assert result.optimized_tokens == after <= 140
    assert result.saved_tokens == max(0, before - after)
    assert result.reduction_pct == round(100 * result.saved_tokens / before, 1)
    assert result.ref_id in result.text
    assert stored[result.ref_id] == redact_secrets(raw)
    assert secret not in result.text


@pytest.mark.parametrize("kind", ["table", "conversation", "fence"])
@pytest.mark.parametrize("smaller", [True, False])
def test_optimizer_model_recounts_candidates_instead_of_foreign_statistics(
    monkeypatch, model_optimizer, kind, smaller
):
    from types import SimpleNamespace

    optimizer, tokenizer, _ = model_optimizer
    raw = {
        "table": '[{"value": "' + "x" * 200 + '"}]',
        "conversation": "User: " + "question " * 30,
        "fence": '```json\n[{"value": "' + "x" * 200 + '"}]\n```',
    }[kind]
    candidate = "short candidate" if smaller else "expanded candidate " * 300
    foreign = SimpleNamespace(
        text=candidate,
        compacted_tokens=99999 if smaller else 0,
        original_tokens=1,
        distilled_tokens=99999 if smaller else 0,
    )
    monkeypatch.setattr(optimizer, "compact_table", lambda *args, **kwargs: foreign)
    monkeypatch.setattr(optimizer, "distill_conversation", lambda *args, **kwargs: foreign)
    result = optimizer.optimize_context(raw, 10000, False, model="generic")
    if smaller:
        assert candidate in result.text
        assert result.optimized_tokens < result.original_tokens
    else:
        assert result.text == raw
    assert result.optimized_tokens == tokenizer.count_model_tokens(result.text, "generic").count


def test_optimizer_passes_model_to_prompt_alignment(monkeypatch, model_optimizer):
    from types import SimpleNamespace

    optimizer, _, _ = model_optimizer
    calls = []

    def align(text, *, model):
        calls.append(model)
        return SimpleNamespace(aligned_text=text)

    monkeypatch.setattr(optimizer, "lint_prompt", lambda text: {"issues": ["dynamic prefix"]})
    monkeypatch.setattr(optimizer, "align_prompt", align)
    optimizer.optimize_context("Current time: 12:00\n" + "Keep rules.\n" * 20, model="llama")
    assert calls == ["llama"]


def test_optimizer_unknown_model_precedes_cache(monkeypatch):
    from usagetrim.core import optimizer

    def forbidden():
        pytest.fail("unsupported model must not construct cache")

    monkeypatch.setattr(optimizer, "ContextCache", forbidden)
    with pytest.raises(ValueError, match="Unknown model"):
        optimizer.optimize_context("payload", model="unknown")


def test_optimizer_model_small_input_still_enforces_budget(model_optimizer):
    optimizer, _, _ = model_optimizer
    with pytest.raises(ValueError, match="recovery reference"):
        optimizer.optimize_context("abcdefgh", 1, False, model="generic")


def test_optimizer_none_keeps_legacy_counting(monkeypatch, model_optimizer):
    from usagetrim.metrics.tokenizer import TokenCount

    optimizer, _, _ = model_optimizer

    def forbidden(*args, **kwargs):
        pytest.fail("legacy path must not invoke model counting")

    monkeypatch.setattr(optimizer, "count_model_tokens", forbidden)
    monkeypatch.setattr(optimizer, "count_tokens", lambda text: TokenCount(3, 2, 1))
    implicit = optimizer.optimize_context("hello")
    explicit = optimizer.optimize_context("hello", model=None)
    assert implicit == explicit
    assert explicit.original_tokens == explicit.optimized_tokens == 2
    assert explicit.text == "hello"


def test_optimize_intra_fence_json_to_toon():
    users = [{"id": i, "name": f"User_{i}", "role": "developer", "active": True} for i in range(25)]
    prompt = f"""You are a senior data analyst.
Please review these active employee records:

```json
{json.dumps(users, indent=2)}
```

Highlight any anomalies or unexpected permissions.
"""
    res = optimize_context(prompt, budget=2000)
    assert res.primary_mode == "intra_fence_hybrid"
    assert "fence:toon" in res.pipeline_stages
    assert "You are a senior data analyst." in res.text
    assert "Highlight any anomalies" in res.text
    assert "[id | name | role | active]" in res.text
    assert res.optimized_tokens < res.original_tokens
    assert res.saved_tokens > 0


def test_optimize_intra_fence_diff():
    # Lockfiles are collapsed by slim_git_diff
    diff_payload = """diff --git a/package-lock.json b/package-lock.json
index 1234567..89abcdef 100644
--- a/package-lock.json
+++ b/package-lock.json
@@ -1,50 +1,50 @@
+ "name": "super-huge-lockfile",
+ "version": "1.0.0",
""" + "\n".join(f'+ "entry_{i}": {{ "version": "0.{i}.0" }}' for i in range(200))

    prompt = f"""Review the following dependency update patch:

```diff
{diff_payload}
```

Is this package upgrade safe?
"""
    res = optimize_context(prompt, budget=2000)
    assert res.primary_mode == "intra_fence_hybrid"
    assert "fence:diff" in res.pipeline_stages
    assert "Review the following dependency update patch:" in res.text
    assert "Is this package upgrade safe?" in res.text
    assert "lockfile/generated diff omitted by usagetrim" in res.text
    assert res.optimized_tokens < res.original_tokens


def test_optimize_intra_fence_traceback():
    traceback_payload = """Traceback (most recent call last):
  File "server.py", line 142, in handle_request
    response = router.dispatch(path)
  File "router.py", line 55, in dispatch
    raise KeyError("Route '/api/v2/users' not registered")
KeyError: "Route '/api/v2/users' not registered"
"""
    prompt = f"""An unexpected exception occurred:

```text
{traceback_payload}
```

What is causing this failure?
"""
    res = optimize_context(prompt, budget=2000)
    assert "An unexpected exception occurred:" in res.text
    assert "What is causing this failure?" in res.text
    assert "KeyError" in res.text


def test_optimize_whole_document_table():
    data = json.dumps([{"col_a": i, "col_b": f"val_{i}"} for i in range(30)])
    res = optimize_context(data, budget=1000)
    assert res.primary_mode == "table_toon"
    assert "table_compression" in res.pipeline_stages
    assert "[col_a | col_b]" in res.text


def test_optimize_whole_document_conversation():
    transcript = """User: Can you check the build status for the backend service?
Assistant: I inspected the test suite and all 400 unit tests passed successfully on commit aed2393. Here is the full breakdown of every test run across our 15 microservices: service auth passed with 40 tests, service payment passed with 80 tests, service data passed with 120 tests, service worker passed with 90 tests, service notifications passed with 70 tests.
User: Should we use SQLite or PostgreSQL for local developer caching?
Assistant: We decided to use SQLite with WAL mode for the local context cache because it requires zero daemon setup. Setting up PostgreSQL would require docker containers and background daemons which slows down developer onboarding and introduces extra memory overhead.
User: Great. Which files need to be updated for this change?
Assistant: We modified src/usagetrim/core/cache.py and src/usagetrim/cli.py to support the new cache schema and ensure connection pooling works correctly across threads.
User: What are the next steps before opening the pull request?
Assistant: We must run ruff format, verify all tests pass, and generate the pull request description detailing the benchmark results and memory footprint.
User: Go ahead and run the checks now.
Assistant: All checks passed cleanly with 100% test success rate. Ready for review.
"""
    res = optimize_context(transcript, budget=500)
    assert res.primary_mode == "conversation_distill"
    assert "conversation_distillation" in res.pipeline_stages
    assert "Distilled Conversation Context" in res.text
    assert "src/usagetrim/core/cache.py" in res.text


def test_optimize_whole_document_prompt_alignment():
    raw_prompt = """Today is Monday, September 21, 2026. Current time: 14:00:00 UTC.
Request ID: req_98723498234

You are an expert Python systems architect.
Always write clean, typed, modular code.
Follow PEP 8 conventions.
"""
    res = optimize_context(raw_prompt, budget=1000)
    assert res.primary_mode == "prompt_cache_align"
    assert "prompt_cache_alignment" in res.pipeline_stages
    assert "You are an expert Python systems architect." in res.text


def test_optimize_strict_budget_ceiling_and_ccr_recovery():
    long_text = "\n".join(
        f"Log line {i}: process worker heartbeat ping received ok" for i in range(500)
    )
    res = optimize_context(long_text, budget=120)
    assert res.optimized_tokens <= 120
    assert res.ref_id.startswith("tc_")
    assert f"Ref: {res.ref_id}" in res.text
    recovered = ContextCache().retrieve(res.ref_id)
    assert recovered == long_text


def test_optimize_budget_ceiling_stage_trigger():
    # Intra-fence hybrid with large instructions that exceed budget after fence compaction
    users = [{"id": i, "name": f"User_{i}", "role": "developer"} for i in range(20)]
    instructions = "\n".join(
        f"Instruction {i}: ensure all safety guidelines are followed strictly." for i in range(80)
    )
    prompt = f"{instructions}\n\n```json\n{json.dumps(users)}\n```\n\nFinal review step."
    res = optimize_context(prompt, budget=150)
    assert res.optimized_tokens <= 150
    assert "intra_fence_hybrid" == res.primary_mode
    assert "fence:toon" in res.pipeline_stages
    assert "budget_ceiling_enforcement" in res.pipeline_stages


def test_optimize_short_passthrough():
    short = "Hello world"
    res = optimize_context(short, budget=2000)
    assert res.primary_mode == "passthrough"
    assert res.text == short
    assert res.saved_tokens == 0


def test_optimize_secret_redaction():
    secret_text = "Deploying with access key AKIAIOSFODNN7EXAMPLE to production"
    res = optimize_context(secret_text, budget=2000)
    assert "AKIAIOSFODNN7EXAMPLE" not in res.text
    assert "[REDACTED_AWS_KEY]" in res.text
    assert ContextCache().retrieve(res.ref_id) == res.text


def _long_observation():
    return "\n".join(
        f"Diagnostic row {i}: worker completed operation in {i + 10} ms" for i in range(200)
    )


def test_mask_completed_observations_reduces_history_and_preserves_active_result():
    output = _long_observation()
    history = [
        {"role": "user", "content": "Inspect the service."},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "done",
                    "function": {"name": "read_file", "arguments": '{"path":"src/service.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "done", "content": output, "metadata": {"exit": 0}},
        {"role": "assistant", "content": "The service is healthy."},
        {"role": "user", "content": "Now inspect the active request."},
        {"role": "tool", "tool_call_id": "active", "content": "active diagnostic " * 200},
    ]
    before = copy.deepcopy(history)
    masked = mask_completed_observations(history)
    assert history == before
    assert masked[:2] == history[:2] and masked[3:] == history[3:]
    assert masked[2]["tool_call_id"] == "done"
    assert masked[2]["metadata"] == {"exit": 0}
    assert "read_file" in masked[2]["content"] and "src/service.py" in masked[2]["content"]
    ref = re.search(r"tc_[a-f0-9]+", masked[2]["content"])[0]
    assert ContextCache().retrieve(ref) == output
    raw_tokens = count_tokens(json.dumps(history)).openai
    assert 100 * (raw_tokens - count_tokens(json.dumps(masked)).openai) / raw_tokens > 60
    masked[2]["metadata"]["exit"] = 1
    assert history[2]["metadata"]["exit"] == 0


@pytest.mark.parametrize(
    "content", [None, "", " \n", [], [{"type": "thinking", "thinking": "Working"}]]
)
def test_mask_requires_nonempty_assistant_text(content):
    history = [
        {"role": "tool", "tool_call_id": "pending", "content": _long_observation()},
        {
            "role": "assistant",
            "content": content,
            "tool_calls": [{"id": "next", "function": {"name": "read"}}],
        },
    ]
    assert mask_completed_observations(history) == history


@pytest.mark.parametrize("role", ["user", "human", "system", "developer"])
def test_mask_does_not_use_synthesis_from_another_turn(role):
    history = [
        {"role": "tool", "content": _long_observation()},
        {"role": role, "content": "Start another request."},
        {"role": "assistant", "content": "New request completed."},
    ]
    assert mask_completed_observations(history) == history


@pytest.mark.parametrize("structured_content", [False, True])
def test_mask_anthropic_results_preserves_blocks_and_uses_text_synthesis(structured_content):
    output = _long_observation()
    content = (
        [
            {"type": "text", "text": output, "extra": "keep"},
            {"type": "image", "source": {"type": "base64", "data": "keep"}},
        ]
        if structured_content
        else output
    )
    history = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "lookup",
                    "name": "lookup",
                    "input": {"query": "service"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "lookup",
                    "is_error": True,
                    "content": content,
                }
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "The lookup result shows the cause."}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "active", "content": output}],
        },
    ]
    before = copy.deepcopy(history)
    masked = mask_completed_observations(history)
    assert history == before
    assert masked[0] == history[0] and masked[2:] == history[2:]
    block = masked[1]["content"][0]
    assert block["tool_use_id"] == "lookup" and block["is_error"] is True
    if structured_content:
        assert block["content"][1] == content[1]
        assert block["content"][0]["extra"] == "keep"
        receipt = block["content"][0]["text"]
    else:
        receipt = block["content"]
    assert "Tool lookup" in receipt and "service" in receipt
    assert ContextCache().retrieve(re.search(r"tc_[a-f0-9]+", receipt)[0]) == output


def test_mask_mixed_user_instruction_and_tool_result_is_conservative():
    history = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "old", "content": _long_observation()},
                {"type": "text", "text": "Start a new task."},
            ],
        },
        {"role": "assistant", "content": "New task complete."},
    ]
    assert mask_completed_observations(history) == history


def test_mask_receipt_and_recovery_redact_secrets():
    secret = "ghp_" + "a" * 36
    output = f"warning {secret}\n" + _long_observation()
    history = [
        {"role": "tool", "content": output},
        {"role": "assistant", "content": "The warning was handled."},
    ]
    receipt = mask_completed_observations(history)[0]["content"]
    assert secret not in receipt and "[REDACTED" in receipt
    recovered = ContextCache().retrieve(re.search(r"tc_[a-f0-9]+", receipt)[0])
    assert secret not in recovered and "[REDACTED" in recovered


def test_mask_preserves_small_results_and_assistant_thoughts():
    history = [
        {"role": "tool", "content": "ok"},
        {"role": "assistant", "content": "<thinking>Kept intact.</thinking>\nDone."},
    ]
    assert mask_completed_observations(history) == history
    assert mask_completed_observations([]) == []
