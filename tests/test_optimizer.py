from __future__ import annotations

import copy
import json
import re

import pytest

from usagetrim.core.cache import ContextCache
from usagetrim.core.optimizer import mask_completed_observations, optimize_context
from usagetrim.metrics.tokenizer import count_tokens


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
