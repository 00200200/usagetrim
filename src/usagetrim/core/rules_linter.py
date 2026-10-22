from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from usagetrim.metrics.tokenizer import count_tokens

CACHE_BUSTING_PATTERNS = [
    (
        re.compile(
            r"\b(?:current\s+(?:time|date|timestamp)|now\(\)|datetime\.now)\b", re.IGNORECASE
        ),
        "Dynamic timestamp breaks Anthropic / Gemini prompt caching!",
    ),
    (
        re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\b"),
        "Hardcoded ISO timestamp in instructions.",
    ),
    (
        re.compile(
            r"\b(?:session_id|uuid|request_id)\s*[:=]\s*['\"][0-9a-fA-F-]{16,}['\"]",
            re.IGNORECASE,
        ),
        "Dynamic UUID in prompt prefix invalidates prompt cache.",
    ),
]

VERBOSE_BOILERPLATE_PATTERNS = [
    (
        re.compile(
            r"You are a helpful (?:coding )?assistant.*?(?:expert|world-class).*?\.", re.IGNORECASE
        ),
        "Verbose persona intro wastes tokens with zero impact on reasoning.",
    ),
    (
        re.compile(r"Please make sure to always remember to never forget.*?\.", re.IGNORECASE),
        "Hyper-verbose phrasing.",
    ),
]


@dataclass
class RuleIssue:
    line_num: int
    rule_name: str
    message: str
    snippet: str


PROMPT_CACHE_MIN_TOKENS: int = 1024


@dataclass
class LintResult:
    file_path: str
    token_count: int
    line_count: int
    issues: list[RuleIssue]
    is_cache_friendly: bool
    exceeds_cache_threshold: bool = False
    tokens_to_cache_threshold: int = 0
    cache_advice: str = ""


def lint_rule_content(content: str, file_name: str = "rules") -> LintResult:
    lines = content.splitlines()
    issues: list[RuleIssue] = []
    is_cache_friendly = True
    for block in _scan_rules(content):
        if block.kind == "protected" or (block.kind == "dynamic" and _safe_dynamic(block)):
            continue
        if block.kind == "dynamic":
            issues.append(
                RuleIssue(
                    block.line,
                    "dynamic-instruction",
                    "Dynamic context contains instructions or uncertain data; kept in place.",
                    block.lines[0],
                )
            )
            is_cache_friendly = False
        for offset, line in enumerate(block.lines):
            for pat, desc in CACHE_BUSTING_PATTERNS:
                if pat.search(line):
                    issues.append(
                        RuleIssue(block.line + offset, "cache-busting", desc, line.strip()[:60])
                    )
                    is_cache_friendly = False
            if re.search(
                r"(?:^|\s)(?:/[^\s]+|[A-Za-z]:\\[^\s]+)|\b(?:version|build[_ -]?id|generated date|session[_ ]?id)\s*[:=]",
                line,
                re.I,
            ):
                issues.append(
                    RuleIssue(
                        block.line + offset,
                        "volatile-advisory",
                        "Potential environment/version data: verify scope; not moved automatically.",
                        line.strip()[:60],
                    )
                )
            for pat, desc in VERBOSE_BOILERPLATE_PATTERNS:
                if pat.search(line):
                    issues.append(
                        RuleIssue(
                            block.line + offset, "verbose-boilerplate", desc, line.strip()[:60]
                        )
                    )

    # Check consecutive empty lines
    consecutive_empty = 0
    for idx, line in enumerate(lines, 1):
        if not line.strip():
            consecutive_empty += 1
            if consecutive_empty > 2:
                issues.append(
                    RuleIssue(
                        line_num=idx,
                        rule_name="excessive-newlines",
                        message="Multiple blank lines waste tokens",
                        snippet="<blank line>",
                    )
                )
        else:
            consecutive_empty = 0

    tokens = count_tokens(content).openai
    exceeds_threshold = tokens >= PROMPT_CACHE_MIN_TOKENS
    tokens_to_threshold = max(0, PROMPT_CACHE_MIN_TOKENS - tokens)
    if exceeds_threshold:
        advice = "Exceeds the 1,024-token reference threshold for a standalone prefix. Provider/model eligibility and cache hits are not guaranteed."
    else:
        advice = (
            f"Below the 1,024-token standalone reference threshold ({tokens_to_threshold} to boundary). "
            "Additional tools such as the usagetrim MCP desktop profile change prefix size; actual caching depends on the provider/model."
        )

    return LintResult(
        file_path=file_name,
        token_count=tokens,
        line_count=len(lines),
        issues=issues,
        is_cache_friendly=is_cache_friendly,
        exceeds_cache_threshold=exceeds_threshold,
        tokens_to_cache_threshold=tokens_to_threshold,
        cache_advice=advice,
    )


