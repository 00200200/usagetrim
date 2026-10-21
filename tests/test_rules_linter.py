import pytest

from usagetrim.core.rules_linter import lint_rule_content, minify_rules, optimize_rules

SAMPLE_RULES_BAD = """
# Project Guidelines

You are a helpful coding assistant expert. Please make sure to always remember to never forget this.

Current Date: 2026-09-20T14:39:40
Session_id: 'abcdef0123456789abcdef'



- Always run tests before committing.
"""


def test_lint_detects_cache_busting():
    res = lint_rule_content(SAMPLE_RULES_BAD)
    assert not res.is_cache_friendly
    assert any(iss.rule_name == "cache-busting" for iss in res.issues)
    assert any(iss.rule_name == "verbose-boilerplate" for iss in res.issues)


def test_minify_rules():
    minified = minify_rules(SAMPLE_RULES_BAD)
    assert "<!--" not in minified
    assert "\n\n\n" not in minified


def test_optimize_rules_strips_boilerplate_and_aligns_cache():
    from usagetrim.core.rules_linter import optimize_rules

    res = optimize_rules(SAMPLE_RULES_BAD)
    assert res["optimized_tokens"] < res["original_tokens"]
    assert res["savings_pct"] > 0
    assert res["is_cache_friendly"] is False

    opt_text = res["optimized_content"]
    assert "You are a helpful coding assistant" not in opt_text
    assert "Please make sure to always remember" not in opt_text
    # Metadata under Project Guidelines must keep that section scope.
    assert "Dynamic Runtime Context" not in opt_text
    assert "Current Date: 2026-09-20T14:39:40" in opt_text
    assert "- Always run tests before committing." in opt_text


def test_generate_desktop_rules():
    from usagetrim.core.rules_linter import generate_desktop_rules

    claude_rules = generate_desktop_rules("claude")
    assert "UsageTrim Claude Rules" in claude_rules
    assert "usagetrim_read" in claude_rules
    assert "usagetrim_code" in claude_rules

    codex_rules = generate_desktop_rules("codex")
    assert "UsageTrim Codex Rules" in codex_rules
    assert "usagetrim_exec" in codex_rules


@pytest.mark.parametrize(
    "content",
    [
        "Please make sure to always remember to never forget to run `uv run pytest` and never use pip.\n",
        "You are a helpful assistant who must not modify `/etc/hosts`.\n",
        '```sh\nprintf "Please ensure never delete /srv/data"\n```\n',
        '- Run `echo "Please ensure the file exists"`.\n',
        "- parent\n    - child\n        - grandchild\n",
        "Please make sure to always remember to never forget https://example.com/admin and /srv/data.\n",
        "- If current time is unknown, do not deploy.\n- Otherwise run `deploy.sh`.\n",
        "---\nsettings:\n    outer:\n        inner: true\n---\n- Run tests.\n",
    ],
)
@pytest.mark.parametrize(
    "transform", [minify_rules, lambda text: optimize_rules(text)["optimized_content"]]
)
def test_rules_preserve_technical_structure(content, transform):
    assert transform(content) == content


def test_rules_existing_dynamic_footer_is_idempotent():
    content = "# Rules\n- Run tests.\n\n<!-- Dynamic Runtime Context (isolated for prompt caching) -->\n> Current Date: 2026-09-20T14:39:40\n"
    optimized = optimize_rules(content)["optimized_content"]
    assert optimize_rules(optimized)["optimized_content"] == optimized


