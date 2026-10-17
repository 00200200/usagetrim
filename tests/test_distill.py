from __future__ import annotations

from usagetrim.core.cache import ContextCache
from usagetrim.core.distill import (
    count_history_tokens,
    count_message_tokens,
    distill_conversation,
    trim_conversation_history,
)


def test_distill_conversation_markdown_dialogue():
    transcript = (
        "User: Can we fix the bug in src/api/client.py?\n\n"
        "Assistant: Sure, I see the bug. We decided to implement retry logic with exponential backoff.\n\n"
        "User: Sounds good, but make sure not to use external libraries.\n\n"
        "Assistant: Understood. We chose to use standard library urllib instead. "
        "The previous request failed because of connection timeout.\n"
        "Updated src/api/client.py and verified tests pass.\n"
    )
    res = distill_conversation(transcript, budget=1000)
    assert res.message_count >= 4
    assert "src/api/client.py" in res.files_referenced
    assert "Distilled Conversation Context" in res.text
    assert "Goals & User Inquiries" in res.text
    assert "retry logic" in res.text or "urllib" in res.text
    assert res.ref_id is not None
    # Verify CCR retrieval of full original
    recovered = ContextCache().retrieve(res.ref_id)
    assert "exponential backoff" in recovered


def test_distill_conversation_json_lines():
    import json

    lines = [
        json.dumps({"role": "user", "content": "Let's optimize database queries in db/session.py"}),
        json.dumps({"role": "assistant", "content": "I agree on adding an index to users.id."}),
    ]
    raw = "\n".join(lines)
    res = distill_conversation(raw, budget=1000)
    assert res.message_count == 2
    assert "db/session.py" in res.files_referenced


def test_distill_conversation_thought_block_pruning():
    raw = (
        "User: Start migration.\n\n"
        "Assistant: <thinking>Let me think through the migration steps...</thinking>\n"
        "We configured the database schema in models/user.py.\n"
    )
    res = distill_conversation(raw, budget=1000)
    assert "<thinking>" not in res.text
    assert "Let me think" not in res.text
    assert "models/user.py" in res.files_referenced


def test_distill_conversation_budget_enforcement():
    # Long conversation with many turns
    dialogue = []
    for i in range(50):
        dialogue.append(f"User: Step {i} for src/module_{i}.py")
        dialogue.append(f"Assistant: We decided to implement feature {i}. Everything is working.")
    raw = "\n\n".join(dialogue)

    res = distill_conversation(raw, budget=200)
    assert res.distilled_tokens <= 250
    assert "Full transcript reference" in res.text or "truncated to fit" in res.text


def test_trim_conversation_history_short_history_untouched():
    messages = [
        {"role": "system", "content": "You are a helpful coding assistant."},
        {"role": "user", "content": "Hello, how do I configure logging?"},
        {"role": "assistant", "content": "Use logging.basicConfig()."},
    ]
    trimmed = trim_conversation_history(messages, max_tokens=4000, keep_recent_turns=6)
    assert len(trimmed) == 3
    assert trimmed[0] == messages[0]
    assert trimmed[1] == messages[1]
    assert trimmed[2] == messages[2]


