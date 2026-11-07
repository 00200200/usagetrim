"""Synthetic local BPE tests, not certification against Meta's real vocabulary."""

import base64
import hashlib
import socket

import pytest

from usagetrim.metrics import llama_tokenizer, tokenizer


@pytest.fixture
def offline_guard(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Network or vocabulary download attempted")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    if tokenizer.tiktoken is not None:
        import tiktoken.load

        monkeypatch.setattr(tiktoken.load, "read_file", forbidden)
        monkeypatch.setattr(tokenizer.tiktoken, "get_encoding", forbidden)


@pytest.fixture
def synthetic_vocabulary(monkeypatch, tmp_path, offline_guard):
    if tokenizer.tiktoken is None:
        pytest.skip("Real BPE execution requires the project's tiktoken dependency")
    # Complete byte fallback plus a few meaningful merges; no Meta data.
    pieces = [bytes([value]) for value in range(256)] + [b"he", b"hel", b"hell", b"hello"]
    snapshot = b"".join(
        base64.b64encode(piece) + b" " + str(rank).encode() + b"\n"
        for rank, piece in enumerate(pieces)
    )
    path = tmp_path / "tokenizer.model"
    path.write_bytes(snapshot)
    # Production requires 128,000 ranks. Only synthetic tests relax that check.
    monkeypatch.setattr(llama_tokenizer, "_BASE_TOKENS", len(pieces))
    monkeypatch.setenv("USAGETRIM_LLAMA3_TOKENIZER_PATH", str(path))
    return path


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", 0),
        ("hello", 1),
        ("def f():\n    return 42\n", 23),
        ('{"ok":true}', 11),
        (" \t\r\n", 4),
        ("🦙漢字", 10),
        ("<|begin_of_text|>", 17),
    ],
)
def test_local_llama_counts_plain_text_without_chat_tokens(synthetic_vocabulary, text, expected):
    result = tokenizer.count_model_tokens(text, "llama")
    assert result.count == expected
    assert result.family is tokenizer.ModelFamily.LLAMA
    assert not result.is_estimate
    digest = hashlib.sha256(synthetic_vocabulary.read_bytes()).hexdigest()
    assert result.method == "llama3-2024-04-local-sha256-" + digest


def test_local_llama_without_configuration_uses_existing_fallback(monkeypatch, offline_guard):
    monkeypatch.delenv("USAGETRIM_LLAMA3_TOKENIZER_PATH", raising=False)
    result = tokenizer.count_model_tokens("🦙漢字", "llama")
    assert result.count == 3
    assert result.is_estimate
    assert result.method == "utf8-bytes-div4-ceil"


def test_local_llama_without_tiktoken_does_not_open_configuration(monkeypatch, offline_guard):
    monkeypatch.setenv("USAGETRIM_LLAMA3_TOKENIZER_PATH", "/configured/tokenizer.model")
    monkeypatch.setattr(tokenizer, "tiktoken", None)

    def forbidden(*args):
        pytest.fail("Absent backend must not read configuration")

    monkeypatch.setattr(llama_tokenizer, "_read_snapshot", forbidden)
    assert tokenizer.count_model_tokens("hello", "llama").is_estimate


@pytest.mark.parametrize(
    "configuration",
    [
        "https://example.invalid/tokenizer.model",
        "file:///tmp/tokenizer.model",
        "//host/tokenizer.model",
    ],
)
def test_local_llama_refuses_remote_paths(configuration, monkeypatch, offline_guard):
    monkeypatch.setenv("USAGETRIM_LLAMA3_TOKENIZER_PATH", configuration)
    result = tokenizer.count_model_tokens("hello", "llama")
    assert result.is_estimate and result.method == "utf8-bytes-div4-ceil"


@pytest.mark.parametrize(
    "payload",
    [
        b"not-a-vocabulary",
        b"!!!! 0\n",
        b"YQ== 0\nYQ== 1\n",
        b"YQ== 0\nYg== 0\n",
        b"YQ== 128000\n",
        b"YQ== -1\n",
        b"YQ== 0\n\n",
        b"",
    ],
)
def test_invalid_local_llama_vocabulary_falls_back(payload, tmp_path, monkeypatch, offline_guard):
    path = tmp_path / "tokenizer.model"
    path.write_bytes(payload)
    monkeypatch.setenv("USAGETRIM_LLAMA3_TOKENIZER_PATH", str(path))
    assert tokenizer.count_model_tokens("hello", "llama").is_estimate