@pytest.mark.parametrize(
    "name", ["CLAUDE.md", "AGENTS.md", ".cursorrules", "project.mdc", "copilot-instructions.md"]
)
def test_rules_formats_preserve_opaque_blocks_and_constraints(name):
    content = (
        "---\ndescription: Please ensure this exact text\nsettings:\n    child:\n        enabled: true\n---\n"
        "# Rules\n<!-- instruction marker: NEVER remove -->\n"
        "- MUST use uv; SHOULD test; MAY inspect.\n"
        "- Do not use pip unless authorized; only when offline, except production.\n"
        "- Paths: /srv/data and ./src/main.py; version v1.2.3; --locked.\n"
        "- URL: https://example.com/a?q=1\n"
        '    printf "Please ensure keep this"\n'
        '~~~~sh\nprintf "exact"\n~~~\n~~~~\n'
        "```md\n~~~nested\n```\n"
        "- Unicode: Zażółć e\u0301 🙂\n"
    )
    assert minify_rules(content) == content
    result = optimize_rules(content)["optimized_content"]
    assert result == content
    assert lint_rule_content(content, name).file_path == name


@pytest.mark.parametrize(
    "content",
    [
        '```python\nprint("never change")\n',
        "---\nmissing: end\n",
        "<!-- unclosed instruction\nPlease ensure never remove\n",
        "````md\n```\nPlease ensure this stays\n````\n",
    ],
)
def test_rules_uncertain_or_long_fences_are_opaque(content):
    assert optimize_rules(content)["optimized_content"] == content


def test_rules_dedup_is_local_and_conservative():
    content = (
        "# One\n- Run tests.\n- Run  tests.\n"
        "- NEVER deploy.\n- MAY deploy.\n- Deploy only if approved.\n"
        "- Deploy unless prohibited.\n"
        '- Run `echo "a  b"`.\n- Run `echo "a b"`.\n'
        "- Parent\n    - child\n- Parent\n    - other child\n"
        "# Two\n- Run tests.\n"
    )
    output = optimize_rules(content)["optimized_content"]
    assert output.count("- Run tests.") == 2
    assert "- Run  tests." not in output
    for line in content.splitlines()[3:-2]:
        assert line in output
    assert optimize_rules(output)["optimized_content"] == output


def test_rules_distinct_quoted_paths_and_numbered_steps_not_deduplicated():
    content = "- Read /a  b.\n- Read /a b.\n1. Run tests.\n1. Run tests.\n"
    assert optimize_rules(content)["optimized_content"] == content


def test_rules_only_explicit_metadata_moves_and_stabilizes_prefix():
    def document(date):
        return f'# Dynamic Runtime Context\nCurrent Date: {date}\nSession_id: "abcdef0123456789"\n\n# Static\n- Run tests.\n'

    first = optimize_rules(document("2026-09-20T14:39:40"))["optimized_content"]
    second = optimize_rules(document("2026-09-21T14:39:40"))["optimized_content"]
    assert first.startswith("# Static\n- Run tests.\n\n# Dynamic Runtime Context\n")
    assert (
        first.split("# Dynamic Runtime Context")[0] == second.split("# Dynamic Runtime Context")[0]
    )
    assert optimize_rules(first)["optimized_content"] == first
    assert lint_rule_content(first).is_cache_friendly


@pytest.mark.parametrize(
    "line",
    [
        "Version: v1.2.3",
        "- If current time is unknown, do not deploy.",
        "Machine path: /home/user/project",
    ],
)
def test_rules_unmarked_volatile_content_stays_put(line):
    content = line + "\n# Rules\n- Run tests.\n"
    assert optimize_rules(content)["optimized_content"] == content
    assert lint_rule_content(content).issues


def test_rules_unsafe_dynamic_section_is_not_moved_or_ignored_by_linter():
    content = "# Dynamic Runtime Context\n- If current time is unknown, do not deploy.\n# Static\nNever deploy before Current Date: 2026-09-20T14:39:40\n"
    assert optimize_rules(content)["optimized_content"] == content
    issues = lint_rule_content(content).issues
    assert any(issue.rule_name == "dynamic-instruction" for issue in issues)
    assert any(issue.rule_name == "cache-busting" and issue.line_num == 4 for issue in issues)


def test_rules_whitespace_hard_breaks_and_unicode_are_stable():
    content = "\r\n# Rules  \r\n\r\n\r\n- Żółć e\u0301  \r\n  Continue.\t \r\n\r\n"
    expected = "# Rules  \n\n- Żółć e\u0301  \n  Continue.\n"
    assert minify_rules(content) == expected
    assert optimize_rules(content)["optimized_content"] == expected
    assert optimize_rules(expected)["optimized_content"] == expected


