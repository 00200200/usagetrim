import builtins
import json
import socket
import sqlite3
import sys
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from usagetrim.cli import app
from usagetrim.core import semantic_search as semantic
from usagetrim.core.cache import ContextCache
from usagetrim.metrics.tokenizer import count_tokens


class FakeEmbedder:
    def __init__(self, max_chars=10000):
        self.max_chars = max_chars
        self.calls = []

    def can_embed(self, text):
        return len(text) <= self.max_chars

    def embed(self, texts):
        self.calls.append(list(texts))
        return [
            [1.0, 0.0, 0.0]
            if "auth failure" in text or "401 Unauthorized" in text
            else [0.0, 1.0, 0.0]
            if "database connection failure" in text or "psycopg2.OperationalError" in text
            else [0.0, 0.0, 1.0]
            for text in texts
        ]


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("semantic retrieval attempted network access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def test_lexical_output_and_no_semantic_import(monkeypatch):
    cache = ContextCache()
    ref = cache.store("timeout\n" + "ordinary\n" * 35)
    original_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.endswith("semantic_search") or name.startswith("fastembed"):
            pytest.fail("lexical search imported semantic backend")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    expected = f"# {ref} lines 1-30\ntimeout\n" + "\n".join(["ordinary"] * 29)
    assert cache.search(ref, "timeout") == expected
    assert cache.hybrid_search(ref, "timeout") == expected


@pytest.mark.parametrize(
    "query,diagnostic",
    [
        ("auth failure", "HTTP 401 Unauthorized: JWT claims expired"),
        (
            "database connection failure",
            'psycopg2.OperationalError: could not translate host name "db" to address',
        ),
    ],
)
def test_paraphrase_zero_lexical_hits(query, diagnostic):
    cache = ContextCache()
    source = "\n".join(["ordinary"] * 10 + [diagnostic, "żółć 日本語"])
    ref = cache.store(source)
    assert cache.search(ref, query).startswith("No matches")
    embedder = FakeEmbedder()
    output = cache.hybrid_search(ref, query, limit=1, embedder=embedder)
    assert f"# {ref} lines 11-12 [semantic candidate]\n{diagnostic}\nżółć 日本語" in output
    assert "confidence" not in output
    assert len(embedder.calls) == 1


def test_partial_or_triggers_semantic_without_other_refs_or_persistence(tmp_path):
    cache = ContextCache(tmp_path / "cache.db")
    source = "\n".join(
        ["failure in unrelated backup"] + ["ordinary"] * 29 + ["HTTP 401 Unauthorized"]
    )
    ref = cache.store(source)
    cache.store("PRIVATE_OTHER_REF\nHTTP 401 Unauthorized")
    assert "401" not in cache.search(ref, "auth failure")
    embedder = FakeEmbedder()
    before = inquiry_count(cache, ref)
    output = cache.hybrid_search(ref, "auth failure", limit=1, embedder=embedder)
    assert "lines 31-31" in output and "401 Unauthorized" in output
    assert inquiry_count(cache, ref) == before + 1
    assert "PRIVATE_OTHER_REF" not in str(embedder.calls)
    with sqlite3.connect(cache.db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {
        "output_cache",
        "recovery_indexed",
        "recovery_search",
        "recovery_search_data",
        "recovery_search_idx",
        "recovery_search_content",
        "recovery_search_docsize",
        "recovery_search_config",
    }
    assert b"auth failure" not in cache.db_path.read_bytes()
    assert not (tmp_path / "telemetry.db").exists()


def inquiry_count(cache, ref):
    with sqlite3.connect(cache.db_path) as conn:
        return conn.execute("SELECT hit_count FROM output_cache WHERE ref_id=?", (ref,)).fetchone()[
            0
        ]


def test_full_lexical_coverage_does_not_load_backend(monkeypatch):
    cache = ContextCache()
    ref = cache.store("authentication failure\n" + "ordinary\n" * 40)
    monkeypatch.setattr(semantic, "load_embedder", lambda: pytest.fail("backend loaded"))
    expected = cache.search(ref, "authentication failure")
    before = inquiry_count(cache, ref)
    assert cache.hybrid_search(ref, "authentication failure") == expected
    assert inquiry_count(cache, ref) == before + 1


@pytest.mark.parametrize("reason", ["unset", "missing", "invalid", "dependency"])
def test_unavailable_backend_falls_back_without_network(tmp_path, monkeypatch, reason):
    monkeypatch.delenv("USAGETRIM_SEMANTIC_MODEL_PATH", raising=False)
    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "0")
    monkeypatch.setitem(
        sys.modules, "onnxruntime", SimpleNamespace(disable_telemetry_events=lambda: None)
    )
    if reason != "unset":
        path = tmp_path / "model"
        monkeypatch.setenv("USAGETRIM_SEMANTIC_MODEL_PATH", str(path))
        if reason in {"invalid", "dependency"}:
            path.mkdir()
            (path / "config.json").write_text("{}")
            (path / "tokenizer.json").write_text("{}" if reason == "dependency" else "invalid")
            (path / "tokenizer_config.json").write_text(
                '{"model_max_length": 512, "pad_token": "[PAD]"}'
            )
            (path / "special_tokens_map.json").write_text("{}")
            (path / "model.onnx").write_bytes(b"not a model")
    original_import = builtins.__import__
    imports = []

    def guarded(name, *args, **kwargs):
        if name == "fastembed":
            imports.append(name)
            raise ImportError("optional dependency omitted")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    cache = ContextCache()
    ref = cache.store("failure in backup\nHTTP 401 Unauthorized")
    expected = cache.search(ref, "auth failure")
    assert cache.hybrid_search(ref, "auth failure") == expected
    assert bool(imports) == (reason == "dependency")


@pytest.mark.parametrize("failure", [None, "parser", "loader", "programmer"])
def test_backend_local_only_and_untruncated_length_check(tmp_path, monkeypatch, failure):
    model_path = tmp_path / "local model"
    model_path.mkdir()
    monkeypatch.setenv("USAGETRIM_SEMANTIC_MODEL_PATH", str(model_path))
    for name, value in {
        "config.json": {},
        "tokenizer.json": {},
        "special_tokens_map.json": {},
        "tokenizer_config.json": {"model_max_length": 8, "pad_token": "[PAD]"},
    }.items():
        (model_path / name).write_text(json.dumps(value))
    (model_path / "model.onnx").write_bytes(b"local test asset")
    settings = []
    telemetry_disabled = []
    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "0")
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "onnxruntime":
            assert semantic.os.environ["ORT_DISABLE_TELEMETRY"] == "1"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    def disable_telemetry():
        assert semantic.os.environ["ORT_DISABLE_TELEMETRY"] == "1"
        telemetry_disabled.append(True)

    class Tokenizer:
        truncation = {"max_length": 8}
        padding = True

        @classmethod
        def from_file(cls, path):
            if failure == "parser":
                raise Exception("invalid tokenizer data")
            if failure == "programmer":
                raise AssertionError("programmer bug")
            return cls()

        @classmethod
        def from_str(cls, text):
            assert text == "actual backend tokenizer including added tokens"
            return cls()

        def to_str(self):
            return "actual backend tokenizer including added tokens"

        def no_truncation(self):
            self.truncation = None

        def no_padding(self):
            self.padding = False

        def encode(self, text):
            assert self.truncation is None and not self.padding
            return SimpleNamespace(ids=list(range(len(text) + 2)))

    def construct(**kwargs):
        assert telemetry_disabled == [True]
        settings.append(kwargs)
        if failure == "loader":
            raise ValueError("invalid local model")
        return SimpleNamespace(model=SimpleNamespace(tokenizer=Tokenizer()))

    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=construct))
    monkeypatch.setitem(sys.modules, "tokenizers", SimpleNamespace(Tokenizer=Tokenizer))
    monkeypatch.setitem(
        sys.modules, "onnxruntime", SimpleNamespace(disable_telemetry_events=disable_telemetry)
    )
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime.capi",
        SimpleNamespace(onnxruntime_pybind11_state=SimpleNamespace()),
    )
    if failure == "programmer":
        with pytest.raises(AssertionError, match="programmer bug"):
            semantic.load_embedder()
        return
    embedder = semantic.load_embedder()
    if failure:
        assert embedder is None
        return
    assert settings == [
        {
            "model_name": "BAAI/bge-small-en-v1.5",
            "specific_model_path": str(model_path),
            "local_files_only": True,
            "cache_dir": str(model_path),
            "providers": ["CPUExecutionProvider"],
            "threads": 1,
        }
    ]
    assert embedder.can_embed("abcdef")
    assert not embedder.can_embed("abcdefg")


