"""In-chat conversation and transcript distillation for AI coding assistants."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

from usagetrim.core.cache import ContextCache
from usagetrim.core.redactor import redact_secrets
from usagetrim.metrics.tokenizer import count_tokens

_COLOR = re.compile(r"\x1b\[[0-9;]*m")
_THOUGHT_BLOCK = re.compile(
    r"<(?:thinking|thought)>.*?</(?:thinking|thought)>", re.DOTALL | re.IGNORECASE
)
_FILE_PATH = re.compile(r"(?:[\w\-.]+/)+[\w\-.]+\.[a-zA-Z0-9]+")
_DECISION_PATTERNS = re.compile(
    r"\b(?:decided to|agree(?:d)? on|choose|chose|switch(?:ed)? to|implement(?:ed)?|"
    r"will use|must use|standardize on|configured)\b[^\n.!?]+[.!?]",
    re.IGNORECASE,
)
_FAILED_PATTERNS = re.compile(
    r"\b(?:failed|error|rejected|reverted|cannot|could not|broken|bug|exception)\b[^\n.!?]+[.!?]",
    re.IGNORECASE,
)


@dataclass
class DistillResult:
    original_tokens: int
    distilled_tokens: int
    saved_tokens: int
    reduction_pct: float
    ref_id: str
    text: str
    message_count: int
    files_referenced: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_tokens": self.original_tokens,
            "distilled_tokens": self.distilled_tokens,
            "saved_tokens": self.saved_tokens,
            "reduction_pct": self.reduction_pct,
            "ref_id": self.ref_id,
            "text": self.text,
            "message_count": self.message_count,
            "files_referenced": self.files_referenced,
        }


def _parse_messages(text: str) -> list[tuple[str, str]]:
    """Parse dialogue turns from markdown, XML, or JSON lines."""
    messages: list[tuple[str, str]] = []

    # Check if text is JSON-lines
    stripped = text.strip()
    if stripped.startswith("{") and "\n" in stripped:
        parsed_json_lines = True
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                role = obj.get("role", obj.get("source", "unknown"))
                content = obj.get("content", obj.get("text", ""))
                if isinstance(content, str) and content.strip():
                    messages.append((str(role).capitalize(), content.strip()))
            except Exception:
                parsed_json_lines = False
                break
        if parsed_json_lines and messages:
            return messages

    # Parse role-based markdown dialogue
    role_pattern = re.compile(
        r"^(?:#{1,4}\s*)?(User|Assistant|Human|System|Claude|GPT|Antigravity|Agent|You|Codex|ChatGPT|Gemini):\s*",
        re.MULTILINE | re.IGNORECASE,
    )
    splits = list(role_pattern.finditer(text))
    if splits:
        for idx, match in enumerate(splits):
            raw_role = match.group(1).capitalize()
            role = "User" if raw_role.lower() == "you" else raw_role
            start = match.end()
            end = splits[idx + 1].start() if idx + 1 < len(splits) else len(text)
            chunk = text[start:end].strip()
            if chunk:
                messages.append((role, chunk))
        return messages

    # Fallback: treat paragraphs or whole text as single message
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return [("User", p) for p in paragraphs] or [("User", text)]


def distill_conversation(text: str, budget: int = 1500) -> DistillResult:
    """Distill raw multi-turn conversation into dense executive context with zero loss CCR recovery."""
    cleaned = _COLOR.sub("", text)
    cleaned = _THOUGHT_BLOCK.sub("", cleaned)
    redacted = redact_secrets(cleaned)
    original_tokens = count_tokens(redacted).openai

    cache = ContextCache()
    ref_id = cache.store(redacted, source="conversation-distill")

    messages = _parse_messages(redacted)
    message_count = len(messages)

    # 1. Identify User Goal & Initial Prompts
    user_requests: list[str] = []
    assistant_responses: list[str] = []
    files_seen: set[str] = set()

    for role, content in messages:
        # Extract files
        for match in _FILE_PATH.finditer(content):
            path = match.group()
            if not path.startswith("http") and not path.endswith((".com", ".org", ".net", ".io")):
                files_seen.add(path)

        if role in {"User", "Human"}:
            first_sentence = content.splitlines()[0][:200].strip()
            if first_sentence and first_sentence not in user_requests:
                user_requests.append(first_sentence)
        else:
            assistant_responses.append(content)

    # 2. Extract Key Decisions
    decisions: list[str] = []
    for resp in assistant_responses:
        for match in _DECISION_PATTERNS.finditer(resp):
            item = match.group().strip()
            if item not in decisions and len(item) > 15:
                decisions.append(item)
            if len(decisions) >= 8:
                break

    # 3. Extract Discarded Attempts & Failures
    failures: list[str] = []
    for resp in assistant_responses:
        for match in _FAILED_PATTERNS.finditer(resp):
            item = match.group().strip()
            if item not in failures and len(item) > 15:
                failures.append(item)
            if len(failures) >= 5:
                break

    # 4. Extract Last Assistant Action / State
    last_action = ""
    if assistant_responses:
        last_resp_lines = [
            line.strip() for line in assistant_responses[-1].splitlines() if line.strip()
        ]
        last_action = "\n".join(last_resp_lines[-4:]) if last_resp_lines else ""

    # Build dense executive context block
    sections: list[str] = [
        "# Distilled Conversation Context",
        f"Messages: {message_count} turns · Full Original Cached: usagetrim retrieve {ref_id}",
        "",
        "## Goals & User Inquiries",
    ]
    for req in user_requests[:6]:
        sections.append(f"- {req}")
    if not user_requests:
        sections.append("- (Continued task session)")

    if decisions:
        sections.append("\n## Key Decisions & Agreed Architecture")
        for dec in decisions:
            sections.append(f"- {dec}")

    if failures:
        sections.append("\n## Resolved Issues & Discarded Attempts")
        for fail in failures:
            sections.append(f"- {fail}")

    if files_seen:
        sections.append("\n## Referenced Files")
        sorted_files = sorted(list(files_seen))[:20]
        sections.append(", ".join(f"`{f}`" for f in sorted_files))

    if last_action:
        sections.append(f"\n## Active Working State\n{last_action}")

    distilled_text = "\n".join(sections).strip() + f"\n\n[Full transcript reference: {ref_id}]\n"
    distilled_tokens = count_tokens(distilled_text).openai

    # Enforce budget ceiling
    if distilled_tokens > budget:
        notice = f"\n\n[Summary truncated to fit {budget} tokens. Full transcript: usagetrim retrieve {ref_id}]\n"
        # A single long assistant line can exceed the entire budget. Bound the
        # complete emitted text, including recovery, without splitting Unicode.
        low, high = 0, len(distilled_text)
        while low < high:
            middle = (low + high + 1) // 2
            if count_tokens(distilled_text[:middle] + notice).openai <= budget:
                low = middle
            else:
                high = middle - 1
        distilled_text = distilled_text[:low] + notice
        distilled_tokens = count_tokens(distilled_text).openai

    saved = max(0, original_tokens - distilled_tokens)
    pct = round((saved / original_tokens) * 100, 1) if original_tokens else 0.0

    return DistillResult(
        original_tokens=original_tokens,
        distilled_tokens=distilled_tokens,
        saved_tokens=saved,
        reduction_pct=pct,
        ref_id=ref_id,
        text=distilled_text,
        message_count=message_count,
        files_referenced=sorted(list(files_seen)),
    )


def count_message_tokens(msg: dict[str, Any]) -> int:
    """Accurately estimate tokens for a single chat message dict."""
    tokens = 4  # Chat format framing overhead per message
    role = msg.get("role", "")
    if role:
        tokens += 1
    content = msg.get("content")
    if isinstance(content, str):
        tokens += count_tokens(content).openai
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                text = part.get("text") or part.get("content") or ""
                if isinstance(text, str) and text:
                    tokens += count_tokens(text).openai
                elif isinstance(text, list):
                    for sub in text:
                        if isinstance(sub, dict) and "text" in sub:
                            tokens += count_tokens(sub["text"]).openai
            elif isinstance(part, str):
                tokens += count_tokens(part).openai
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if isinstance(tc, dict):
                fn = tc.get("function", {})
                name = fn.get("name") or tc.get("name") or ""
                args = fn.get("arguments") or tc.get("input") or ""
                if isinstance(args, dict):
                    args = json.dumps(args)
                tokens += count_tokens(f"{name}:{args}").openai
    return tokens


def count_history_tokens(messages: list[dict[str, Any]]) -> int:
    """Count total tokens across a list of chat message dicts."""
    return sum(count_message_tokens(m) for m in messages) + 3


def _extract_tool_calls_map(messages: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """Build a mapping from tool_call_id to (tool_name, arguments_str)."""
    mapping: dict[str, tuple[str, str]] = {}
    for m in messages:
        tcs = m.get("tool_calls")
        if isinstance(tcs, list):
            for tc in tcs:
                if isinstance(tc, dict):
                    cid = tc.get("id")
                    fn = tc.get("function", {})
                    name = fn.get("name") or tc.get("name") or "tool"
                    args = fn.get("arguments") or tc.get("input") or ""
                    if isinstance(args, dict):
                        args = json.dumps(args)
                    if cid:
                        mapping[str(cid)] = (name, str(args))
        content = m.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    cid = block.get("id")
                    name = block.get("name") or "tool"
                    inp = block.get("input") or {}
                    if isinstance(inp, dict):
                        inp = json.dumps(inp)
                    if cid:
                        mapping[str(cid)] = (name, str(inp))
    return mapping


def _create_tool_receipt(
    tool_name: str,
    args_str: str,
    raw_output: str,
    cache: ContextCache,
) -> str:
    """Store raw output in CCR cache and return a high-signal 1-line receipt."""
    ref_id = cache.store(raw_output, source="tool-observation")
    lines = raw_output.splitlines()
    line_count = len(lines)

    target = ""
    if args_str:
        try:
            d = json.loads(args_str) if isinstance(args_str, str) else args_str
            if isinstance(d, dict):
                for k in [
                    "path",
                    "file",
                    "filename",
                    "command",
                    "cmd",
                    "query",
                    "pattern",
                    "symbol",
                ]:
                    if k in d:
                        val = str(d[k])
                        target = f'("{val}")' if len(val) <= 45 else f'("{val[:42]}...")'
                        break
        except Exception:
            pass
    if not target and args_str and len(str(args_str)) <= 35:
        target = f"({args_str})"

    # Detect summary signal from output lines
    summary = f"returned {line_count} lines"
    for line_item in lines[:10] + lines[-10:]:
        clean = line_item.strip()
        clow = clean.lower()
        if any(
            w in clow for w in ["passed", "failed", "error", "ok", "warning", "tests", "success"]
        ):
            if 8 <= len(clean) <= 60:
                summary = clean
                break

    return f"[Tool {tool_name}{target} -> {summary} (omitted by usagetrim; ref: {ref_id})]"


def _compact_intermediate_message(
    msg: dict[str, Any],
    tool_calls_map: dict[str, tuple[str, str]],
    cache: ContextCache,
    min_tool_tokens: int = 80,
) -> dict[str, Any]:
    """Compact verbose tool observations and condense thoughts in an intermediate turn."""
    m = copy.deepcopy(msg)
    role = str(m.get("role", "")).lower()

    if role == "tool":
        content = m.get("content")
        if isinstance(content, str):
            tok_count = count_tokens(content).openai
            if tok_count > min_tool_tokens:
                cid = str(m.get("tool_call_id", ""))
                name, args_str = tool_calls_map.get(cid, (m.get("name") or "tool", ""))
                m["content"] = _create_tool_receipt(name, args_str, content, cache)
        return m

    content = m.get("content")
    if isinstance(content, list):
        new_blocks: list[Any] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                b = copy.deepcopy(block)
                res_content = b.get("content")
                if isinstance(res_content, str):
                    if count_tokens(res_content).openai > min_tool_tokens:
                        cid = str(b.get("tool_use_id", ""))
                        name, args_str = tool_calls_map.get(cid, ("tool", ""))
                        b["content"] = _create_tool_receipt(name, args_str, res_content, cache)
                new_blocks.append(b)
            else:
                new_blocks.append(block)
        m["content"] = new_blocks
        return m

    if role == "assistant" and isinstance(content, str):
        if _THOUGHT_BLOCK.search(content):
            cleaned = _THOUGHT_BLOCK.sub("[Thought process condensed]", content).strip()
            m["content"] = cleaned

    return m


def _synthesize_milestone_checkpoint(
    intermediate_msgs: list[dict[str, Any]],
    cache: ContextCache,
) -> str:
    """Summarize decisions, failures, and file references across intermediate turns."""
    text_corpus: list[str] = []
    files_seen: set[str] = set()

    for m in intermediate_msgs:
        content = m.get("content")
        if isinstance(content, str):
            text_corpus.append(content)
            for match in _FILE_PATH.finditer(content):
                path = match.group()
                if not path.startswith("http") and not path.endswith((".com", ".org", ".net")):
                    files_seen.add(path)

    full_intermediate_str = "\n".join(text_corpus)
    ref_id = cache.store(full_intermediate_str, source="milestone-checkpoint")

    decisions: list[str] = []
    for match in _DECISION_PATTERNS.finditer(full_intermediate_str):
        item = match.group().strip()
        if item not in decisions and len(item) > 15:
            decisions.append(item)
        if len(decisions) >= 5:
            break

    failures: list[str] = []
    for match in _FAILED_PATTERNS.finditer(full_intermediate_str):
        item = match.group().strip()
        if item not in failures and len(item) > 15:
            failures.append(item)
        if len(failures) >= 4:
            break

    sections = [
        f"[Milestone Checkpoint: {len(intermediate_msgs)} historical turns condensed]",
    ]
    if decisions:
        sections.append("Key Decisions: " + "; ".join(decisions[:4]))
    if failures:
        sections.append("Resolved Issues: " + "; ".join(failures[:3]))
    if files_seen:
        sorted_files = sorted(list(files_seen))[:10]
        sections.append("Referenced Files: " + ", ".join(f"`{f}`" for f in sorted_files))
    sections.append(f"Full intermediate history cached: usagetrim retrieve {ref_id}")

    return "\n".join(sections)


def trim_conversation_history(
    messages: list[dict[str, Any]],
    max_tokens: int = 16000,
    keep_recent_turns: int = 6,
    cache: ContextCache | None = None,
) -> list[dict[str, Any]]:
    """Dynamically trims and compacts a chat message history array to strictly fit within token limits.

    1. Keeps initial system prompt(s) and user instruction pinned verbatim.
    2. Keeps the last `keep_recent_turns` messages verbatim in working memory.
    3. For intermediate turns:
       - Compacts verbose tool observations into high-signal 1-line receipts with 0-loss CCR caching.
       - Strips historical thinking/thought blocks.
    4. If total tokens still exceed max_tokens, rolls intermediate turns into structured milestone checkpoints.
    5. Preserves JSON message schema and alternating user/assistant message sequences.
    """
    if not messages:
        return []

    target_tokens = max(100, max_tokens)
    c = cache if cache is not None else ContextCache()

    # If already under budget and fewer than keep_recent_turns + 2, return clean copy
    if len(messages) <= keep_recent_turns + 2 and count_history_tokens(messages) <= target_tokens:
        return copy.deepcopy(messages)

    n = len(messages)

    # 1. Identify pinned prefix: all leading system messages + the first user message
    pinned_end = 0
    found_user = False
    for i, m in enumerate(messages):
        role = str(m.get("role", "")).lower()
        if role == "system":
            pinned_end = i + 1
        elif role in {"user", "human"} and not found_user:
            pinned_end = i + 1
            found_user = True
            break
        else:
            break

    pinned_msgs = [copy.deepcopy(m) for m in messages[:pinned_end]]

    # 2. Identify recent window: last keep_recent_turns messages
    recent_start = max(pinned_end, n - keep_recent_turns)

    # Guarantee tool calls and tool responses are not split across the boundary
    while recent_start < n:
        r_msg = messages[recent_start]
        r_role = str(r_msg.get("role", "")).lower()
        if r_role == "tool" and recent_start > pinned_end:
            recent_start -= 1
        elif (
            isinstance(r_msg.get("content"), list)
            and any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in r_msg.get("content", [])
            )
            and recent_start > pinned_end
        ):
            recent_start -= 1
        else:
            break

    intermediate_src = messages[pinned_end:recent_start]
    recent_msgs = [copy.deepcopy(m) for m in messages[recent_start:]]

    if not intermediate_src:
        candidate = pinned_msgs + recent_msgs
        if count_history_tokens(candidate) <= target_tokens:
            return candidate

    # 3. Compact intermediate messages
    tool_calls_map = _extract_tool_calls_map(messages)
    compacted_intermediate: list[dict[str, Any]] = [
        _compact_intermediate_message(m, tool_calls_map, c) for m in intermediate_src
    ]

    candidate = pinned_msgs + compacted_intermediate + recent_msgs
    current_tokens = count_history_tokens(candidate)

    if current_tokens <= target_tokens:
        return candidate

    # 4. If still over budget, summarize intermediate messages into a milestone checkpoint
    if compacted_intermediate:
        milestone_text = _synthesize_milestone_checkpoint(compacted_intermediate, c)

        last_pinned_role = (
            str(pinned_msgs[-1].get("role", "user")).lower() if pinned_msgs else "system"
        )
        first_recent_role = (
            str(recent_msgs[0].get("role", "user")).lower() if recent_msgs else "assistant"
        )

        milestone_msgs: list[dict[str, Any]] = []
        if last_pinned_role in {"user", "human"}:
            if first_recent_role in {"user", "human"}:
                milestone_msgs.append({"role": "assistant", "content": milestone_text})
            else:
                milestone_msgs.append({"role": "assistant", "content": milestone_text})
                milestone_msgs.append(
                    {"role": "user", "content": "Acknowledged. Proceed with current state."}
                )
        else:
            milestone_msgs.append(
                {"role": "user", "content": f"[Milestone Checkpoint]\n{milestone_text}"}
            )
            if first_recent_role != "user":
                milestone_msgs.append(
                    {"role": "assistant", "content": "Acknowledged milestone context."}
                )

        candidate = pinned_msgs + milestone_msgs + recent_msgs
        current_tokens = count_history_tokens(candidate)
        if current_tokens <= target_tokens:
            return candidate

    # 5. Strict active safety: if recent_msgs alone or candidate still exceeds target_tokens,
    # compact any large tool outputs in recent_msgs (except the very latest turn)
    if len(recent_msgs) > 2:
        for idx in range(len(recent_msgs) - 2):
            recent_msgs[idx] = _compact_intermediate_message(
                recent_msgs[idx], tool_calls_map, c, min_tool_tokens=150
            )
        candidate = pinned_msgs + (milestone_msgs if compacted_intermediate else []) + recent_msgs

    return candidate
