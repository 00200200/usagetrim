from __future__ import annotations

from usagetrim.core.cache import ContextCache
from usagetrim.core.cleaner import CleanerOptions, compact_terminal_output, strip_ansi
from usagetrim.core.redactor import redact_secrets
from usagetrim.core.safe_filter import recovery_notice
from usagetrim.metrics.tokenizer import count_model_tokens, count_tokens, model_counting


@model_counting
def compress_to_budget(
    text: str,
    max_tokens: int,
    provider: str = "claude",
    source: str = "adaptive",
    *,
    original_text: str | None = None,
    suffix: str = "",
    model: str | None = None,
) -> str:
    """Bound the complete text, including recovery metadata, using a local estimator.

    Truncated content is recoverable after secret redaction. Tiny budgets that
    cannot hold a recovery reference raise rather than silently losing content.
    Provider estimates are not billing counts for any particular model.
    An explicit model selects model-family counting, which may also be estimated.
    """
    result, _ = _compress_to_budget_with_ref(
        text,
        max_tokens,
        provider,
        source,
        original_text=original_text,
        suffix=suffix,
        model=model,
    )
    return result


@model_counting
def _compress_to_budget_with_ref(
    text: str,
    max_tokens: int,
    provider: str = "claude",
    source: str = "adaptive",
    *,
    original_text: str | None = None,
    suffix: str = "",
    model: str | None = None,
) -> tuple[str, str | None]:
    """Return the emitted text and its authoritative recovery reference."""
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    if provider not in {"claude", "openai", "gemini"}:
        raise ValueError("provider must be claude, openai, or gemini")
    if model is not None:
        # Reject unsupported models before constructing or writing to the cache.
        count_model_tokens("", model)

    def size(value: str) -> int:
        value = redact_secrets(value)
        if model is not None:
            return count_model_tokens(value, model).count
        return getattr(count_tokens(value), provider)

    original = redact_secrets(text if original_text is None else original_text)
    cleaned = redact_secrets(text)
    suffix = redact_secrets(suffix)
    if cleaned.rstrip("\n") == original.rstrip("\n") and size(cleaned + suffix) <= max_tokens:
        return redact_secrets(cleaned + suffix), None

    # Sanitization is deliberately applied before persistence, not only display.
    ref_id = ContextCache().store(original, source=source)
    existing_notice = recovery_notice(ref_id)
    if cleaned.endswith(existing_notice):
        cleaned = cleaned.removesuffix(existing_notice)
    marker = f"\n[... Ref: {ref_id}; retrieve omitted lines]"
    if size(marker + suffix) > max_tokens:
        raise ValueError("max_tokens is too small for the recovery reference")

    cleaned = redact_secrets(strip_ansi(cleaned).strip())
    if size(cleaned + marker + suffix) <= max_tokens:
        return redact_secrets(cleaned + marker + suffix), ref_id

    target_lines = max(4, max_tokens // 12)
    compacted = compact_terminal_output(
        cleaned,
        CleanerOptions(
            max_lines=target_lines,
            head_lines=max(1, target_lines // 3),
            tail_lines=max(2, target_lines * 2 // 3),
            enable_cache=False,
        ),
    )
    if size(compacted + marker + suffix) <= max_tokens:
        return redact_secrets(compacted + marker + suffix), ref_id

    # Retain both the start and final diagnostics, even for a single huge line.
    # Only measured candidates are returned: BPE counts need not be monotonic.
    best = redact_secrets(marker + suffix)
    low, high = 0, len(compacted)
    while low <= high:
        keep = (low + high) // 2
        head = keep // 3
        tail = keep - head
        candidate = compacted[:head] + marker
        if tail:
            candidate += "\n" + compacted[-tail:]
        candidate = redact_secrets(candidate + suffix)
        if size(candidate) <= max_tokens:
            best = candidate
            low = keep + 1
        else:
            high = keep - 1
    return best, ref_id