@pytest.mark.parametrize(
    "client,title",
    [
        ("claude", "Claude"),
        ("codex", "Codex"),
        ("codex-desktop", "Codex"),
        ("cursor", "Cursor"),
        ("copilot", "Copilot"),
    ],
)
def test_rules_clients_are_explicit(client, title):
    from usagetrim.core.rules_linter import generate_desktop_rules

    assert generate_desktop_rules(client).startswith(f"# UsageTrim {title} Rules\n")


@pytest.mark.parametrize("client", ["unknown", "", None, True])
def test_rules_invalid_client_rejected(client):
    from usagetrim.core.rules_linter import generate_desktop_rules

    with pytest.raises(ValueError):
        generate_desktop_rules(client)


VERBOSE_RULES = """Hello Claude!
Welcome to the project.
You are a helpful coding assistant.
You are an expert AI assistant.
You are a world-class software assistant.
As an AI language model, you are a helpful assistant.
Please make sure to always remember to never forget this.

# Technical rules
- MUST run `uv run pytest` after changes.
- NEVER use pip/poetry.
- Keep `/srv/data` intact.
- If tests fail, run `uv run pytest -x`.
"""


def test_rules_verbose_fixture_reduction_preserves_every_technical_rule():
    from usagetrim.metrics.tokenizer import count_tokens

    result = optimize_rules(VERBOSE_RULES)
    output = result["optimized_content"]
    for line in VERBOSE_RULES.splitlines():
        if line.startswith(("#", "-")):
            assert line in output
    assert result["original_tokens"] == count_tokens(VERBOSE_RULES).openai
    assert result["optimized_tokens"] == count_tokens(output).openai
    assert result["savings_pct"] >= 50
    assert result["token_delta"] == result["original_tokens"] - result["optimized_tokens"]
    assert optimize_rules(output)["optimized_content"] == output


def test_rules_signed_expansion_and_cost_are_not_hidden():
    from usagetrim.metrics.pricing import estimate_savings
    from usagetrim.metrics.tokenizer import count_tokens

    result = optimize_rules("")
    before, after = count_tokens(""), count_tokens(result["optimized_content"])
    assert result["token_delta"] == before.openai - after.openai
    assert result["token_delta"] < 0
    assert result["savings_pct"] == 0  # no denominator for empty input
    assert result["saved_tokens"] == 0
    assert (
        result["estimated_baseline_input_cost_delta"]
        == estimate_savings(
            before.claude - after.claude, before.openai - after.openai, before.gemini - after.gemini
        ).avg_saved_usd
    )
    assert result["estimated_baseline_input_cost_delta"] < 0


def test_rules_indented_code_preserves_internal_blank_lines_and_list_like_text():
    content = "# Code\n\n    Please ensure this text stays\n\n\n    - Run tests.\n    - Run tests.\n\n# End\n"
    assert optimize_rules(content)["optimized_content"] == content
    assert minify_rules(content) == content


def test_rules_setext_heading_is_never_mistaken_for_persona_prose():
    content = "You are a helpful assistant.\n===========================\n- Follow this section.\n"
    assert optimize_rules(content)["optimized_content"] == content


@pytest.mark.parametrize("value", ["MUST", "never", "if", '"unclosed'])
def test_dynamic_ambiguous_values_are_not_reordered(value):
    content = f"# Dynamic Runtime Context\nSession_id: {value}\n# Rules\n- Run tests.\n"
    assert optimize_rules(content)["optimized_content"] == content
    assert any(
        issue.rule_name == "dynamic-instruction" for issue in lint_rule_content(content).issues
    )


def test_rules_duplicates_in_separate_lists_remain():
    content = "- Run tests.\n\n- Run tests.\n"
    assert optimize_rules(content)["optimized_content"] == content


