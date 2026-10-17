from __future__ import annotations

from usagetrim.core.prompt_optimizer import align_prompt, lint_prompt, minify_prompt


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
