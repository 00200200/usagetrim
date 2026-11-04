from __future__ import annotations

import pytest

from usagetrim.core.prompt_optimizer import align_prompt, lint_prompt, minify_prompt


@pytest.mark.parametrize(
    "text",
    [
        "session_id: abc\n=======\nKeep all rules.\n",
        "session_id: abc\n-------\nKeep all rules.\n",
        "session_id: abc\nContinuation of the heading\n=======\nKeep rules.\n",
        "session_id: abc\n    code child\nKeep rules.\n",
        "- session_id: abc\n  Keep its child instruction.\n",
        "session_id: abc\n- Keep the first rule.\n  - Keep the nested rule.\n",
        "```text\nsession_id: abc\nKeep code exactly.\n```\n",
        "~~~text\nsession_id: abc\nKeep code exactly.\n~~~\n",
        ">session_id: abc\n> Keep the quoted instruction.\n",
        "---\nsession_id: abc\n---\nKeep rules.\n",
        "<!--\nsession_id: abc\n-->\nKeep rules.\n",
        "session_id: abc\n# Parent section\nKeep rules.\n",
        "session_id: abc\nIf current time is unknown, never deploy.\nKeep rules.\n",
    ],
)
def test_model_alignment_preserves_ambiguous_markdown_blocks(text):
    from usagetrim.core.prompt_optimizer import align_to_cache_boundary

    assert align_to_cache_boundary(text, 1, model="generic") == text
    assert align_prompt(text, model="generic").aligned_text == text


def test_boundary_alignment_preserves_order_of_plain_instructions():
    from usagetrim.core.prompt_optimizer import align_to_cache_boundary

    text = "session_id: abc\nrequest_id: request-123\nFirst validate.\nThen deploy.\n"
    output = align_to_cache_boundary(text, 1, model="generic")
    assert output.index("First validate.") < output.index("Then deploy.")
    assert output.index("Then deploy.") < output.index("session_id: abc")
    assert output.index("session_id: abc") < output.index("request_id: request-123")
    assert "First validate.\nThen deploy." in output


def test_minification_redacts_secret_created_by_comment_removal():
    text = "ghp_<!-- separator -->" + "A" * 36
    output = minify_prompt(text, model="generic").minified_text
    assert "A" * 36 not in output
    assert "REDACTED" in output


@pytest.mark.parametrize("length,reached", [(12, False), (13, True), (17, True)])
def test_align_boundary_measures_static_prefix_only(length, reached):
    from usagetrim.core.prompt_optimizer import align_to_cache_boundary

    static = "x" * length
    text = "session_id: abcdef0123456789\n" + static + "\n"
    result = align_to_cache_boundary(text, 4, model="generic")
    if reached:
        assert result == align_prompt(text, model="generic").aligned_text
        assert result.startswith(static)
        assert "session_id: abcdef0123456789" in result
    else:
        assert result == text


@pytest.mark.parametrize("boundary", [0, -1, True, 1.5, "1024", None])
def test_align_boundary_rejects_invalid_threshold(boundary):
    from usagetrim.core.prompt_optimizer import align_to_cache_boundary

    with pytest.raises(ValueError, match="positive integer"):
        align_to_cache_boundary("", boundary)


@pytest.mark.parametrize("key", ["request", "session", "turn", "conversation"])
def test_align_boundary_preserves_ambiguous_instruction_fields(key):
    from usagetrim.core.prompt_optimizer import align_to_cache_boundary

    text = f"{key}: deploy\n" + "Keep all safety constraints.\n" * 30
    assert align_to_cache_boundary(text, 4, model="generic") == text


@pytest.mark.parametrize("key", ["session_id", "request_id", "turn_id", "conversation_id"])
def test_align_boundary_can_relocate_unambiguous_identifiers(key):
    from usagetrim.core.prompt_optimizer import align_to_cache_boundary

    static = "Keep all safety constraints.\n" * 30
    text = f"{key}: abc123\n" + static
    result = align_to_cache_boundary(text, 4, model="generic")
    assert result == align_prompt(text, model="generic").aligned_text
    assert result.startswith(static.rstrip())
    assert f"{key}: abc123" in result


def test_align_boundary_rejects_unknown_model():
    from usagetrim.core.prompt_optimizer import align_to_cache_boundary

    with pytest.raises(ValueError, match="Unknown model"):
        align_to_cache_boundary("", model="unknown")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "session_id: " + "a" * 200 + "\nx\n",
        "If current time is unknown, do not deploy.\n" + "Keep rules.\n" * 20,
        "# Staging\nsession_id: abc\n" + "Keep rules.\n" * 20,
        "session_id: abc\nKeep this hard break.  \n",
    ],
)
def test_align_boundary_preserves_insufficient_or_unsafe_input(text):
    from usagetrim.core.prompt_optimizer import align_to_cache_boundary

    assert align_to_cache_boundary(text, 4, model="generic") == text


