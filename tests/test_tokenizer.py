from dataclasses import FrozenInstanceError

import pytest

from usagetrim.metrics import tokenizer
from usagetrim.metrics.pricing import estimate_savings
from usagetrim.metrics.tokenizer import (
    CacheBreakpointInfo,
    ModelFamily,
    ModelTokenCount,
    TokenCount,
    check_cache_breakpoint,
    compute_metrics,
    count_model_tokens,
    count_tokens,
    detect_model_family,
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


@pytest.mark.parametrize("model", [item.value for item in ModelFamily])
@pytest.mark.parametrize(
    "text,base_count",
    [
        ("", 0),
        ("hello", 2),
        ("🦙漢字", 3),
        ("x = 1\n", 2),
        ('{"ok":true}', 3),
        (" \t\r\n", 1),
        ("\udcff", 1),
        ("\ud83e\udd99", 1),
    ],
)
def test_model_api_imports_and_runs_without_tiktoken(monkeypatch, model, text, base_count):
    import builtins
    import runpy
    import socket

    original_import = builtins.__import__
    attempted_imports = []

    def without_tiktoken(name, *args, **kwargs):
        if name == "tiktoken" or name.startswith("tiktoken."):
            attempted_imports.append(name)
            raise ModuleNotFoundError("No module named 'tiktoken'", name="tiktoken")
        return original_import(name, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("network access attempted without tiktoken")

    monkeypatch.setattr(builtins, "__import__", without_tiktoken)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    namespace = runpy.run_path(tokenizer.__file__)
    assert namespace["tiktoken"] is None
    expected = base_count + int(base_count > 0 and model in {"claude", "gemini"})
    count = namespace["count_model_tokens"](text, model)
    assert count.count == expected
    assert count.is_estimate is True
    assert "utf8-bytes-div4-ceil" in count.method
    assert namespace["count_model_tokens"](text, model) == count
    breakpoint = namespace["check_cache_breakpoint"](text, model, min_tokens=1)
    assert breakpoint.token_count == expected
    assert breakpoint.method == count.method
    assert namespace["detect_model_family"]({"LLM_MODEL": "claude-sonnet-4"}).value == "claude"
    # Only the initial optional import is attempted; no loader or registry import.
    assert attempted_imports == ["tiktoken"]


@pytest.mark.parametrize("name", ["o200k_base", "cl100k_base"])
def test_model_tokens_reuses_warm_encoding_when_backend_unavailable(monkeypatch, name):
    monkeypatch.setattr(tokenizer, "tiktoken", None)
    monkeypatch.setattr(tokenizer, "_O200K_ENC", FakeEncoding(name))
    monkeypatch.setattr(tokenizer, "_CL100K_ENC", None)
    result = count_model_tokens("hello world", "openai")
    assert result.count == 2
    assert result.method == name
    assert result.is_estimate is (name != "o200k_base")


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


def test_tokenizer_cache_boundary_delegates_arguments_and_result(monkeypatch):
    from usagetrim.core import prompt_optimizer

    calls = []

    def align(text, target_boundary=1024, *, model="claude"):
        calls.append((text, target_boundary, model))
        return "delegated result"

    monkeypatch.setattr(prompt_optimizer, "align_to_cache_boundary", align)
    assert tokenizer.align_to_cache_boundary("prompt") == "delegated result"
    assert tokenizer.align_to_cache_boundary("🦙", 128, model="llama") == "delegated result"
    assert calls == [("prompt", 1024, "claude"), ("🦙", 128, "llama")]


@pytest.mark.parametrize("boundary", [0, -1, True])
def test_tokenizer_cache_boundary_preserves_validation(boundary):
    with pytest.raises(ValueError, match="positive integer"):
        tokenizer.align_to_cache_boundary("prompt", boundary)


def test_tokenizer_cache_boundary_offline_and_unknown_model(monkeypatch):
    import tiktoken.load
    import tiktoken.registry

    def forbidden(*args, **kwargs):
        pytest.fail("network or vocabulary loading attempted")

    monkeypatch.setattr(tiktoken.load, "read_file", forbidden)
    monkeypatch.setattr(tokenizer.tiktoken, "get_encoding", forbidden)
    monkeypatch.setattr(tokenizer, "_O200K_ENC", None)
    monkeypatch.setattr(tokenizer, "_CL100K_ENC", None)
    monkeypatch.setattr(tiktoken.registry, "ENCODINGS", {})
    text = "session_id: abc123\n" + "Keep 漢字🙂.\n" * 30
    result = tokenizer.align_to_cache_boundary(text, 4)
    assert result.startswith("Keep 漢字🙂.\n" * 30)
    assert "session_id: abc123" in result
    with pytest.raises(ValueError, match="Unknown model"):
        tokenizer.align_to_cache_boundary(text, model="unknown")


def test_model_tokens_unknown_rejected_before_backend(monkeypatch):
    def fail():
        pytest.fail("unknown model must not load a backend")

    monkeypatch.setattr(tokenizer, "get_o200k", fail)
    with pytest.raises(ValueError, match="openai, claude, gemini, llama, generic"):
        count_model_tokens("hello", "gpt-something")


@pytest.mark.parametrize(
    "variable,model,expected",
    [
        ("LLM_MODEL", "gpt-4o", ModelFamily.OPENAI),
        ("LLM_MODEL", "claude-sonnet-4", ModelFamily.CLAUDE),
        ("ANTHROPIC_MODEL", "claude-3-7-sonnet", ModelFamily.CLAUDE),
        ("OPENAI_MODEL", "o3-mini", ModelFamily.OPENAI),
        ("LLM_MODEL", "gemini-2.5-pro", ModelFamily.GEMINI),
        ("LLM_MODEL", "meta-llama/Llama-3.1-8B", ModelFamily.LLAMA),
    ],
)
def test_detect_model_family_recognizes_explicit_families(variable, model, expected):
    assert detect_model_family({variable: model}) is expected


def test_detect_model_family_priority_and_unknown_do_not_fall_through():
    assert (
        detect_model_family(
            {
                "LLM_MODEL": "claude-3-sonnet",
                "ANTHROPIC_MODEL": "claude-3-haiku",
                "OPENAI_MODEL": "gpt-4o",
            }
        )
        is ModelFamily.CLAUDE
    )
    assert detect_model_family({"LLM_MODEL": "custom-model", "OPENAI_MODEL": "gpt-4o"}) is None
    assert (
        detect_model_family({"ANTHROPIC_MODEL": " ", "OPENAI_MODEL": "gpt-4o"})
        is ModelFamily.OPENAI
    )


def test_detect_model_family_empty_environment_is_unset():
    assert detect_model_family({}) is None
    assert detect_model_family({"LLM_MODEL": " ", "ANTHROPIC_MODEL": ""}) is None


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


@pytest.mark.parametrize("count", [0, 1023, 1024, 1025])
def test_cache_breakpoint_boundaries_and_delegation(monkeypatch, count):
    def count_stub(text, model):
        assert text == "sample" and model == "claude"
        return ModelTokenCount(count, model, ModelFamily.CLAUDE, "test-method", True)

    monkeypatch.setattr(tokenizer, "count_model_tokens", count_stub)
    result = check_cache_breakpoint("sample")
    assert result == CacheBreakpointInfo(
        "claude",
        ModelFamily.CLAUDE,
        count,
        1024,
        count >= 1024,
        max(0, 1024 - count),
        "test-method",
        True,
    )
    with pytest.raises(FrozenInstanceError):
        result.threshold_reached = True


@pytest.mark.parametrize("threshold", [0, -1, True, False, 1.0, "1024", None, [], {}])
def test_cache_breakpoint_invalid_threshold_before_counting(monkeypatch, threshold):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid threshold must not invoke token counting")

    monkeypatch.setattr(tokenizer, "count_model_tokens", forbidden)
    with pytest.raises(ValueError, match="positive integer"):
        check_cache_breakpoint("sample", min_tokens=threshold)


@pytest.mark.parametrize("text", [None, False, 1, b"text", []])
def test_cache_breakpoint_invalid_text(text):
    with pytest.raises(TypeError, match="text must be a string"):
        check_cache_breakpoint(text)


@pytest.mark.parametrize("model", [None, False, 1, [], {}])
def test_cache_breakpoint_invalid_model_type(model):
    with pytest.raises(TypeError, match="model must be a string"):
        check_cache_breakpoint("sample", model)


@pytest.mark.parametrize("model", ["", "unknown", "gpt-something"])
def test_cache_breakpoint_unknown_model(model):
    with pytest.raises(ValueError, match="Unknown model"):
        check_cache_breakpoint("", model)


@pytest.mark.parametrize("model", [item.value for item in ModelFamily])
@pytest.mark.parametrize(
    "text,base_count",
    [
        ("", 0),
        ("abcd", 1),
        ("abcde", 2),
        ("🦙", 1),
        ("漢字", 2),
        ("\udcff", 1),
        ("\ud83e\udd99", 1),
    ],
)
def test_cache_breakpoint_offline_unicode(monkeypatch, model, text, base_count):
    import tiktoken.load
    import tiktoken.registry

    def forbidden(*args, **kwargs):
        pytest.fail("offline comparison must not load vocabulary or access the network")

    monkeypatch.setattr(tokenizer, "_O200K_ENC", None)
    monkeypatch.setattr(tokenizer, "_CL100K_ENC", None)
    monkeypatch.setattr(tiktoken.registry, "ENCODINGS", {})
    monkeypatch.setattr(tokenizer.tiktoken, "get_encoding", forbidden)
    monkeypatch.setattr(tiktoken.load, "read_file", forbidden)
    # For these small counts, each legacy multiplier adds one to nonzero counts.
    expected = base_count + int(base_count > 0 and model in {"claude", "gemini"})
    result = check_cache_breakpoint(text, model, min_tokens=1)
    assert result.token_count == expected
    assert result.model == model and result.family is ModelFamily(model)
    assert result.min_tokens == 1
    assert result.threshold_reached is (expected >= 1)
    assert result.tokens_missing == max(0, 1 - expected)
    assert result.is_estimate is True
    assert "utf8-bytes-div4-ceil" in result.method
    assert check_cache_breakpoint(text, model, min_tokens=1) == result


@pytest.mark.parametrize("model", [item.value for item in ModelFamily])
def test_cache_breakpoint_preserves_count_metadata(model_token_backend, model):
    count = count_model_tokens("hello world", model)
    result = check_cache_breakpoint("hello world", model, min_tokens=count.count)
    assert result.token_count == count.count
    assert result.method == count.method
    assert result.is_estimate is count.is_estimate
    assert result.threshold_reached is True
    assert result.tokens_missing == 0
