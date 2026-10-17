"""Prompt cache aligner and cacheability optimizer for Anthropic, OpenAI, and Gemini prompt caching."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

from usagetrim.metrics.tokenizer import count_tokens

_TIMESTAMP_PATTERN = re.compile(
    r"\b(?:today(?:'s)? date|current time|timestamp|date is|time is|utc|iso8601)\b[^\n]*|"
    r"\b\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}(?::\d{2})?[^\n]*|"
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}[^\n]*",
    re.IGNORECASE,
)
_UUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_DYNAMIC_SESSION_PATTERN = re.compile(
    r"\b(?:session(?:_id)?|request(?:_id)?|turn(?:_id)?|conversation(?:_id)?)\s*[:=]\s*\S+",
    re.IGNORECASE,
)


@dataclass
class PromptAlignResult:
    original_tokens: int
    aligned_tokens: int
    cacheability_score: int
    issues_found: list[str]
    aligned_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_tokens": self.original_tokens,
            "aligned_tokens": self.aligned_tokens,
            "cacheability_score": self.cacheability_score,
            "issues_found": self.issues_found,
            "aligned_text": self.aligned_text,
        }


def lint_prompt(text: str) -> dict[str, Any]:
    """Audit prompt for cache-busting dynamic elements in prefix."""
    lines = text.splitlines()
    if not lines:
        return {"cacheability_score": 100, "issues": [], "recommendations": []}

    prefix_cutoff = min(len(lines), max(5, len(lines) // 3))
    prefix_lines = lines[:prefix_cutoff]

    issues: list[str] = []
    for idx, line in enumerate(prefix_lines, 1):
        if _TIMESTAMP_PATTERN.search(line):
            issues.append(
                f"Line {idx}: Dynamic timestamp/date in prompt prefix busts prompt cache: '{line.strip()[:60]}'"
            )
        if _UUID_PATTERN.search(line):
            issues.append(
                f"Line {idx}: Volatile UUID in prompt prefix busts prompt cache: '{line.strip()[:60]}'"
            )
        if _DYNAMIC_SESSION_PATTERN.search(line):
            issues.append(
                f"Line {idx}: Session or request ID in prompt prefix busts prompt cache: '{line.strip()[:60]}'"
            )

    score = max(0, 100 - (len(issues) * 30))
    recommendations = []
    if issues:
        recommendations.append(
            "Move dynamic variables (timestamps, IDs) to the prompt suffix to preserve static cacheable prefix."
        )
        recommendations.append(
            "Use 'usagetrim prompt align' to automatically restructure prompt sections for maximum cache hits."
        )

    return {
        "cacheability_score": score,
        "issues": issues,
        "recommendations": recommendations,
        "prefix_lines_checked": prefix_cutoff,
    }


def align_prompt(text: str) -> PromptAlignResult:
    """Restructure prompt: static invariants at the top, volatile variables at the bottom."""
    lines = text.splitlines(keepends=True)
    orig_tokens = count_tokens(text).openai

    lint_res = lint_prompt(text)
    issues = lint_res["issues"]

    if not issues:
        return PromptAlignResult(
            original_tokens=orig_tokens,
            aligned_tokens=orig_tokens,
            cacheability_score=lint_res["cacheability_score"],
            issues_found=[],
            aligned_text=text,
        )

    static_lines: list[str] = []
    volatile_lines: list[str] = []

    for line in lines:
        if (
            _TIMESTAMP_PATTERN.search(line)
            or _UUID_PATTERN.search(line)
            or _DYNAMIC_SESSION_PATTERN.search(line)
        ):
            volatile_lines.append(line)
        else:
            static_lines.append(line)

    # Reconstruct: static prefix followed by separated dynamic context block
    sections: list[str] = ["".join(static_lines).rstrip()]
    if volatile_lines:
        sections.append(
            "\n\n<!-- DYNAMIC RUNTIME CONTEXT (ISOLATED TO PRESERVE PROMPT CACHE) -->\n"
            + "".join(volatile_lines).strip()
        )

    aligned_text = "\n".join(sections).strip() + "\n"
    aligned_tokens = count_tokens(aligned_text).openai

    # Post-check score
    new_lint = lint_prompt(aligned_text)

    return PromptAlignResult(
        original_tokens=orig_tokens,
        aligned_tokens=aligned_tokens,
        cacheability_score=new_lint["cacheability_score"],
        issues_found=issues,
        aligned_text=aligned_text,
    )


_FILLER_PHRASES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"^\s*You are a(?:n)?\s+(?:helpful|expert|world-class|autonomous)?\s*(?:AI|coding|software)?\s*assistant\s*(?:specialized\s+in\s+[^.\n]+)?\.\s*",
            re.IGNORECASE | re.MULTILINE,
        ),
        "",
    ),
    (
        re.compile(
            r"\b(?:Please\s+)?make\s+sure\s+to\s+always\s+(?:remember\s+to\s+)?",
            re.IGNORECASE,
        ),
        "Always ",
    ),
    (
        re.compile(
            r"\b(?:Please\s+)?ensure\s+(?:that\s+)?you\s+(?:always\s+)?",
            re.IGNORECASE,
        ),
        "Ensure ",
    ),
    (
        re.compile(
            r"\bIt\s+is\s+(?:critically\s+|extremely\s+|very\s+)?important\s+(?:that\s+you\s+)?",
            re.IGNORECASE,
        ),
        "Important: ",
    ),
    (
        re.compile(
            r"\bUnder\s+no\s+circumstances\s+should\s+you\s+(?:ever\s+)?",
            re.IGNORECASE,
        ),
        "Never ",
    ),
    (
        re.compile(r"\bDo\s+not\s+ever\s+", re.IGNORECASE),
        "Never ",
    ),
]


@dataclass
class PromptMinifyResult:
    original_tokens: int
    minified_tokens: int
    saved_tokens: int
    reduction_pct: float
    minified_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_tokens": self.original_tokens,
            "minified_tokens": self.minified_tokens,
            "saved_tokens": self.saved_tokens,
            "reduction_pct": self.reduction_pct,
            "minified_text": self.minified_text,
        }


def minify_prompt(text: str) -> PromptMinifyResult:
    """Minify system prompts, CLAUDE.md, and AGENTS.md instructions without semantic loss.

    Removes markdown comments, collapses hyper-verbose phrasing, strips excessive blank lines,
    and normalizes list indentation to save 20-40% prompt tokens.
    """
    orig_tokens = count_tokens(text).openai
    if not text.strip():
        return PromptMinifyResult(0, 0, 0, 0.0, text)

    # 1. Strip HTML comments (<!-- ... -->)
    content = re.sub(r"<!--[\s\S]*?-->", "", text)

    # 2. Compress verbose boilerplate phrasings
    for pattern, replacement in _FILLER_PHRASES:
        content = pattern.sub(replacement, content)

    # 3. Clean line by line
    lines = content.splitlines()
    cleaned_lines: list[str] = []
    prev_blank = False
    prev_line: str | None = None

    for line in lines:
        stripped = line.rstrip()
        if not stripped:
            if not prev_blank and cleaned_lines:
                cleaned_lines.append("")
                prev_blank = True
            continue

        prev_blank = False
        leading_spaces = len(stripped) - len(stripped.lstrip())
        if leading_spaces > 4 and stripped.lstrip().startswith(("-", "*", "•", "1.")):
            stripped = "  " + stripped.lstrip()

        if stripped == prev_line:
            continue

        prev_line = stripped
        cleaned_lines.append(stripped)

    minified_text = "\n".join(cleaned_lines).strip() + "\n"
    minified_tokens = count_tokens(minified_text).openai
    saved = max(0, orig_tokens - minified_tokens)
    pct = round((saved / orig_tokens * 100.0), 1) if orig_tokens > 0 else 0.0

    return PromptMinifyResult(
        original_tokens=orig_tokens,
        minified_tokens=minified_tokens,
        saved_tokens=saved,
        reduction_pct=pct,
        minified_text=minified_text,
    )


_VOLATILE_PATH_PATTERN = re.compile(
    r"(?:/private/var/folders/[^\s:]+|/(?:tmp|var/tmp)/[^\s:]+)",
    re.IGNORECASE,
)


def stabilize_cache_prefix(text: str) -> str:
    """Normalize volatile elements in text prefixes to protect prompt cache hits.

    1. Normalizes CRLF \\r\\n to \\n and strips trailing whitespace.
    2. Isolates volatile prefix variables (timestamps, volatile paths, session UUIDs)
       to dynamic context sections so that Anthropic (1024-token) and OpenAI (128-token)
       prompt caching achieves optimal hit rates.
    """
    if not text.strip():
        return text

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)

    aligned = align_prompt(normalized).aligned_text
    stabilized = _VOLATILE_PATH_PATTERN.sub("<tmpdir>", aligned)

    return stabilized


class PromptCacheResult(tuple):
    """Result tuple supporting (messages, tools) unpacking while providing metadata attributes."""

    def __new__(
        cls,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        breakpoints_placed: int = 0,
        total_tokens: int = 0,
        system: str | list[dict[str, Any]] | None = None,
    ):
        return super().__new__(cls, (messages, tools))

    def __init__(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        breakpoints_placed: int = 0,
        total_tokens: int = 0,
        system: str | list[dict[str, Any]] | None = None,
    ):
        self.messages = messages
        self.tools = tools
        self.breakpoints_placed = breakpoints_placed
        self.total_tokens = total_tokens
        self.system = system

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": self.messages,
            "tools": self.tools,
            "breakpoints_placed": self.breakpoints_placed,
            "total_tokens": self.total_tokens,
            "system": self.system,
        }


def _message_token_count(msg: dict[str, Any]) -> int:
    content = msg.get("content")
    if isinstance(content, str):
        return count_tokens(content).claude
    elif isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content") or ""
                if not text and "input" in block:
                    text = json.dumps(block["input"], sort_keys=True)
                total += count_tokens(str(text)).claude
            else:
                total += count_tokens(str(block)).claude
        return max(1, total)
    elif content is None:
        return 0
    else:
        return count_tokens(str(content)).claude


def _tool_token_count(tool: dict[str, Any]) -> int:
    return count_tokens(json.dumps(tool, sort_keys=True)).claude


def _add_cache_control_to_tool(tool: dict[str, Any]) -> None:
    tool["cache_control"] = {"type": "ephemeral"}


def _add_cache_control_to_message(msg: dict[str, Any]) -> None:
    content = msg.get("content")
    if isinstance(content, str):
        msg["content"] = [
            {
                "type": "text",
                "text": content,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    elif isinstance(content, list) and content:
        for b in content:
            if isinstance(b, dict) and "cache_control" in b:
                return
        last_block = content[-1]
        if isinstance(last_block, dict):
            last_block["cache_control"] = {"type": "ephemeral"}
        else:
            content[-1] = {
                "type": "text",
                "text": str(last_block),
                "cache_control": {"type": "ephemeral"},
            }
    elif isinstance(content, list) and not content:
        msg["content"] = [
            {
                "type": "text",
                "text": "",
                "cache_control": {"type": "ephemeral"},
            }
        ]


def inject_prompt_cache_breakpoints(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    system: str | list[dict[str, Any]] | None = None,
    min_tokens: int = 1024,
    max_breakpoints: int = 4,
) -> PromptCacheResult:
    """Auto-inject Anthropic Prompt Caching breakpoints at optimal prefix boundaries.

    Positions up to `max_breakpoints` (default: 4) `cache_control: {"type": "ephemeral"}`
    breakpoints at stable prefix milestones meeting Anthropic's `min_tokens` (default: 1024)
    threshold.

    1. Stabilizes tool definitions alphabetically by name to prevent cache busts.
    2. Places a breakpoint on the last tool definition if tools + system >= min_tokens.
    3. Positions remaining breakpoints across conversational message history turns,
       ensuring the most recent stable context is cached for subsequent turns.
    """
    messages_copy = copy.deepcopy(messages)
    tools_copy = copy.deepcopy(tools) if tools is not None else []
    system_copy = copy.deepcopy(system) if system is not None else None

    # 1. Stabilize tool definitions alphabetically
    if tools_copy:
        tools_copy = sorted(tools_copy, key=lambda t: str(t.get("name", "")))
        for t in tools_copy:
            t.pop("cache_control", None)

    # 2. Compute prefix token counts
    system_tokens = 0
    if isinstance(system_copy, str):
        system_tokens = count_tokens(system_copy).claude
    elif isinstance(system_copy, list):
        for block in system_copy:
            if isinstance(block, dict):
                system_tokens += count_tokens(block.get("text", "")).claude

    tools_tokens = sum(_tool_token_count(t) for t in tools_copy)
    prefix_tokens = system_tokens + tools_tokens

    breakpoints_placed = 0
    remaining_breakpoints = max_breakpoints

    # 3. Checkpoint on System / Tools
    if tools_copy and prefix_tokens >= min_tokens and remaining_breakpoints > 0:
        _add_cache_control_to_tool(tools_copy[-1])
        breakpoints_placed += 1
        remaining_breakpoints -= 1

    # 4. Checkpoints in Messages
    cumulative_tokens = prefix_tokens
    cum_token_counts: list[int] = []

    for msg in messages_copy:
        t_count = _message_token_count(msg)
        cumulative_tokens += t_count
        cum_token_counts.append(cumulative_tokens)

    eligible_indices = [idx for idx, c_tok in enumerate(cum_token_counts) if c_tok >= min_tokens]

    if eligible_indices and remaining_breakpoints > 0:
        if len(eligible_indices) <= remaining_breakpoints:
            selected_indices = eligible_indices
        else:
            last_idx = eligible_indices[-1]
            earlier = eligible_indices[:-1]
            num_earlier = remaining_breakpoints - 1

            if num_earlier > 0 and earlier:
                step = len(earlier) / num_earlier
                selected_earlier = [earlier[int(k * step)] for k in range(num_earlier)]
                selected_indices = sorted(set(selected_earlier + [last_idx]))
            else:
                selected_indices = [last_idx]

        for idx in selected_indices:
            _add_cache_control_to_message(messages_copy[idx])
            breakpoints_placed += 1

    return PromptCacheResult(
        messages=messages_copy,
        tools=tools_copy,
        breakpoints_placed=breakpoints_placed,
        total_tokens=cumulative_tokens,
        system=system_copy,
    )