@pytest.mark.parametrize("model", ["openai", "claude", "gemini", "llama", "generic"])
def test_align_boundary_unicode_offline_without_padding(monkeypatch, model):
    import tiktoken.load
    import tiktoken.registry

    from usagetrim.core.prompt_optimizer import align_to_cache_boundary
    from usagetrim.metrics import tokenizer

    def forbidden(*args, **kwargs):
        pytest.fail("network or vocabulary loading attempted")

    monkeypatch.setattr(tiktoken.load, "read_file", forbidden)
    monkeypatch.setattr(tokenizer.tiktoken, "get_encoding", forbidden)
    monkeypatch.setattr(tokenizer, "_O200K_ENC", None)
    monkeypatch.setattr(tokenizer, "_CL100K_ENC", None)
    monkeypatch.setattr(tiktoken.registry, "ENCODINGS", {})
    text = "Current time: 2026-11-03T12:00:00Z\n" + "🦙漢字" * 20 + "\n"
    result = align_to_cache_boundary(text, 20, model=model)
    assert result == align_prompt(text, model=model).aligned_text
    assert result.startswith("🦙漢字" * 20)
    assert sorted(line for line in text.splitlines() if line) == sorted(
        line for line in result.splitlines() if line and not line.startswith("<!--")
    )


@pytest.mark.parametrize("model", ["openai", "claude", "gemini", "llama", "generic"])
@pytest.mark.parametrize(
    "text",
    ["", "   ", "Keep 漢字🙂.\n", "Current time: 12:00\nPlease ensure you run tests.\n"],
)
def test_prompt_model_statistics_transforms_and_offline(monkeypatch, model, text):
    import tiktoken.load
    import tiktoken.registry

    from usagetrim.core import prompt_optimizer as prompts
    from usagetrim.metrics import tokenizer

    def forbidden(*args, **kwargs):
        pytest.fail("model prompt counting must not load vocabulary or use legacy counting")

    monkeypatch.setattr(
        prompts,
        "count_tokens",
        lambda value: tokenizer.TokenCount(len(value), len(value), len(value)),
    )
    legacy_aligned = prompts.align_prompt(text)
    legacy_minified = prompts.minify_prompt(text)
    legacy_stabilized = prompts.stabilize_cache_prefix(text)
    assert prompts.align_prompt(text, model=None) == legacy_aligned
    assert prompts.minify_prompt(text, model=None) == legacy_minified
    assert prompts.stabilize_cache_prefix(text, model=None) == legacy_stabilized

    monkeypatch.setattr(prompts, "count_tokens", forbidden)
    monkeypatch.setattr(tiktoken.load, "read_file", forbidden)
    monkeypatch.setattr(tokenizer.tiktoken, "get_encoding", forbidden)
    monkeypatch.setattr(tokenizer, "_O200K_ENC", None)
    monkeypatch.setattr(tokenizer, "_CL100K_ENC", None)
    monkeypatch.setattr(tiktoken.registry, "ENCODINGS", {})
    aligned = prompts.align_prompt(text, model=model)
    minified = prompts.minify_prompt(text, model=model)
    assert aligned.aligned_text == legacy_aligned.aligned_text
    assert minified.minified_text == legacy_minified.minified_text
    assert prompts.stabilize_cache_prefix(text, model=model) == legacy_stabilized
    before = tokenizer.count_model_tokens(text, model).count
    after = tokenizer.count_model_tokens(minified.minified_text, model).count
    assert aligned.original_tokens == minified.original_tokens == before
    assert aligned.aligned_tokens == tokenizer.count_model_tokens(aligned.aligned_text, model).count
    assert minified.minified_tokens == after
    assert minified.saved_tokens == max(0, before - after)
    assert minified.reduction_pct == (
        round(100 * minified.saved_tokens / before, 1) if before else 0.0
    )


@pytest.mark.parametrize("text", ["", "Keep instructions."])
@pytest.mark.parametrize("function", ["align_prompt", "minify_prompt", "stabilize_cache_prefix"])
def test_prompt_functions_reject_unknown_model(function, text):
    from usagetrim.core import prompt_optimizer as prompts

    with pytest.raises(ValueError, match="Unknown model"):
        getattr(prompts, function)(text, model="unknown")


def test_lint_clean_prompt():
    prompt = (
        "You are an expert Python assistant.\n"
        "Follow clean code standards and write unit tests.\n"
        "Always use typer for CLI tools.\n"
    )
    res = lint_prompt(prompt)
    assert res["cacheability_score"] == 100
    assert len(res["issues"]) == 0


def test_lint_volatile_prompt():
    prompt = (
        "Current time: 2026-09-21T08:52:10Z\n"
        "session_id: 12345-abcde\n"
        "You are a coding assistant.\n"
        "Here are the rules to follow.\n"
    )
    res = lint_prompt(prompt)
    assert res["cacheability_score"] < 100
    assert any("timestamp" in i.lower() for i in res["issues"])
    assert any("session" in i.lower() for i in res["issues"])


