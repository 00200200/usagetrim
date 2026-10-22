"""Unified Autonomous Context Optimizer for AI coding assistants.

Combines all compression best practices (Headroom CCR, TOON tabular compression,
prompt cache prefix alignment, AST skeletonization, and log/traceback folding)
into a single smart, self-routing pipeline.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from usagetrim.core.cache import ContextCache
from usagetrim.core.clip import compact_text
from usagetrim.core.diff_slimmer import slim_git_diff
from usagetrim.core.distill import (
    _compact_tool_observations,
    _extract_tool_calls_map,
    distill_conversation,
)
from usagetrim.core.prompt_optimizer import align_prompt, lint_prompt
from usagetrim.core.redactor import redact_secrets
from usagetrim.core.table import compact_table
from usagetrim.metrics.tokenizer import count_tokens

_COLOR = re.compile(r"\x1b\[[0-9;]*m")
_FENCE_PATTERN = re.compile(r"(```([a-zA-Z0-9_\-]+)?\n(.*?)```)", re.DOTALL)


def mask_completed_observations(history: list[dict]) -> list[dict]:
    """Copy history, masking verbose results synthesized later in the same turn.

    A nonblank assistant string or text block completes preceding observations.
    Ordinary user/human, system and developer messages reset the turn; Anthropic
    user messages containing only tool_result blocks continue it. Mixed user
    instructions/results are left untouched. Active results remain verbatim.
    """
    result = copy.deepcopy(history)
    pending: list[int] = []
    tool_calls_map: dict[str, tuple[str, str]] = {}
    cache = None
    for index, message in enumerate(history):
        role = str(message.get("role", "")).lower()
        content = message.get("content")
        tool_results = (
            isinstance(content, list)
            and bool(content)
            and all(
                isinstance(block, dict) and block.get("type") == "tool_result" for block in content
            )
        )
        if role in {"system", "developer", "user", "human"} and not (
            role == "user" and tool_results
        ):
            pending.clear()
            tool_calls_map.clear()
        tool_calls_map.update(_extract_tool_calls_map([message]))
        if role == "tool" or (role == "user" and tool_results):
            pending.append(index)
        elif role == "assistant":
            has_text = isinstance(content, str) and bool(content.strip())
            if isinstance(content, list):
                has_text = any(
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                    and bool(block["text"].strip())
                    for block in content
                )
            if has_text and pending:
                if cache is None:
                    cache = ContextCache()
                for observation in pending:
                    result[observation] = _compact_tool_observations(
                        history[observation], tool_calls_map, cache
                    )
                pending.clear()
    return result


@dataclass
class OptimizeResult:
    original_tokens: int
    optimized_tokens: int
    saved_tokens: int
    reduction_pct: float
    ref_id: str
    text: str
    primary_mode: str
    pipeline_stages: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_tokens": self.original_tokens,
            "optimized_tokens": self.optimized_tokens,
            "saved_tokens": self.saved_tokens,
            "reduction_pct": self.reduction_pct,
            "ref_id": self.ref_id,
            "text": self.text,
            "primary_mode": self.primary_mode,
            "pipeline_stages": self.pipeline_stages,
        }


def _optimize_code_fence(lang: str, body: str, budget: int) -> tuple[str, str | None]:
    """Optimize code blocks inside markdown fences."""
    lang_clean = (lang or "").lower().strip()
    body_clean = body.strip()

    # 1. JSON fence -> compact with TOON
    if lang_clean == "json" or (
        body_clean.startswith(("[", "{")) and body_clean.endswith(("]", "}"))
    ):
        res = compact_table(body, budget=budget, format_type="toon")
        if res.compacted_tokens < res.original_tokens:
            return f"```text\n{res.text.strip()}\n```", "fence:toon"

    # 2. Diff fence -> slim diff
    if lang_clean in {"diff", "patch"} or body_clean.startswith(("diff --git", "--- a/")):
        slimmed = slim_git_diff(body)
        if count_tokens(slimmed).openai < count_tokens(body).openai:
            return f"```{lang_clean or 'diff'}\n{slimmed.strip()}\n```", "fence:diff"

    # 3. Log / Traceback inside fence
    if "Traceback (most recent call last):" in body or "FAILED " in body or " PASSED" in body:
        clip_res = compact_text(body, budget=budget)
        if clip_res.compacted_tokens < clip_res.original_tokens:
            return f"```{lang_clean or 'text'}\n{clip_res.text.strip()}\n```", "fence:clip"

    return "", None


def optimize_context(
    raw_input: str,
    budget: int = 2000,
    preserve_prompt_prefix: bool = True,
) -> OptimizeResult:
    """Autonomously optimize any prompt, log, table, or transcript with 0-loss CCR guarantee."""
    cleaned = _COLOR.sub("", raw_input)
    redacted = redact_secrets(cleaned)
    original_tokens = count_tokens(redacted).openai

    cache = ContextCache()
    ref_id = cache.store(redacted, source="unified-optimizer")

    if original_tokens <= 12:
        return OptimizeResult(
            original_tokens=original_tokens,
            optimized_tokens=original_tokens,
            saved_tokens=0,
            reduction_pct=0.0,
            ref_id=ref_id,
            text=redacted,
            primary_mode="passthrough",
            pipeline_stages=["secret_redaction"],
        )

    pipeline_stages: list[str] = ["secret_redaction"]
    working_text = redacted
    primary_mode = "hybrid"

    # Step 1: Check for Hybrid Intra-Fence Blocks
    fences = list(_FENCE_PATTERN.finditer(working_text))
    if fences:
        transformed_parts = []
        last_idx = 0
        fence_optimized = False
        for match in fences:
            full_fence, lang, body = match.group(1), match.group(2), match.group(3)
            transformed_parts.append(working_text[last_idx : match.start()])
            new_fence, stage = _optimize_code_fence(lang, body, budget=budget // 2)
            if new_fence and stage:
                transformed_parts.append(new_fence)
                pipeline_stages.append(stage)
                fence_optimized = True
            else:
                transformed_parts.append(full_fence)
            last_idx = match.end()
        transformed_parts.append(working_text[last_idx:])
        if fence_optimized:
            working_text = "".join(transformed_parts)
            primary_mode = "intra_fence_hybrid"

    # Step 2: Whole-Document Semantic Classification (if not already handled)
    stripped = working_text.strip()

    # A. Check Tabular Data (JSON Array or CSV)
    if primary_mode != "intra_fence_hybrid" and (
        (stripped.startswith("[") and stripped.endswith("]"))
        or (stripped.startswith("{") and '"' in stripped and "{" in stripped)
    ):
        tbl_res = compact_table(working_text, budget=budget, format_type="toon")
        if tbl_res.compacted_tokens < original_tokens:
            working_text = tbl_res.text
            primary_mode = "table_toon"
            pipeline_stages.append("table_compression")

    # B. Check Multi-turn Chat Conversation / Transcript
    elif primary_mode != "intra_fence_hybrid" and re.search(
        r"^(?:#{1,4}\s*)?(User|Assistant|Human|System):\s*",
        working_text,
        re.MULTILINE | re.IGNORECASE,
    ):
        dist_res = distill_conversation(working_text, budget=budget)
        if dist_res.distilled_tokens < original_tokens:
            working_text = dist_res.text
            primary_mode = "conversation_distill"
            pipeline_stages.append("conversation_distillation")

    # C. Check System Prompt with cache-busting prefix
    elif preserve_prompt_prefix:
        lint = lint_prompt(working_text)
        if lint["issues"]:
            align_res = align_prompt(working_text)
            working_text = align_res.aligned_text
            primary_mode = "prompt_cache_align"
            pipeline_stages.append("prompt_cache_alignment")

    # C2. Dev server logs or traceback
    if primary_mode == "hybrid":
        from usagetrim.core.specialized import filter_dev_server_logs, filter_traceback

        tb_filtered = filter_traceback(working_text)
        dev_filtered = filter_dev_server_logs(tb_filtered)
        if count_tokens(dev_filtered).openai < count_tokens(working_text).openai:
            working_text = dev_filtered
            primary_mode = "dev_log_traceback"
            pipeline_stages.append("dev_log_traceback_compaction")

    # D. General text / terminal logs / diffs fallback
    if primary_mode == "hybrid" or working_text == redacted:
        clip_res = compact_text(working_text, budget=budget)
        if clip_res.compacted_tokens < original_tokens:
            working_text = clip_res.text
            primary_mode = clip_res.content_type
            pipeline_stages.append(f"clip_{clip_res.content_type}")

    if working_text == redacted and primary_mode == "hybrid":
        primary_mode = "passthrough"

    # Step 3: Strict Budget Ceiling Enforcement
    current_tokens = count_tokens(working_text).openai
    if current_tokens > budget:
        lines = working_text.splitlines(keepends=True)
        target = max(6, int(len(lines) * (budget / current_tokens) * 0.8))
        head_count = target // 2
        tail_count = target - head_count
        head = lines[:head_count]
        tail = lines[-tail_count:] if tail_count > 0 else []
        omitted = len(lines) - len(head) - len(tail)
        trunc_notice = (
            f"\n[... {omitted} lines omitted by UsageTrim to fit {budget} token budget. "
            f"Ref: {ref_id} - recover with: usagetrim retrieve {ref_id} ...]\n"
        )
        working_text = "".join(head) + trunc_notice + "".join(tail)
        current_tokens = count_tokens(working_text).openai

        while current_tokens > budget and (len(head) > 1 or len(tail) > 0):
            if len(tail) > 0:
                tail.pop(0)
            else:
                head.pop()
            omitted = len(lines) - len(head) - len(tail)
            trunc_notice = (
                f"\n[... {omitted} lines omitted by UsageTrim to fit {budget} token budget. "
                f"Ref: {ref_id} - recover with: usagetrim retrieve {ref_id} ...]\n"
            )
            working_text = "".join(head) + trunc_notice + "".join(tail)
            current_tokens = count_tokens(working_text).openai
        pipeline_stages.append("budget_ceiling_enforcement")

    saved = max(0, original_tokens - current_tokens)
    pct = round((saved / original_tokens) * 100, 1) if original_tokens else 0.0

    return OptimizeResult(
        original_tokens=original_tokens,
        optimized_tokens=current_tokens,
        saved_tokens=saved,
        reduction_pct=pct,
        ref_id=ref_id,
        text=working_text,
        primary_mode=primary_mode,
        pipeline_stages=pipeline_stages,
    )