@pytest.mark.parametrize(
    "config,tokenizer_config,special",
    [
        ({"pad_token_id": []}, {"model_max_length": 512, "pad_token": "[PAD]"}, {}),
        ({}, {"model_max_length": "512", "pad_token": "[PAD]"}, {}),
        ({}, {"model_max_length": 512}, {}),
        ({}, {"model_max_length": 512, "pad_token": "[PAD]"}, {"cls_token": {"content": 123}}),
    ],
)
def test_malformed_model_metadata_is_unavailable(
    tmp_path, monkeypatch, config, tokenizer_config, special
):
    monkeypatch.setenv("USAGETRIM_SEMANTIC_MODEL_PATH", str(tmp_path))
    for name, value in {
        "config.json": config,
        "tokenizer.json": {},
        "tokenizer_config.json": tokenizer_config,
        "special_tokens_map.json": special,
    }.items():
        (tmp_path / name).write_text(json.dumps(value))
    (tmp_path / "model.onnx").write_bytes(b"not a model")
    assert semantic.load_embedder() is None


def test_runtime_unavailability_falls_back_but_programmer_errors_are_visible():
    cache = ContextCache()
    ref = cache.store("HTTP 401 Unauthorized")

    class Broken(FakeEmbedder):
        def embed(self, texts):
            raise semantic.SemanticUnavailable("runtime unavailable")

    assert cache.hybrid_search(ref, "auth failure", embedder=Broken()).startswith("No matches")

    class Bug(FakeEmbedder):
        def embed(self, texts):
            raise AssertionError("programmer bug")

    with pytest.raises(AssertionError, match="programmer bug"):
        cache.hybrid_search(ref, "auth failure", embedder=Bug())


