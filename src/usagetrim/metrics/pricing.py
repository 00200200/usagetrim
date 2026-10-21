from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple


class ModelPricing(NamedTuple):
    name: str
    input_per_million: float  # USD per 1M input tokens
    output_per_million: float  # USD per 1M output tokens
    cache_read_per_million: float | None = None  # USD if prompt cached
    cache_write_per_million: float | None = None  # USD for cache creation (not a surcharge)


# Static reference snapshots: provider rates may change and are not fetched here.
# Existing rates remain unchanged for estimate_savings() compatibility.
# None means no rate is modeled.
PRICING_TABLE: dict[str, ModelPricing] = {
    # Anthropic: cache writes use the 5-minute TTL (1.25x standard input).
    # https://platform.claude.com/docs/en/build-with-claude/prompt-caching
    "claude-3-7-sonnet": ModelPricing("Claude 3.7 Sonnet", 3.00, 15.00, 0.30, 3.75),
    "claude-3-5-sonnet": ModelPricing("Claude 3.5 Sonnet", 3.00, 15.00, 0.30, 3.75),
    "claude-3-5-haiku": ModelPricing("Claude 3.5 Haiku", 0.80, 4.00, 0.08, 1.00),
    "claude-3-opus": ModelPricing("Claude 3 Opus", 15.00, 75.00, 1.50, 18.75),
    # OpenAI
    "gpt-4o": ModelPricing("GPT-4o", 2.50, 10.00, 1.25),
    "gpt-4o-mini": ModelPricing("GPT-4o mini", 0.15, 0.60, 0.075),
    "o1": ModelPricing("o1 (reasoning)", 15.00, 60.00, 7.50),
    "o3-mini": ModelPricing("o3-mini", 1.10, 4.40, 0.55),
    # Historical standard (non-promotional) DeepSeek model rates. Automatic
    # cache misses belong to standard input; no separate write rate is modeled.
    # https://api-docs.deepseek.com/news/news1226/
    # https://api-docs.deepseek.com/news/news250120/
    "deepseek-v3": ModelPricing("DeepSeek-V3", 0.27, 1.10, 0.07),
    "deepseek-r1": ModelPricing("DeepSeek-R1", 0.55, 2.19, 0.14),
    # Google Gemini
    # gemini-2.0-pro is omitted: it was experimental, with no standard paid rate.
    # Do not substitute another Gemini model's pricing for it.
    "gemini-2.0-flash": ModelPricing("Gemini 2.0 Flash", 0.10, 0.40, 0.025),
    "gemini-1.5-pro": ModelPricing("Gemini 1.5 Pro", 1.25, 5.00, 0.31),
    "gemini-1.5-flash": ModelPricing("Gemini 1.5 Flash", 0.075, 0.30, 0.018),
}


def calculate_cost(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Estimate USD from disjoint token categories using static reference rates.

    Rates are snapshots and may differ from subsequent provider pricing.
    Input tokens exclude cache reads/writes: cache creation is charged at its
    full rate, not added to standard input again. Unmodeled cache rates raise
    ValueError for nonzero usage. Storage, TTL variants and other fees are not
    included. This function performs no network requests.
    """
    if not isinstance(model, str) or model not in PRICING_TABLE:
        raise ValueError(f"Unknown model: {model!r}")
    pricing = PRICING_TABLE[model]
    categories = (
        ("input_tokens", input_tokens, pricing.input_per_million),
        ("output_tokens", output_tokens, pricing.output_per_million),
        ("cache_read_tokens", cache_read_tokens, pricing.cache_read_per_million),
        ("cache_write_tokens", cache_write_tokens, pricing.cache_write_per_million),
    )
    total = 0.0
    for name, tokens, rate in categories:
        if type(tokens) is not int or tokens < 0:
            raise ValueError(f"{name} must be a non-negative integer")
        if tokens:
            if rate is None:
                raise ValueError(f"No {name} rate modeled for {model!r}")
            total += tokens * rate / 1_000_000
    return total


@dataclass(frozen=True)
class CostSavings:
    claude_saved_usd: float
    openai_saved_usd: float
    gemini_saved_usd: float

    @property
    def avg_saved_usd(self) -> float:
        return (self.claude_saved_usd + self.openai_saved_usd + self.gemini_saved_usd) / 3.0

    def format_claude(self) -> str:
        return f"${self.claude_saved_usd:.4f}"

    def format_avg(self) -> str:
        return f"${self.avg_saved_usd:.4f}"


def estimate_savings(
    saved_claude_tokens: int, saved_openai_tokens: int, saved_gemini_tokens: int
) -> CostSavings:
    # Baseline comparison: Claude 3.5/3.7 Sonnet ($3.00/1M input), GPT-4o ($2.50/1M input), Gemini 1.5 Pro ($1.25/1M input)
    claude_usd = (saved_claude_tokens / 1_000_000.0) * PRICING_TABLE[
        "claude-3-7-sonnet"
    ].input_per_million
    openai_usd = (saved_openai_tokens / 1_000_000.0) * PRICING_TABLE["gpt-4o"].input_per_million
    gemini_usd = (saved_gemini_tokens / 1_000_000.0) * PRICING_TABLE[
        "gemini-1.5-pro"
    ].input_per_million

    return CostSavings(
        claude_saved_usd=claude_usd,
        openai_saved_usd=openai_usd,
        gemini_saved_usd=gemini_usd,
    )