@dataclass
class _RuleBlock:
    kind: str
    lines: list[str]
    line: int
    scope: tuple[int, ...] = ()
    uncertain: bool = False


def _starts_non_setext_block(line: str) -> bool:
    """Recognize block starts that cannot serve as Setext title text."""
    if not line.strip():
        return True
    if re.match(
        r"^(?: {4}|\t| {0,3}(?:`{3,}|~{3,}|#{1,6}(?:[ \t]|$)|>|<!--|(?:[-+*]|\d+[.)])(?:[ \t]|$)))",
        line,
    ):
        return True
    return bool(
        re.fullmatch(
            r" {0,3}(?:\*(?:[ \t]*\*){2,}|-(?:[ \t]*-){2,}|_(?:[ \t]*_){2,})[ \t]*",
            line,
        )
    )


def _starts_markdown_block(lines: list[str], index: int) -> bool:
    line = lines[index]
    if _starts_non_setext_block(line):
        return True
    if re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", line):
        return True
    if re.fullmatch(r"Dynamic Runtime Context\s*:?\s*", line, re.I):
        return True
    # A Setext heading begins at its title, not at the underline.
    return index + 1 < len(lines) and bool(re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", lines[index + 1]))


def _inline_code_end(lines: list[str], start: int) -> tuple[int, bool] | None:
    """Return (end, closed) within one block; never seek a close in a new block."""
    delimiter = None
    for index in range(start, len(lines)):
        if index > start and _starts_markdown_block(lines, index):
            return index, False
        line = lines[index]
        for match in re.finditer(r"`+", line):
            if delimiter is None:
                prefix = line[: match.start()]
                if (len(prefix) - len(prefix.rstrip("\\"))) % 2:
                    continue
                delimiter = len(match[0])
            elif len(match[0]) == delimiter:
                delimiter = None
        if delimiter is None:
            return (index + 1, True) if index > start else None
    return len(lines), False


def _rule_line(line: str) -> str:
    # Markdown hard breaks are semantic, unlike ordinary trailing padding.
    return line if line.endswith("  ") and line.strip() else line.rstrip(" \t")


def _fenced_block_end(lines: list[str], start: int) -> int | None:
    """Return the end of a fenced block, respecting its delimiter and length."""
    opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", lines[start])
    if opening is None:
        return None
    token = opening[1]
    index = start + 1
    while index < len(lines):
        closing = re.fullmatch(
            r" {0,3}" + re.escape(token[0]) + "{" + str(len(token)) + r",}[ \t]*",
            lines[index],
        )
        index += 1
        if closing:
            break
    return index


def _html_comment_start(line: str) -> int | None:
    """Find a comment opener outside inline backtick code spans."""
    delimiter = None
    for match in re.finditer(r"`+|<!--", line):
        token = match[0]
        if token == "<!--":
            if delimiter is None:
                return match.start()
        elif (len(line[: match.start()]) - len(line[: match.start()].rstrip("\\"))) % 2 == 0:
            if delimiter is None:
                delimiter = len(token)
            elif len(token) == delimiter:
                delimiter = None
    return None


def _html_comment_end(lines: list[str], start: int) -> int | None:
    """Consume block comments; stop inline comments at a new Markdown block."""
    opening = _html_comment_start(lines[start])
    if opening is None:
        return None
    block_comment = bool(re.match(r"^ {0,3}<!--", lines[start]))
    index = start + 1
    if "-->" not in lines[start][opening + 4 :]:
        while index < len(lines):
            if not block_comment and _starts_non_setext_block(lines[index]):
                break
            closed = "-->" in lines[index]
            index += 1
            if closed:
                break
    return index


def _setext_heading(lines: list[str], start: int) -> tuple[int, int] | None:
    """Find a Setext underline after consecutive lines of one paragraph."""
    if _starts_non_setext_block(lines[start]):
        return None
    index = start + 1
    while index < len(lines):
        if re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", lines[index]):
            level = 1 if lines[index].lstrip().startswith("=") else 2
            return index + 1, level
        # Indented code cannot interrupt an existing paragraph. Nonblank
        # indented lines therefore continue a title, but cannot start one.
        continuation = lines[index]
        indented = continuation.startswith(("    ", "\t")) and bool(continuation.strip())
        if not indented and _starts_non_setext_block(continuation):
            break
        index += 1
    return None


def _complete_list_items(blocks: list[_RuleBlock]) -> list[_RuleBlock]:
    """Keep lazy prose, nested lists and opaque code attached to their item."""
    result = []
    index = 0
    while index < len(blocks):
        first = blocks[index]
        if first.kind != "list":
            result.append(first)
            index += 1
            continue
        indent = len(first.lines[0]) - len(first.lines[0].lstrip())
        end = index + 1
        while end < len(blocks):
            following = blocks[end]
            line = following.lines[0]
            next_indent = len(line) - len(line.lstrip())
            if next_indent <= indent and (
                following.kind == "heading" or following.scope != first.scope
            ):
                break
            if following.kind == "list" and next_indent <= indent:
                break
            if following.kind == "blank":
                lookahead = end + 1
                while lookahead < len(blocks) and blocks[lookahead].kind == "blank":
                    lookahead += 1
                if lookahead == len(blocks):
                    break
                next_line = blocks[lookahead].lines[0]
                if len(next_line) - len(next_line.lstrip()) <= indent:
                    break
            elif not blocks[end - 1].lines[-1].strip() and next_indent <= indent:
                break
            end += 1
        lines = []
        for part in blocks[index:end]:
            lines.extend(
                part.lines
                if part.kind in {"protected", "dynamic"}
                else [_rule_line(line) for line in part.lines]
            )
        result.append(
            _RuleBlock(
                "list",
                lines,
                first.line,
                first.scope,
                any(part.uncertain for part in blocks[index:end]),
            )
        )
        index = end
    return result


def _scan_rules(content: str) -> list[_RuleBlock]:
    """Track heading ancestry and complete opaque/list boundaries before rewriting."""
    lines = content.replace("\r\n", "\n").split("\n")
    blocks = []
    headings = []
    index = 0
    while index < len(lines):
        start = index
        line = lines[index]
        heading = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+|$)", line)
        heading_level = None
        uncertain = False
        fence_end = _fenced_block_end(lines, index)
        setext = _setext_heading(lines, index)
        html_comment = _html_comment_start(line)
        if index == 0 and line.lstrip("\ufeff") == "---":
            index += 1
            while index < len(lines) and lines[index] not in {"---", "..."}:
                index += 1
            index = min(index + 1, len(lines))
            kind = "protected"
        elif fence_end is not None:
            index = fence_end
            kind = "protected"
        elif line.startswith(("    ", "\t")) and not re.match(r"^\s*(?:[-+*]|\d+[.)])\s", line):
            index += 1
            while index < len(lines) and (
                not lines[index].strip() or lines[index].startswith(("    ", "\t"))
            ):
                index += 1
            kind = "protected"
        elif (
            "`" in line
            and (html_comment is None or line.index("`") < html_comment)
            and (inline_end := _inline_code_end(lines, index)) is not None
        ):
            index, closed = inline_end
            uncertain = not closed
            kind = "protected"
            heading_level = len(heading[1]) if heading else None
        elif setext is not None:
            index, heading_level = setext
            kind = "protected"
            uncertain = any(
                (_html_comment_end(lines, title_index) or 0) > index
                for title_index in range(start, index - 1)
                if _html_comment_start(lines[title_index]) is not None
            )
        elif re.fullmatch(
            r"(?:#{1,6}\s+)?Dynamic Runtime Context\s*:?\s*|<!-- Dynamic Runtime Context \(isolated for prompt caching\) -->",
            line,
            re.I,
        ):
            heading_level = len(heading[1]) if heading else None
            level = heading_level or 6
            index += 1
            while index < len(lines):
                next_heading = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+|$)", lines[index])
                if next_heading and len(next_heading[1]) <= level:
                    break
                next_setext = _setext_heading(lines, index)
                if next_setext is not None and next_setext[1] <= level:
                    break
                # Only headings outside opaque blocks can end this section.
                next_line = lines[index]
                next_html = _html_comment_start(next_line)
                next_inline = (
                    _inline_code_end(lines, index)
                    if "`" in next_line and (next_html is None or next_line.index("`") < next_html)
                    else None
                )
                index = (
                    _fenced_block_end(lines, index)
                    or (next_inline[0] if next_inline is not None else None)
                    or _html_comment_end(lines, index)
                    or (next_setext[0] if next_setext is not None else None)
                    or index + 1
                )
            kind = "dynamic"
        elif html_comment is not None:
            index = _html_comment_end(lines, index)
            # If opaque constructs overlap, retain the union rather than exposing
            # a continuation as ordinary prose.
            for boundary in {start, index - 1}:
                if "`" in lines[boundary]:
                    inline_end = _inline_code_end(lines, boundary)
                    if inline_end is not None:
                        end, closed = inline_end
                        index = max(index, end)
                        uncertain |= not closed
            kind = "protected"
            if heading_level is None:
                heading_level = len(heading[1]) if heading else None
        else:
            index += 1
            if not line.strip():
                kind = "blank"
            elif heading:
                heading_level = len(heading[1])
                kind = "heading"
            elif re.match(r"^\s*(?:[-+*]|\d+[.)])\s", line):
                kind = "list"
            else:
                kind = "prose"
        if heading_level is not None:
            while headings and headings[-1][0] >= heading_level:
                headings.pop()
        scope = tuple(position for _, position in headings)
        if heading_level is not None:
            headings.append((heading_level, start + 1))
        blocks.append(_RuleBlock(kind, lines[start:index], start + 1, scope, uncertain))
    return _complete_list_items(blocks)


