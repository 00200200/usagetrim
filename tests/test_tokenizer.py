from dataclasses import FrozenInstanceError

import pytest

from usagetrim.metrics import tokenizer
from usagetrim.metrics.pricing import estimate_savings
from usagetrim.metrics.tokenizer import (
    ModelFamily,
    ModelTokenCount,
    TokenCount,
    compute_metrics,
    count_model_tokens,
    count_tokens,
)


def test_count_tokens(model_token_backend):
    text = "def hello_world():\n    return 'Hello, World!'"
    tok = count_tokens(text)
    assert isinstance(tok, TokenCount)
    assert tok.claude > 0
    assert tok.openai > 0
    assert tok.gemini > 0
    assert tok.avg > 0


def test_compute_metrics(model_token_backend):
    raw = "A very long raw string with lots of repetitive details... " * 100
    compact = "A very long raw string..."
    m = compute_metrics(raw, compact)
    assert m.reduction_pct > 80.0
    assert m.saved_tokens.claude > 0
    assert m.saved_tokens.openai > 0
    assert m.saved_tokens.gemini > 0


def test_estimate_savings():
    savings = estimate_savings(100_000, 100_000, 100_000)
    assert savings.claude_saved_usd > 0
    assert savings.openai_saved_usd > 0
    assert savings.gemini_saved_usd > 0
    assert "$" in savings.format_avg()


class FakeEncoding:
    def __init__(self, name):
        self.name = name

    def encode(self, text, *, disallowed_special):
        assert disallowed_special == ()
        return text.split()


@pytest.fixture
def model_token_backend(monkeypatch):
    monkeypatch.setattr(tokenizer, "get_o200k", lambda: FakeEncoding("o200k_base"))
    monkeypatch.setattr(tokenizer, "get_cl100k", lambda: FakeEncoding("cl100k_base"))
    monkeypatch.setattr(
        tokenizer,
        "_model_encoding",
        lambda name: tokenizer.get_o200k() if name == "o200k_base" else tokenizer.get_cl100k(),
    )


@pytest.mark.parametrize(
    "model,count,method,estimate",
    [
        ("openai", 2, "o200k_base", False),
        ("claude", 3, "legacy-o200k_base-x1.10", True),
        ("gemini", 3, "legacy-o200k_base-x1.02", True),
        ("llama", 3, "utf8-bytes-div4-ceil", True),
        ("generic", 3, "utf8-bytes-div4-ceil", True),
    ],
)
def test_model_tokens_metadata(model_token_backend, model, count, method, estimate):
    result = count_model_tokens("hello world", model)
    assert result == ModelTokenCount(count, model, ModelFamily(model), method, estimate)
    assert count_model_tokens("", model).count == 0


def test_model_tokens_unknown_rejected_before_backend(monkeypatch):
    def fail():
        pytest.fail("unknown model must not load a backend")

    monkeypatch.setattr(tokenizer, "get_o200k", fail)
    with pytest.raises(ValueError, match="openai, claude, gemini, llama, generic"):
        count_model_tokens("hello", "gpt-something")


def test_model_tokens_cl100k_fallback_and_cached_fallback_identity(
    monkeypatch, model_token_backend
):
    def unavailable():
        raise OSError("encoding unavailable")

    monkeypatch.setattr(tokenizer, "get_o200k", unavailable)
    monkeypatch.setattr(tokenizer, "get_cl100k", lambda: FakeEncoding("cl100k_base"))
    result = count_model_tokens("hello world", "openai")
    assert result.count == 2 and result.method == "cl100k_base" and result.is_estimate
    # The legacy helper may itself return a previously cached cl100k fallback.
    monkeypatch.setattr(tokenizer, "get_o200k", lambda: FakeEncoding("cl100k_base"))
    assert count_model_tokens("hello world", "openai") == result
    assert count_model_tokens("hello world", "claude").method == "legacy-cl100k_base-x1.10"


@pytest.mark.parametrize("model", [item.value for item in ModelFamily])
def test_model_tokens_pure_python_fallback(monkeypatch, model_token_backend, model):
    def unavailable():
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(tokenizer, "get_o200k", unavailable)
    monkeypatch.setattr(tokenizer, "get_cl100k", unavailable)
    text = "Zażółć 🦙 漢字"
    result = count_model_tokens(text, model)
    assert result == count_model_tokens(text, model)
    assert result.count > 0 and result.is_estimate and "utf8" in result.method
    assert count_model_tokens("", model).count == 0


