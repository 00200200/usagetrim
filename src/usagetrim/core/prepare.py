"""Explicit, local preparation before pasting text into a chat. No usage events."""

from usagetrim.core.distill import distill_conversation
from usagetrim.core.redactor import redact_secrets
from usagetrim.core.safe_filter import safe_compact_output
from usagetrim.metrics.tokenizer import count_model_tokens, count_tokens

MAX_INPUT_BYTES = 128 * 1024
# Desktop-friendly summary/optimize ceiling (Claude Desktop / Codex paste targets).
DEFAULT_PREPARE_BUDGET = 2000
DEFAULT_PREPARE_MODE = "conservative"


def format_prepare_counts(before: int, after: int) -> str:
    """Human-readable before → after with signed delta and percent for CLI/pet."""
    delta = after - before
    if before == 0:
        percent = 0 if after == 0 else 100
    else:
        percent = round(delta / before * 100)
    if delta == 0:
        delta_text = "0"
        percent_text = "0%"
    else:
        sign = "−" if delta < 0 else "+"
        delta_text = f"{sign}{abs(delta):,}"
        percent_text = f"{sign}{abs(percent)}%"
    return f"{before:,} → {after:,} ({delta_text} · {percent_text})"


def prepare_text(
    text: str,
    *,
    mode: str = DEFAULT_PREPARE_MODE,
    budget: int = DEFAULT_PREPARE_BUDGET,
    model: str | None = None,
) -> dict:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("text must be UTF-8 text of at most 128 KiB")
    if mode not in {"conservative", "summary", "optimize", "desktop"}:
        raise ValueError("mode must be conservative, summary, optimize, or desktop")
    if type(budget) is not int or not 128 <= budget <= 8000:
        raise ValueError("budget must be an integer from 128 to 8000")
    if model is not None:
        count_model_tokens("", model)

    def size(value: str) -> int:
        if model is not None:
            return count_model_tokens(value, model).count
        return count_tokens(value).openai

    model_options = {} if model is None else {"model": model}
    redacted = redact_secrets(text)
    if mode == "desktop" and redacted.strip():
        from usagetrim.core.optimizer import optimize_context
        from usagetrim.core.prompt_optimizer import stabilize_cache_prefix
        from usagetrim.core.specialized import filter_dev_server_logs, filter_traceback

        tb_filtered = filter_traceback(redacted)
        dev_filtered = filter_dev_server_logs(tb_filtered)
        stabilized = stabilize_cache_prefix(dev_filtered)
        candidate = optimize_context(stabilized, budget=budget, **model_options).text
    elif mode == "optimize" and redacted.strip():
        from usagetrim.core.optimizer import optimize_context

        candidate = optimize_context(redacted, budget=budget, **model_options).text
    elif mode == "summary" and redacted.strip():
        candidate = distill_conversation(redacted, budget=budget).text
    else:
        candidate = safe_compact_output(redacted)
    # Never make a draft larger just to advertise a transformation. Redactions
    # remain even when their replacement labels themselves cost more tokens.
    if model is not None:
        candidate = redact_secrets(candidate)
    if size(candidate) >= size(redacted):
        candidate = redacted
    if model is not None and mode != "conservative" and size(candidate) > budget:
        from usagetrim.core.adaptive import compress_to_budget

        # Recheck the final candidate and include recovery metadata in the budget.
        # Conservative mode deliberately does not truncate to a target budget.
        candidate = compress_to_budget(
            candidate, budget, original_text=redacted, source="prepare", model=model
        )
    before = size(text)
    after = size(candidate)
    difference = before - after
    if before == 0:
        percent = 0 if after == 0 else 100
    else:
        percent = round((after - before) / before * 100)
    result = {
        "text": candidate,
        "before": before,
        "after": after,
        "difference": difference,
        "percent": percent,
        "counts": format_prepare_counts(before, after),
        "changed": candidate != text,
        "redacted": redacted != text,
        "mode": mode,
        "method": "o200k_base",
        "delivery": "preview",
    }
    if model is not None:
        measurement = count_model_tokens(candidate, model)
        result.update(
            model=measurement.model,
            family=measurement.family.value,
            method=measurement.method,
            is_estimate=measurement.is_estimate,
        )
    return result