def test_rules_persona_with_semantic_hard_break_is_not_removed():
    content = "Hello Claude!  \n- Run tests.\n"
    assert optimize_rules(content)["optimized_content"] == content


def test_rules_nonempty_expansion_has_signed_percentage():
    result = optimize_rules("x")
    assert result["token_delta"] < 0
    assert result["savings_pct"] < 0
    assert result["estimated_baseline_input_cost_delta"] < 0


@pytest.mark.parametrize(
    "date,session",
    [
        ("2026-10-20T22:00:00", "abcdef0123456789"),
        ("2026-10-21T23:00:00", "fedcba9876543210"),
    ],
)
def test_rules_standalone_metadata_canonical_suffix_and_stable_prefix(date, session):
    marker = "# Dynamic Runtime Context"
    content = f"Current Date: {date}\nSession_id: {session}\n# Rules\n- Run tests.\n"
    expected_prefix = "# Rules\n- Run tests.\n\n"
    expected = expected_prefix + marker + f"\nCurrent Date: {date}\nSession_id: {session}\n"
    output = optimize_rules(content)["optimized_content"]
    assert output == expected
    assert output.split(marker)[0].encode() == expected_prefix.encode()
    assert optimize_rules(output)["optimized_content"] == output
    assert minify_rules(content) == content


@pytest.mark.parametrize(
    "line",
    [
        "Current Time: 2026-10-20T22:00:00",
        "Timestamp: 2026-10-20T22:00:00",
        "Generated Date: 2026-10-20",
        "Session ID: abcdef0123456789",
        "Request ID: request-123",
        "Request_id: request-123",
    ],
)
def test_rules_standalone_metadata_whitelist(line):
    content = line + "\n# Rules\n- Run tests.\n"
    output = optimize_rules(content)["optimized_content"]
    assert output.startswith("# Rules\n- Run tests.\n\n# Dynamic Runtime Context")
    assert output.endswith(line + "\n")
    assert optimize_rules(output)["optimized_content"] == output


@pytest.mark.parametrize(
    "line",
    [
        "- If current time is unknown, do not deploy.",
        "Use version 3.2 for production.",
        "Version: 3.2",
        "Machine path: /home/user/project",
        "Project root: /srv/app",
        "Build ID: release-42",
        "Never deploy before Current Date: 2026-10-20",
        "`Current Date: 2026-10-20`",
        "- Current Date: 2026-10-20",
        "  Current Date: 2026-10-20",
        "Current Date: 2026-10-20 then deploy",
        "Session_id: never",
        "Request ID: `deploy`",
    ],
)
def test_rules_standalone_metadata_never_captures_instructions_or_other_keys(line):
    content = line + "\n# Rules\n- Run tests.\n"
    assert optimize_rules(content)["optimized_content"] == content


@pytest.mark.parametrize(
    "wrapper",
    [
        "```\n{}\n```\n",
        "~~~\n{}\n~~~\n",
        "    {}\n",
        "<!--\n{}\n-->\n",
        "---\n{}\n---\n",
        "- Rule paragraph\n{}\n",
    ],
)
def test_rules_standalone_metadata_respects_protected_and_list_context(wrapper):
    content = wrapper.format("Current Date: 2026-10-20")
    assert optimize_rules(content)["optimized_content"] == content


def test_rules_standalone_metadata_merges_existing_safe_dynamic_blocks():
    marker = "# Dynamic Runtime Context"
    content = (
        "Current Date: 2026-10-20\n" + marker + "\nSession_id: abcdef0123456789\n"
        "# Rules\n- Run tests.\n\n" + marker + "\nRequest ID: request-123\n"
    )
    output = optimize_rules(content)["optimized_content"]
    assert output.count("Dynamic Runtime Context") == 1
    assert (
        output
        == "# Rules\n- Run tests.\n\n"
        + marker
        + "\nCurrent Date: 2026-10-20\nSession_id: abcdef0123456789\nRequest ID: request-123\n"
    )
    assert optimize_rules(output)["optimized_content"] == output