def _safe_dynamic(block: _RuleBlock) -> bool:
    found = False
    for line in block.lines[1:]:
        if not line.strip():
            continue
        if re.search(
            r"\b(?:do not|don't|never|must|should|may|if|unless|except|only when)\b", line, re.I
        ):
            return False
        match = re.fullmatch(r"\s*(?:> ?)?([A-Za-z_ ]+)\s*[:=]\s*(.+?)\s*", line)
        if not match:
            return False
        key = match[1].strip().lower().replace("_", " ")
        value = match[2]
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                return False
            value = value[1:-1]
        if key in {"current date", "current time", "timestamp", "generated date"}:
            valid = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+Z-]+)?", value))
        elif key in {"session id", "request id", "build id", "version"}:
            valid = bool(re.fullmatch(r"[A-Za-z0-9_.-]+", value))
        elif key in {"workspace", "project root", "machine path"}:
            valid = bool(re.fullmatch(r"(?:/|[A-Za-z]:\\)[A-Za-z0-9_./\\:-]+", value))
        else:
            return False
        if not valid:
            return False
        found = True
    return found


_DYNAMIC_MARKER = "# Dynamic Runtime Context"
_STANDALONE_METADATA = re.compile(
    r"(?:Current Date|Current Time|Timestamp|Generated Date|Session(?: ID|_id)|Request(?: ID|_id))\s*[:=]\s*.+",
    re.I,
)