@pytest.mark.parametrize("provider", ["openai", "claude", "gemini"])
@pytest.mark.parametrize("budget", [64, 100, 180, 400])
def test_complete_render_budget_and_truthful_omission(provider, budget):
    cache = ContextCache()
    ref = cache.store("\n".join(["HTTP 401 Unauthorized " + "detail " * 15] * 23))
    output = cache.hybrid_search(
        ref, "auth failure", budget, token_provider=provider, embedder=FakeEmbedder()
    )
    assert getattr(count_tokens(output + "\n"), provider) <= budget
    assert "Omitted" in output and f"retrieve {ref} --lines" in output


def test_oversized_chunk_splits_only_at_line_boundaries():
    cache = ContextCache()
    ref = cache.store("X" * 200 + "\nHTTP 401 Unauthorized\n" + "ordinary\n" * 9)
    embedder = FakeEmbedder(max_chars=50)
    output = cache.hybrid_search(ref, "auth failure", embedder=embedder)
    assert "401 Unauthorized" in output
    assert "--lines 1-1" in output
    for text in embedder.calls[0]:
        assert len(text) <= 50
    for first, last, body in semantic._chunks(cache.retrieve(ref), embedder)[0]:
        assert body == "\n".join(cache.retrieve(ref).splitlines()[first - 1 : last])


def test_ten_eleven_line_boundary_and_deterministic_ties():
    cache = ContextCache()
    ref = cache.store("\n".join(["HTTP 401 Unauthorized"] * 11))
    expected = cache.hybrid_search(ref, "auth failure", embedder=FakeEmbedder())
    assert "lines 1-10" in expected and "lines 11-11" in expected
    assert expected.index("lines 1-10") < expected.index("lines 11-11")
    assert cache.hybrid_search(ref, "auth failure", embedder=FakeEmbedder()) == expected


