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


def _scan_rules(content: str) -> list[_RuleBlock]:
    """Conservative block scanner; uncertain Markdown remains opaque."""
    lines = content.replace("\r\n", "\n").split("\n")
    blocks = []
    index = 0
    while index < len(lines):
        start = index
        line = lines[index]
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if index == 0 and line.lstrip("\ufeff") == "---":
            index += 1
            while index < len(lines) and lines[index] not in {"---", "..."}:
                index += 1
            index = min(index + 1, len(lines))
            kind = "protected"
        elif fence:
            token = fence[1]
            index += 1
            while index < len(lines):
                closing = re.fullmatch(
                    r" {0,3}" + re.escape(token[0]) + "{" + str(len(token)) + r",}[ \t]*",
                    lines[index],
                )
                index += 1
                if closing:
                    break
            kind = "protected"
        elif re.fullmatch(
            r"(?:#{1,6}\s+)?Dynamic Runtime Context\s*:?\s*|<!-- Dynamic Runtime Context \(isolated for prompt caching\) -->",
            line,
            re.I,
        ):
            heading = re.match(r"^(#+) ", line)
            level = len(heading[1]) if heading else 6
            index += 1
            while index < len(lines):
                next_heading = re.match(r"^(#{1,6})\s+", lines[index])
                if next_heading and len(next_heading[1]) <= level:
                    break
                index += 1
            kind = "dynamic"
        elif (
            index + 1 < len(lines)
            and line.strip()
            and re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", lines[index + 1])
        ):
            index += 2
            kind = "protected"  # Setext heading: retain its complete two-line form.
        elif "<!--" in line:
            index += 1
            if "-->" not in line.split("<!--", 1)[1]:
                while index < len(lines):
                    end = "-->" in lines[index]
                    index += 1
                    if end:
                        break
            kind = "protected"
        else:
            index += 1
            if not line.strip():
                kind = "blank"
            elif re.match(r"^#{1,6}\s", line):
                kind = "heading"
            elif re.match(r"^\s*(?:[-+*]|\d+[.)])\s", line):
                kind = "list"
            elif line.startswith(("    ", "\t")):
                kind = "protected"
                while index < len(lines) and (
                    not lines[index].strip() or lines[index].startswith(("    ", "\t"))
                ):
                    index += 1
            else:
                kind = "prose"
        blocks.append(_RuleBlock(kind, lines[start:index], start + 1))
    return blocks


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


_DYNAMIC_MARKER = "<!-- Dynamic Runtime Context (isolated for prompt caching) -->"
_STANDALONE_METADATA = re.compile(
    r"(?:Current Date|Current Time|Timestamp|Generated Date|Session(?: ID|_id)|Request(?: ID|_id))\s*[:=]\s*.+",
    re.I,
)


def _standalone_metadata(block: _RuleBlock) -> bool:
    line = block.lines[0]
    return (
        block.kind == "prose"
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
    output = []
    dynamic = []
    dynamic_marker = None
    in_list = False
    previous_rule = None
    section = 0
    next_content = [None] * len(blocks)
    following = None
    for index in range(len(blocks) - 1, -1, -1):
        next_content[index] = following
        if blocks[index].kind != "blank":
            following = blocks[index]
    for index, block in enumerate(blocks):
        if block.kind in {"blank", "heading"}:
            in_list = False
        elif block.kind == "list":
            in_list = True
        if optimize and not in_list and _standalone_metadata(block):
            dynamic.append(block.lines[0].rstrip(" \t"))
            previous_rule = None
            continue
        if block.kind == "dynamic" and _safe_dynamic(block) and optimize:
            dynamic_marker = dynamic_marker or block.lines[0]
            dynamic.extend(line for line in block.lines[1:] if line.strip())
            previous_rule = None
            continue
        if block.kind in {"protected", "dynamic"}:
            output.extend(block.lines)
            previous_rule = None
            continue
        line = block.lines[0]
        # Two spaces are Markdown's semantic hard break; otherwise trim padding.
        hard_break = line.endswith("  ") and bool(line.strip())
        if not hard_break:
            line = line.rstrip(" \t")
        if block.kind == "heading":
            section += 1
        if optimize and block.kind == "prose" and not hard_break and _boilerplate(line):
            continue
        if block.kind == "blank":
            previous_rule = None
            if output and output[-1] != "":
                output.append("")
            continue
        # Only adjacent, childless simple bullets share an unambiguous scope.
        nested = False
        indent = len(line) - len(line.lstrip(" \t"))
        following = next_content[index]
        if following is not None:
            next_line = following.lines[0]
            nested = (
                bool(next_line.strip()) and len(next_line) - len(next_line.lstrip(" \t")) > indent
            )
        simple = (
            block.kind == "list"
            and indent == 0
            and bool(re.match(r"^\s*[-+*]\s", line))
            and not nested
            and not hard_break
            and not re.search(r"[`<>]|https?://", line)
        )
        rule_text = line[indent:]
        if re.fullmatch(r"[-+*] [A-Za-z ,.!?;:-]+", rule_text):
            rule_text = re.sub(r"[ \t]+", " ", rule_text)
        key = (section, line[:indent], rule_text) if simple else None
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
        output.append("\n".join([dynamic_marker or _DYNAMIC_MARKER, *dynamic]))
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