def _standalone_metadata(block: _RuleBlock) -> bool:
    line = block.lines[0]
    return (
        block.kind == "prose"
        and not block.scope
        and line == line.lstrip()
        and not line.endswith("  ")
        and bool(_STANDALONE_METADATA.fullmatch(line))
        and _safe_dynamic(_RuleBlock("dynamic", [_DYNAMIC_MARKER, line], block.line))
    )


# Full-line greetings/personas only, with a closed vocabulary and no action clause.
_PERSONA = re.compile(
    r"You are (?:a|an) (?:(?:helpful|expert|world-class|senior) )?(?:(?:coding|software|AI) )?assistant(?: expert)?\.",
    re.I,
)
_FILLER = {
    "Hello Claude!",
    "Hello Codex!",
    "Welcome to the project.",
    "Please make sure to always remember to never forget this.",
    "As an AI language model, you are a helpful assistant.",
}


def _boilerplate(line: str) -> bool:
    sentences = re.split(r"(?<=[.!])\s+", line.strip())
    return bool(sentences) and all(
        part in _FILLER or _PERSONA.fullmatch(part) for part in sentences
    )


def _canonical_rules(content: str, *, optimize: bool) -> str:
    blocks = _scan_rules(content)
    if any(block.uncertain for block in blocks):
        # Literal/unclosed backticks are ambiguous: preserve the document rather
        # than exposing possibly protected instructions to any transformation.
        return content.replace("\r\n", "\n").rstrip("\n") + "\n"
    output = []
    dynamic = []
    previous_rule = None
    for block in blocks:
        if optimize and _standalone_metadata(block):
            dynamic.append(block.lines[0].rstrip(" \t"))
            previous_rule = None
            continue
        if block.kind == "dynamic" and not block.scope and _safe_dynamic(block) and optimize:
            dynamic.extend(line for line in block.lines[1:] if line.strip())
            previous_rule = None
            continue
        if block.kind in {"protected", "dynamic"} or (
            block.kind == "list" and len(block.lines) > 1
        ):
            output.extend(block.lines)
            previous_rule = None
            continue
        line = block.lines[0]
        # Two spaces are Markdown's semantic hard break; otherwise trim padding.
        hard_break = line.endswith("  ") and bool(line.strip())
        line = _rule_line(line)
        if optimize and block.kind == "prose" and not hard_break and _boilerplate(line):
            continue
        if block.kind == "blank":
            previous_rule = None
            if output and output[-1] != "":
                output.append("")
            continue
        # Boundaries already include all continuation/child content.
        indent = len(line) - len(line.lstrip(" \t"))
        simple = (
            block.kind == "list"
            and indent == 0
            and bool(re.match(r"^\s*[-+*]\s", line))
            and not hard_break
            and not re.search(r"[`<>]|https?://", line)
        )
        rule_text = line[indent:]
        if re.fullmatch(r"[-+*] [A-Za-z ,.!?;:-]+", rule_text):
            rule_text = re.sub(r"[ \t]+", " ", rule_text)
        key = (block.scope, line[:indent], rule_text) if simple else None
        if optimize and key is not None and key == previous_rule:
            continue
        previous_rule = key
        output.append(line)
    # Strip only empty outer lines, never indentation or protected line content.
    while output and output[0] == "":
        output.pop(0)
    while output and output[-1] == "":
        output.pop()
    if dynamic:
        if output:
            output.append("")
        output.append("\n".join([_DYNAMIC_MARKER, *dynamic]))
    return "\n".join(output).rstrip("\n") + "\n"