def test_trim_conversation_history_30_turns_token_reduction():
    # Construct a 32-turn conversation with intermediate verbose tool outputs
    messages = [
        {"role": "system", "content": "You are an autonomous AI coding agent."},
        {"role": "user", "content": "Fix database connection leak in src/db/pool.py"},
    ]

    for i in range(12):
        messages.append(
            {
                "role": "assistant",
                "content": f"Step {i}: Inspecting file and running diagnostics.",
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json_dumps({"path": f"src/db/pool_{i}.py"}),
                        },
                    }
                ],
            }
        )
        # Large tool output (~300 tokens each)
        tool_output = "\n".join([f"def pool_method_{i}_{j}(): return {j}" for j in range(60)])
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{i}",
                "name": "read_file",
                "content": tool_output,
            }
        )

    # Add 6 recent turns
    for k in range(3):
        messages.append(
            {
                "role": "assistant",
                "content": f"Recent step {k}: Applying minimal patch to src/db/pool.py.",
                "tool_calls": [
                    {
                        "id": f"recent_call_{k}",
                        "type": "function",
                        "function": {
                            "name": "edit_file",
                            "arguments": json_dumps({"path": "src/db/pool.py"}),
                        },
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"recent_call_{k}",
                "name": "edit_file",
                "content": "File src/db/pool.py successfully edited with diff: +connection.close()",
            }
        )

    assert count_message_tokens(messages[0]) > 0
    raw_tokens = count_history_tokens(messages)
    trimmed = trim_conversation_history(messages, max_tokens=16000, keep_recent_turns=6)
    trimmed_tokens = count_history_tokens(trimmed)

    # 1. Token reduction must exceed 50%
    reduction_pct = (raw_tokens - trimmed_tokens) / raw_tokens
    assert reduction_pct >= 0.50, f"Expected >= 50% reduction, got {reduction_pct * 100:.1f}%"

    # 2. Pinned prefix preserved verbatim
    assert trimmed[0] == messages[0]
    assert trimmed[1] == messages[1]

    # 3. Recent 6 turns preserved verbatim
    assert trimmed[-6:] == messages[-6:]

    # 4. Intermediate tool outputs compacted to 1-line receipts with valid tool_call_id
    for msg in trimmed[2:-6]:
        if msg.get("role") == "tool":
            assert "omitted by usagetrim; ref: tc_" in msg["content"]
            assert msg["tool_call_id"].startswith("call_")

    # 5. Recovery via ContextCache works
    ref_match = trimmed[3]["content"].split("ref: ")[1].rstrip(")]")
    recovered = ContextCache().retrieve(ref_match)
    assert "pool_method_0" in recovered


def test_trim_conversation_history_tight_budget_milestone():
    messages = [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "content": "Architect the new authentication pipeline."},
    ]
    for i in range(15):
        messages.append(
            {
                "role": "assistant",
                "content": f"We decided to implement OAuth2 for component {i} in src/auth_{i}.py.",
            }
        )
        messages.append({"role": "tool", "name": "bash", "content": f"Build output {i}\n" * 30})

    messages.append({"role": "user", "content": "Now run final test."})
    messages.append({"role": "assistant", "content": "All tests passed cleanly."})

    # Strict token budget of 500 tokens
    budget = 500
    trimmed = trim_conversation_history(messages, max_tokens=budget, keep_recent_turns=2)

    assert count_history_tokens(trimmed) <= budget + 50
    # Pinned first prompt preserved
    assert trimmed[0]["role"] == "system"
    assert trimmed[1]["role"] == "user"
    assert "authentication pipeline" in trimmed[1]["content"]

    # Milestone checkpoint present
    contents = [str(m.get("content", "")) for m in trimmed]
    assert any("Milestone Checkpoint" in c for c in contents)


def test_trim_conversation_history_anthropic_tool_result_format():
    messages = [
        {"role": "user", "content": "Analyze performance."},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me profile the service."},
                {
                    "type": "tool_use",
                    "id": "t_prof",
                    "name": "profiler",
                    "input": {"target": "api"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t_prof",
                    "content": "\n".join([f"Flamegraph row {i}: 10ms" for i in range(100)]),
                }
            ],
        },
        {"role": "assistant", "content": "Optimization complete."},
    ]
    # keep_recent_turns=1 forces the middle turn into intermediate
    trimmed = trim_conversation_history(messages, max_tokens=1000, keep_recent_turns=1)
    tool_res_msg = trimmed[2]
    assert isinstance(tool_res_msg["content"], list)
    block = tool_res_msg["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "t_prof"
    assert "omitted by usagetrim; ref: tc_" in block["content"]


def test_trim_conversation_history_empty_and_single():
    assert trim_conversation_history([]) == []
    single = [{"role": "user", "content": "Hi"}]
    assert trim_conversation_history(single) == single


def json_dumps(obj):
    import json

    return json.dumps(obj)
