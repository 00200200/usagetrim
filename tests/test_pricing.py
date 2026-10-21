import pytest

from usagetrim.metrics.pricing import PRICING_TABLE, ModelPricing, calculate_cost, estimate_savings


def test_model_pricing_preserves_optional_positional_arguments():
    assert ModelPricing("Example", 1.0, 2.0).cache_write_per_million is None
    assert ModelPricing("Example", 1.0, 2.0, 0.1).cache_read_per_million == 0.1


def test_calculate_cost_standard_input_and_output():
    assert calculate_cost("gpt-4o", input_tokens=100_000, output_tokens=20_000) == pytest.approx(
        0.45
    )
    assert calculate_cost("gpt-4o") == 0.0


def test_calculate_cost_keeps_cache_categories_separate():
    # Each token belongs to one category; writes cost 3.75, not 3 + 3.75.
    assert calculate_cost(
        "claude-3-7-sonnet",
        input_tokens=100_000,
        output_tokens=20_000,
        cache_read_tokens=200_000,
        cache_write_tokens=40_000,
    ) == pytest.approx(0.81)


@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-3-7-sonnet", 3.75),
        ("claude-3-5-sonnet", 3.75),
        ("claude-3-5-haiku", 1.0),
        ("claude-3-opus", 18.75),
    ],
)
def test_anthropic_five_minute_cache_write_rates(model, expected):
    assert calculate_cost(model, cache_write_tokens=1_000_000) == expected


@pytest.mark.parametrize(
    "model,input_rate,output_rate,cache_read_rate",
    [("deepseek-v3", 0.27, 1.10, 0.07), ("deepseek-r1", 0.55, 2.19, 0.14)],
)
def test_deepseek_reference_rates(model, input_rate, output_rate, cache_read_rate):
    assert calculate_cost(model, input_tokens=1_000_000) == input_rate
    assert calculate_cost(model, output_tokens=1_000_000) == output_rate
    assert calculate_cost(model, cache_read_tokens=1_000_000) == cache_read_rate


def test_unmodeled_cache_write_rate_does_not_silently_become_free():
    with pytest.raises(ValueError, match="No cache_write_tokens rate"):
        calculate_cost("gpt-4o", cache_write_tokens=1)


def test_zero_rate_is_distinct_from_unmodeled_rate(monkeypatch):
    monkeypatch.setitem(PRICING_TABLE, "free-cache", ModelPricing("Example", 1, 2, 0, 0))
    assert calculate_cost("free-cache", cache_read_tokens=100, cache_write_tokens=100) == 0


@pytest.mark.parametrize("model", ["unknown", "", None, []])
def test_unknown_or_invalid_model_is_rejected(model):
    with pytest.raises(ValueError, match="Unknown model"):
        calculate_cost(model)


@pytest.mark.parametrize(
    "category", ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"]
)
@pytest.mark.parametrize("tokens", [-1, True, 1.5, "100"])
def test_invalid_token_counts_are_rejected(category, tokens):
    with pytest.raises(ValueError, match=category):
        calculate_cost("claude-3-7-sonnet", **{category: tokens})


def test_estimate_savings_preserves_baselines_and_signed_telemetry_deltas():
    savings = estimate_savings(100_000, 100_000, 100_000)
    assert savings.claude_saved_usd == pytest.approx(0.30)
    assert savings.openai_saved_usd == pytest.approx(0.25)
    assert savings.gemini_saved_usd == pytest.approx(0.125)
    assert savings.avg_saved_usd == pytest.approx(0.225)
    assert savings.format_claude() == "$0.3000"
    assert savings.format_avg() == "$0.2250"
    expansion = estimate_savings(-100_000, -100_000, -100_000)
    assert expansion.avg_saved_usd == pytest.approx(-0.225)