def minify_rules(content: str) -> str:
    """Normalize safe whitespace without changing instructions or opaque blocks."""
    return _canonical_rules(content, optimize=False)


CLIENT_RULE_PATHS = {
    "claude": "CLAUDE.md",
    "codex": "AGENTS.md",
    "cursor": ".cursorrules",
    "copilot": ".github/copilot-instructions.md",
}


def _rules_client(client: str) -> str:
    if not isinstance(client, str):
        raise ValueError("client must be claude, codex, cursor or copilot")
    client = client.lower()
    if client == "codex-desktop":
        client = "codex"
    if client not in CLIENT_RULE_PATHS:
        raise ValueError("client must be claude, codex, cursor or copilot")
    return client


def generate_desktop_rules(client: str = "claude") -> str:
    client = _rules_client(client)
    intro = {
        "cursor": "- Scope: project instructions for Cursor Agent.\n",
        "copilot": "- Scope: repository instructions for GitHub Copilot.\n",
    }.get(client, "")
    return (
        f"# UsageTrim { {'claude': 'Claude', 'codex': 'Codex', 'cursor': 'Cursor', 'copilot': 'Copilot'}[client] } Rules\n"
        + intro
        + "- Targeted inspection: use `usagetrim_code` (outline, symbols, callers) and `usagetrim_read` (symbols/ranges) instead of dumping full files.\n"
        "- Precise changes: use `usagetrim_edit_symbol` for hash-guarded atomic updates.\n"
        "- Shell execution: use `usagetrim_exec` or run `usagetrim run -- <cmd>` in terminal to fold build/test noise.\n"
        "- Recovery: if folded diagnostics or logs are needed, call `usagetrim_retrieve(ref_id=...)`.\n"
        "- Task memory: save milestone checkpoints with `usagetrim_context`.\n"
    )


def optimize_rules(content: str) -> dict[str, Any]:
    """Conservative offline optimization; uncertain instructions remain unchanged."""
    from usagetrim.metrics.pricing import estimate_savings

    optimized = _canonical_rules(content, optimize=True)
    before, after = count_tokens(content), count_tokens(optimized)
    delta = before.openai - after.openai
    costs = estimate_savings(before.claude - after.claude, delta, before.gemini - after.gemini)
    lint = lint_rule_content(optimized)
    return {
        "original_tokens": before.openai,
        "optimized_tokens": after.openai,
        "saved_tokens": max(0, delta),
        "token_delta": delta,
        "savings_pct": round(100 * delta / before.openai, 1) if before.openai else 0.0,
        "optimized_content": optimized,
        "estimated_baseline_input_cost_delta": costs.avg_saved_usd,
        "is_cache_friendly": lint.is_cache_friendly,
        "exceeds_cache_threshold": lint.exceeds_cache_threshold,
        "tokens_to_cache_threshold": lint.tokens_to_cache_threshold,
        "cache_advice": lint.cache_advice,
    }