def test_align_prompt_restructures_volatile_prefix():
    prompt = (
        "Today's date is September 21, 2026\n"
        "request_id: req_987654321\n"
        "You are an AI assistant specialized in context optimization.\n"
        "Rule 1: Always preserve comments.\n"
        "Rule 2: Minimize token waste.\n"
    )
    res = align_prompt(prompt)
    assert res.cacheability_score >= 80
    assert "DYNAMIC RUNTIME CONTEXT" in res.aligned_text
    # Static rules should come first
    assert res.aligned_text.startswith("You are an AI assistant")
    # Date and request ID should be isolated at the end
    assert "Today's date is September 21, 2026" in res.aligned_text


def test_minify_prompt_collapses_fluff_and_comments():
    raw_prompt = """<!-- Global Guidelines -->
You are a helpful and expert AI coding assistant.
Please make sure to always remember to run tests before committing.
It is extremely important that you never commit secrets.

<!-- Environment Notes -->
Under no circumstances should you ever delete production databases.

- Follow Clean Code
- Follow Clean Code
"""
    res = minify_prompt(raw_prompt)
    assert res.reduction_pct > 0
    assert res.saved_tokens > 0
    assert "Global Guidelines" not in res.minified_text
    assert "Always run tests before committing." in res.minified_text
    assert "never commit secrets." in res.minified_text
    assert "Never delete production databases." in res.minified_text
    assert res.minified_text.count("- Follow Clean Code") == 1


def test_inject_prompt_cache_breakpoints_under_min_tokens():
    from usagetrim.core.prompt_optimizer import inject_prompt_cache_breakpoints

    tools = [
        {"name": "fetch_weather", "description": "Get weather forecast"},
        {"name": "calculator", "description": "Compute math expressions"},
    ]
    messages = [
        {"role": "user", "content": "What is 2 + 2?"},
        {"role": "assistant", "content": "2 + 2 = 4"},
    ]

    res = inject_prompt_cache_breakpoints(messages, tools, min_tokens=1024)

    assert res.breakpoints_placed == 0
    # Tools should still be stabilized alphabetically by name
    assert [t["name"] for t in res.tools] == ["calculator", "fetch_weather"]
    assert "cache_control" not in res.tools[-1]
    assert "cache_control" not in res.messages[-1]


def test_inject_prompt_cache_breakpoints_alphabetical_tool_stabilization():
    from usagetrim.core.prompt_optimizer import inject_prompt_cache_breakpoints

    tools = [
        {"name": "zebra_search", "description": "Search zebra database"},
        {"name": "alpha_query", "description": "Query alpha database"},
        {"name": "middle_fetch", "description": "Fetch middle records"},
    ]
    messages = [{"role": "user", "content": "Hello"}]

    res = inject_prompt_cache_breakpoints(messages, tools, min_tokens=100)

    # Alphabetical order: alpha_query, middle_fetch, zebra_search
    assert [t["name"] for t in res.tools] == ["alpha_query", "middle_fetch", "zebra_search"]


def test_inject_prompt_cache_breakpoints_large_tools():
    from usagetrim.core.prompt_optimizer import inject_prompt_cache_breakpoints

    # Create a large tool schema (> 1024 tokens)
    large_desc = "Detailed documentation for complex API operations. " * 80
    tools = [
        {"name": "db_query", "description": large_desc, "input_schema": {"type": "object"}},
        {"name": "api_call", "description": large_desc, "input_schema": {"type": "object"}},
    ]
    messages = [{"role": "user", "content": "Query the database"}]

    res = inject_prompt_cache_breakpoints(messages, tools, min_tokens=500)

    # Breakpoint should be placed on the last tool
    assert res.breakpoints_placed >= 1
    assert "cache_control" in res.tools[-1]
    assert res.tools[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in res.tools[0]


def test_inject_prompt_cache_breakpoints_multi_turn_and_max_cap():
    from usagetrim.core.prompt_optimizer import inject_prompt_cache_breakpoints

    # Generate 10 long conversational turns
    messages = []
    for i in range(10):
        long_text = (
            f"Turn {i} conversation context with extensive domain instructions and code. " * 40
        )
        messages.append({"role": "user" if i % 2 == 0 else "assistant", "content": long_text})

    res = inject_prompt_cache_breakpoints(messages, min_tokens=500, max_breakpoints=4)

    # Maximum 4 breakpoints enforced
    assert res.breakpoints_placed <= 4
    assert res.breakpoints_placed >= 1

    # The last message should have a cache control breakpoint
    last_msg = res.messages[-1]
    assert isinstance(last_msg["content"], list)
    assert any("cache_control" in block for block in last_msg["content"])


def test_inject_prompt_cache_breakpoints_tuple_unpacking():
    from usagetrim.core.prompt_optimizer import inject_prompt_cache_breakpoints

    messages = [{"role": "user", "content": "Long input prompt text. " * 50}]
    tools = [{"name": "tool1", "description": "desc"}]

    res = inject_prompt_cache_breakpoints(messages, tools, min_tokens=100)

    # Standard 2-tuple unpacking must work seamlessly
    out_messages, out_tools = res
    assert isinstance(res, tuple)
    assert len(res) == 2
    assert out_messages == res.messages
    assert out_tools == res.tools
    assert res.total_tokens > 0

    as_dict = res.to_dict()
    assert "messages" in as_dict
    assert "tools" in as_dict
    assert "breakpoints_placed" in as_dict