def test_local_llama_rejects_missing_directory_and_oversized_file(
    tmp_path, monkeypatch, offline_guard
):
    path = tmp_path / "tokenizer.model"
    monkeypatch.setenv("USAGETRIM_LLAMA3_TOKENIZER_PATH", str(path))
    assert tokenizer.count_model_tokens("hello", "llama").is_estimate
    path.mkdir()
    assert tokenizer.count_model_tokens("hello", "llama").is_estimate
    path.rmdir()
    path.write_bytes(b"x" * 65)
    monkeypatch.setattr(llama_tokenizer, "_MAX_FILE_BYTES", 64)
    assert tokenizer.count_model_tokens("hello", "llama").is_estimate


def test_local_llama_rejects_incomplete_base_ranks(synthetic_vocabulary, monkeypatch):
    # A syntactically valid byte-only vocabulary is not the production artifact.
    monkeypatch.setattr(llama_tokenizer, "_BASE_TOKENS", 128_000)
    assert tokenizer.count_model_tokens("hello", "llama").is_estimate


@pytest.mark.parametrize("damage", ["missing-byte", "non-byte-base-rank", "duplicate-rank"])
def test_local_llama_rejects_structurally_incompatible_ranks(synthetic_vocabulary, damage):
    lines = synthetic_vocabulary.read_bytes().splitlines()
    if damage == "missing-byte":
        lines[0] = base64.b64encode(b"unavailable-byte") + b" 0"
    elif damage == "non-byte-base-rank":
        # Preserve all tokens and contiguous ranks, but violate byte fallback order.
        lines[0] = base64.b64encode(b"he") + b" 0"
        lines[256] = base64.b64encode(b"\x00") + b" 256"
    else:
        lines[-1] = base64.b64encode(b"hello") + b" 258"
    synthetic_vocabulary.write_bytes(b"\n".join(lines) + b"\n")
    assert tokenizer.count_model_tokens("hello", "llama").is_estimate


def test_local_llama_long_input_splitting_and_surrogates(synthetic_vocabulary):
    assert tokenizer.count_model_tokens(" " * 25_001, "llama").count == 25_001
    assert tokenizer.count_model_tokens("x" * 400_001, "llama").count == 400_001
    assert tokenizer.count_model_tokens("\udcff", "llama").count == 3
    assert tokenizer.count_model_tokens("\ud83e\udd99", "llama").count == 4


def test_local_llama_optimizer_pins_encoding_and_obeys_budget(synthetic_vocabulary, monkeypatch):
    from usagetrim.core import optimizer
    from usagetrim.core.cache import ContextCache

    encoding = tokenizer._local_llama_encoding()
    original_store = ContextCache.store
    reads = []
    read_snapshot = llama_tokenizer._read_snapshot

    def record_snapshot(path):
        reads.append(path)
        return read_snapshot(path)

    def store_then_remove_configuration(self, *args, **kwargs):
        ref_id = original_store(self, *args, **kwargs)
        synthetic_vocabulary.unlink(missing_ok=True)
        return ref_id

    monkeypatch.setattr(llama_tokenizer, "_read_snapshot", record_snapshot)
    monkeypatch.setattr(ContextCache, "store", store_then_remove_configuration)
    raw = "hello diagnostic 漢字🙂 " * 100
    result = optimizer.optimize_context(raw, budget=128, model="llama")
    assert len(reads) == 1
    assert result.method == encoding.name
    assert not result.is_estimate
    assert result.original_tokens == len(encoding.encode(raw))
    assert result.optimized_tokens == len(encoding.encode(result.text)) <= 128
    assert result.saved_tokens == result.original_tokens - result.optimized_tokens
    assert ContextCache().retrieve(result.ref_id) == raw
    # A later, independent operation may use the fallback; this one never did.
    assert tokenizer.count_model_tokens("hello", "llama").is_estimate


def test_local_llama_does_not_switch_from_fallback_mid_operation(synthetic_vocabulary, monkeypatch):
    path = str(synthetic_vocabulary)
    monkeypatch.delenv("USAGETRIM_LLAMA3_TOKENIZER_PATH")

    @tokenizer.model_counting
    def operation(*, model=None):
        before = tokenizer.count_model_tokens("hello", model)
        monkeypatch.setenv("USAGETRIM_LLAMA3_TOKENIZER_PATH", path)
        after = tokenizer.count_model_tokens("hello", model)
        assert before == after
        assert before.is_estimate

    operation(model="llama")
    assert not tokenizer.count_model_tokens("hello", "llama").is_estimate


def test_local_llama_configuration_does_not_affect_legacy_operations(
    synthetic_vocabulary, monkeypatch
):
    def forbidden():
        pytest.fail("Legacy operation must not load a Llama tokenizer")

    monkeypatch.setattr(tokenizer, "_local_llama_encoding", forbidden)

    @tokenizer.model_counting
    def operation(*, model=None):
        return model

    assert operation() is None