def test_redaction_before_embedding_including_legacy_and_query():
    cache = ContextCache()
    secret = "ghp_" + "A" * 36
    ref = cache.store("HTTP 401 Unauthorized")
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute(
            "UPDATE output_cache SET content=? WHERE ref_id=?",
            (f"HTTP 401 Unauthorized {secret}", ref),
        )
    embedder = FakeEmbedder()
    output = cache.hybrid_search(ref, "auth failure " + secret, embedder=embedder)
    assert secret not in output and secret not in str(embedder.calls)
    assert "REDACTED_GITHUB_TOKEN" in output


@pytest.mark.parametrize(
    "vectors",
    [
        [],
        [[], []],
        [[0.0], [0.0]],
        [[1.0], [float("nan")]],
        [[1.0], [float("inf")]],
        [[1.0], [1.0, 2.0]],
    ],
)
def test_invalid_vectors_fall_back(vectors):
    class Invalid(FakeEmbedder):
        def embed(self, texts):
            return vectors

    cache = ContextCache()
    ref = cache.store("HTTP 401 Unauthorized")
    assert cache.hybrid_search(ref, "auth failure", embedder=Invalid()).startswith("No matches")


@pytest.mark.parametrize(
    "kwargs",
    [{"budget": 0}, {"budget": True}, {"limit": 0}, {"limit": True}, {"token_provider": "remote"}],
)
def test_invalid_arguments(kwargs):
    with pytest.raises(ValueError):
        ContextCache().hybrid_search("tc_missing", "auth failure", **kwargs)


def test_tiny_budget_and_missing_ref_do_not_load_model(monkeypatch):
    monkeypatch.setattr(semantic, "load_embedder", lambda: pytest.fail("loaded model"))
    cache = ContextCache()
    assert "not found" in cache.hybrid_search("tc_missing", "auth failure")
    with pytest.raises(ValueError, match="budget"):
        cache.hybrid_search("tc_missing", "auth failure", budget=1)


@pytest.mark.parametrize(
    "args",
    [
        ["--semantic"],
        ["--semantic", "--lines", "1"],
        ["--semantic", "--query", "auth", "--lines", "1"],
        ["--budget", "100"],
        ["--semantic", "--query", "auth", "--budget", "0"],
    ],
)
def test_cli_semantic_validation(args):
    result = CliRunner().invoke(app, ["retrieve", "tc_missing", *args])
    assert result.exit_code == 2


def test_cli_and_mcp_opt_in_and_legacy_compatibility(monkeypatch):
    from usagetrim.mcp import server

    cache = ContextCache()
    ref = cache.store("HTTP 401 Unauthorized: JWT claims expired")
    runner = CliRunner()
    monkeypatch.setattr(
        semantic, "load_embedder", lambda: pytest.fail("semantic off loaded backend")
    )
    expected = cache.search(ref, "auth failure")
    assert (
        runner.invoke(app, ["retrieve", ref, "--query", "auth failure"]).stdout == expected + "\n"
    )
    assert (
        server.handle_usagetrim_retrieve(
            {"ref_id": ref, "query": "auth failure", "semantic": False}
        )
        == expected
    )
    monkeypatch.setattr(semantic, "load_embedder", FakeEmbedder)
    result = runner.invoke(
        app, ["retrieve", ref, "--query", "auth failure", "--semantic", "--budget", "128"]
    )
    assert result.exit_code == 0, result.output
    assert "401 Unauthorized" in result.stdout
    assert count_tokens(result.stdout).openai <= 128
    result = server.handle_usagetrim_retrieve(
        {"ref_id": ref, "query": "auth failure", "semantic": True, "max_tokens": 128}
    )
    assert "401 Unauthorized" in result and count_tokens(result).claude <= 128
    tool = next(t for t in server.tool_definitions() if t["name"] == "usagetrim_retrieve")
    assert tool["inputSchema"]["properties"]["semantic"] == {
        "type": "boolean",
        "default": False,
        "description": "Allow offline semantic fallback for query; cannot combine with lines.",
    }


@pytest.mark.parametrize(
    "arguments", [{"semantic": True}, {"semantic": "true"}, {"semantic": True, "lines": "1"}]
)
def test_mcp_semantic_validation(arguments):
    from usagetrim.mcp.server import handle_usagetrim_retrieve

    with pytest.raises(ValueError):
        handle_usagetrim_retrieve({"ref_id": "tc_missing", **arguments})