@pytest.mark.parametrize(
    "transform", [minify_rules, lambda text: optimize_rules(text)["optimized_content"]]
)
def test_review_dynamic_metadata_retains_parent_section_scope(transform):
    content = "# Staging\n## Dynamic Runtime Context\nWorkspace: /srv/staging\n\n# Production\n## Dynamic Runtime Context\nWorkspace: /srv/production\n"
    output = transform(content)
    assert output == content
    staging, production = output.split("# Production\n")
    assert "/srv/staging" in staging and "/srv/production" not in staging
    assert "/srv/production" in production and "/srv/staging" not in production
    assert transform(output) == output


@pytest.mark.parametrize("delimiter", ["`", "``", "````"])
def test_review_multiline_inline_code_is_atomic(delimiter):
    content = f"Prose {delimiter}BEGIN\nHello Claude!\nEND{delimiter}\n"
    for transform in (minify_rules, lambda text: optimize_rules(text)["optimized_content"]):
        assert transform(content) == content
        assert transform(transform(content)) == content


def test_review_exact_multiline_inline_code():
    content = "`BEGIN\nHello Claude!\nEND`\n"
    assert optimize_rules(content)["optimized_content"] == content


@pytest.mark.parametrize(
    "continuation",
    [
        "with coverage before release.",
        "  with coverage before release.",
        "    - with coverage before release.",
        "    ```sh\n    uv run pytest --cov\n    ```",
        "    uv run pytest --cov",
        "Hello Claude!",
    ],
)
def test_review_list_item_with_continuation_is_not_a_simple_duplicate(continuation):
    content = "- Run tests.\n- Run tests.\n" + continuation + "\n"
    assert optimize_rules(content)["optimized_content"] == content
    assert (
        optimize_rules(optimize_rules(content)["optimized_content"])["optimized_content"] == content
    )


def test_review_complete_simple_list_items_can_still_be_deduplicated():
    content = "- Run tests.\n- Run tests.\n"
    assert optimize_rules(content)["optimized_content"] == "- Run tests.\n"


@pytest.mark.parametrize("header", ["# Staging", "Staging\n=======", "## Staging"])
@pytest.mark.parametrize("metadata", ["Current Date: 2026-10-20", "Session_id: staging123"])
def test_review_standalone_metadata_inside_heading_scope_is_not_relocated(header, metadata):
    content = header + "\n" + metadata + "\n# Production\n- Run tests.\n"
    assert optimize_rules(content)["optimized_content"] == content


@pytest.mark.parametrize(
    "marker",
    [
        "## Dynamic Runtime Context",
        "<!-- Dynamic Runtime Context (isolated for prompt caching) -->",
    ],
)
def test_review_dynamic_scope_is_not_reconstructed_from_rendered_marker(marker):
    content = (
        "# Staging\n"
        + marker
        + "\nWorkspace: /srv/staging\n\n# Production\n"
        + marker
        + "\nWorkspace: /srv/production\n"
    )
    assert optimize_rules(content)["optimized_content"] == content


@pytest.mark.parametrize("delimiter", ["`", "``"])
def test_review_unclosed_inline_code_keeps_uncertain_remainder(delimiter):
    content = "Prose " + delimiter + "BEGIN\nHello Claude!\nCurrent Date: 2026-10-20\n"
    assert optimize_rules(content)["optimized_content"] == content


def test_review_inline_code_delimiter_lengths_and_literal_backticks():
    content = "Text ``BEGIN\n`\nHello Claude!\nEND``\n"
    assert optimize_rules(content)["optimized_content"] == content


def test_review_child_heading_belongs_to_complete_list_item():
    content = "- Run tests.\n- Run tests.\n  ## Coverage\n  with coverage before release.\n"
    assert optimize_rules(content)["optimized_content"] == content


@pytest.mark.parametrize(
    "content",
    [
        "`BEGIN <!-- marker -->\nHello Claude!\nEND`\n",
        "<!-- `BEGIN\n`\nHello Claude!\n-->\n",
        "<!-- marker --> `BEGIN\nHello Claude!\nEND`\n",
        "`BEGIN\n---\nHello Claude!\nEND`\n",
    ],
)
def test_review_overlapping_opaque_boundaries_never_expose_code_or_comment_content(content):
    assert optimize_rules(content)["optimized_content"] == content