@pytest.mark.parametrize("model", [item.value for item in ModelFamily])
def test_model_tokens_missing_or_corrupt_cache_does_not_download(monkeypatch, tmp_path, model):
    import tiktoken.load
    import tiktoken.registry

    def forbidden(*args, **kwargs):
        raise AssertionError("network access attempted")

    # read_file is tiktoken's uncached I/O boundary, including HTTP downloads.
    monkeypatch.setattr(tiktoken.load, "read_file", forbidden)
    monkeypatch.setattr(tokenizer, "_O200K_ENC", None)
    monkeypatch.setattr(tokenizer, "_CL100K_ENC", None)
    monkeypatch.setattr(tiktoken.registry, "ENCODINGS", {})
    monkeypatch.setattr(tokenizer.tiktoken, "get_encoding", forbidden)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    for name in ("o200k_base", "cl100k_base"):
        (tmp_path / f"{name}.tiktoken").write_bytes(b"corrupt")
    result = count_model_tokens("hello", model)
    assert "utf8-bytes-div4-ceil" in result.method and result.is_estimate
    for path in tmp_path.iterdir():
        path.unlink()
    assert count_model_tokens("hello", model) == result


@pytest.mark.parametrize("name", ["o200k_base", "cl100k_base"])
@pytest.mark.parametrize("source", ["legacy", "registry"])
def test_model_tokens_reuses_instantiated_encoding_without_loading(monkeypatch, name, source):
    import tiktoken.load
    import tiktoken.registry

    def forbidden(*args, **kwargs):
        raise AssertionError("vocabulary loading attempted")

    monkeypatch.setattr(tiktoken.load, "read_file", forbidden)
    monkeypatch.setattr(tokenizer.tiktoken, "get_encoding", forbidden)
    encoding = FakeEncoding(name)
    monkeypatch.setattr(tokenizer, "_O200K_ENC", encoding if source == "legacy" else None)
    monkeypatch.setattr(tokenizer, "_CL100K_ENC", None)
    monkeypatch.setattr(
        tiktoken.registry, "ENCODINGS", {name: encoding} if source == "registry" else {}
    )
    result = count_model_tokens("hello world", "openai")
    assert result.count == 2 and result.method == name
    assert result.is_estimate is (name != "o200k_base")


@pytest.mark.parametrize("registry_state", [None, [], {"o200k_base": FakeEncoding("changed")}])
def test_model_tokens_incompatible_registry_falls_back(monkeypatch, registry_state):
    import tiktoken.registry

    monkeypatch.setattr(tokenizer, "_O200K_ENC", None)
    monkeypatch.setattr(tokenizer, "_CL100K_ENC", None)
    monkeypatch.setattr(tiktoken.registry, "ENCODINGS", registry_state)
    result = count_model_tokens("hello world", "openai")
    assert result.count == 3 and result.is_estimate
    assert result.method == "utf8-bytes-div4-ceil"


def test_model_tokens_fallback_rule_and_frozen_result():
    assert count_model_tokens("🦙", "generic").count == 1
    result = count_model_tokens("abcde", "llama")
    assert result.count == 2
    with pytest.raises(FrozenInstanceError):
        result.count = 3
    with pytest.raises(ValueError, match="nonnegative integer"):
        ModelTokenCount(-1, "generic", ModelFamily.GENERIC, "test", True)


def test_model_tokens_legacy_contract_unchanged(model_token_backend):
    result = count_tokens("hello world")
    assert result == TokenCount(claude=3, openai=2, gemini=3)
    assert result.avg == 2
    metrics = compute_metrics("hello world", "hello")
    assert metrics.raw_tokens == result
    assert metrics.compact_tokens == TokenCount(claude=2, openai=1, gemini=2)


@pytest.mark.parametrize("model", [item.value for item in ModelFamily])
def test_model_tokens_surrogate_fallback(monkeypatch, model):
    import tiktoken.registry

    monkeypatch.setattr(tokenizer, "_O200K_ENC", None)
    monkeypatch.setattr(tokenizer, "_CL100K_ENC", None)
    monkeypatch.setattr(tiktoken.registry, "ENCODINGS", {})

    text = "bad-\udcff"
    result = count_model_tokens(text, model)

    assert result.count > 0
    assert result.is_estimate
    assert "utf8-bytes-div4-ceil" in result.method


def test_model_tokens_surrogate_reuses_warm_encoding_without_fallback(monkeypatch):
    import tiktoken.registry

    class SurrogateEncoding:
        name = "o200k_base"

        def encode(self, text, *, disallowed_special):
            assert text == "bad-\udcff"
            assert disallowed_special == ()
            return [1, 2]

    encoding = SurrogateEncoding()
    monkeypatch.setattr(tokenizer, "_O200K_ENC", encoding)
    monkeypatch.setattr(tokenizer, "_CL100K_ENC", None)
    monkeypatch.setattr(tiktoken.registry, "ENCODINGS", {})

    def forbidden_fallback(text):
        pytest.fail("UTF-8 fallback must not run when an encoding is already available")

    monkeypatch.setattr(tokenizer, "_utf8_estimate", forbidden_fallback)

    result = count_model_tokens("bad-\udcff", "openai")

    assert result.count == 2
    assert result.method == "o200k_base"
    assert result.is_estimate is False