@pytest.mark.parametrize("fence", ["```", "~~~"])
@pytest.mark.parametrize("gap", ["\n", ""])
def test_block_bound_inline_cannot_consume_following_fence(fence, gap):
    block = f"{fence}text\nsome ` literal inside fence\nHello Claude!\n{fence}\n"
    content = "Paragraph with unmatched ` literal\n" + gap + block
    for transform in (minify_rules, lambda text: optimize_rules(text)["optimized_content"]):
        output = transform(content)
        assert output == content
        assert block in output
        assert transform(output) == output


@pytest.mark.parametrize("parent_level", [1, 2])
@pytest.mark.parametrize("comment", [" <!-- env -->", ""])
def test_heading_with_inline_html_keeps_ancestry(parent_level, comment):
    from usagetrim.core.rules_linter import _scan_rules

    parent = "#" * parent_level
    child = "#" * (parent_level + 1)
    content = f"{parent} Staging{comment}\n{child} Dynamic Runtime Context\nWorkspace: /srv/staging\n\n{parent} Production{comment}\n{child} Dynamic Runtime Context\nWorkspace: /srv/production\n"
    output = optimize_rules(content)["optimized_content"]
    assert output == content
    staging, production = output.split(f"{parent} Production{comment}\n")
    assert "/srv/staging" in staging and "/srv/production" not in staging
    assert "/srv/production" in production and "/srv/staging" not in production
    dynamic = [block for block in _scan_rules(content) if block.kind == "dynamic"]
    assert [block.scope for block in dynamic] == [(1,), (5,)]
    assert optimize_rules(output)["optimized_content"] == output


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_fence_remains_an_independent_atomic_scanner_block(fence):
    from usagetrim.core.rules_linter import _scan_rules

    body = [fence + "text", "some ` literal", "Hello Claude!", fence]
    blocks = _scan_rules("Paragraph ` literal\n\n" + "\n".join(body) + "\n")
    assert any(block.kind == "protected" and block.lines == body for block in blocks)
    assert blocks[0].lines == ["Paragraph ` literal"]
    assert blocks[0].uncertain


def test_child_heading_inline_comment_keeps_full_parent_chain():
    from usagetrim.core.rules_linter import _scan_rules

    content = "## Staging <!-- env -->\n### Child <!-- note -->\n#### Dynamic Runtime Context\nWorkspace: /srv/staging\n\n## Production <!-- env -->\n### Child <!-- note -->\n#### Dynamic Runtime Context\nWorkspace: /srv/production\n"
    assert optimize_rules(content)["optimized_content"] == content
    dynamic = [block for block in _scan_rules(content) if block.kind == "dynamic"]
    assert [block.scope for block in dynamic] == [(1, 2), (6, 7)]


@pytest.mark.parametrize(
    "content",
    [
        "Paragraph `BEGIN\nHello Claude!\nEND`\n\n```text\nsome ` literal\nHello Claude!\n```\n",
        "Paragraph ``BEGIN\n`\nHello Claude!\nEND``\n\n~~~text\n` literal\nHello Claude!\n~~~\n",
        "Paragraph `BEGIN <!-- marker -->\nHello Claude!\nEND`\n",
        "<!-- marker --> `BEGIN\nHello Claude!\nEND`\n",
        "Staging <!-- env -->\n==================\n## Dynamic Runtime Context\nWorkspace: /srv/staging\n",
        "- Run tests.\n- Run tests.\n    ~~~text\n    some ` literal\n    Hello Claude!\n    ~~~\n",
    ],
)
def test_targeted_block_collision_self_review_preserves_content(content):
    for transform in (minify_rules, lambda text: optimize_rules(text)["optimized_content"]):
        assert transform(content) == content
        assert transform(transform(content)) == content
