"""Private, transient semantic retrieval. Never download models or persist vectors."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol


class Embedder(Protocol):
    def can_embed(self, text: str) -> bool: ...

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


class SemanticUnavailable(RuntimeError):
    """An expected failure of the optional local inference runtime."""


class _FastEmbed:
    def __init__(self, model, tokenizer, max_tokens, errors):
        self.model, self.tokenizer, self.errors = model, tokenizer, errors
        self.max_tokens = max_tokens

    def can_embed(self, text):
        # Count with the backend's actual special tokens, but without truncation
        # or padding. Respect its configured limit, not just BGE's 512-token cap.
        return len(self.tokenizer.encode(text).ids) <= self.max_tokens

    def embed(self, texts):
        try:
            return [vector.tolist() for vector in self.model.embed(texts, batch_size=16)]
        except self.errors as exc:
            raise SemanticUnavailable("local embedding runtime unavailable") from exc


def load_embedder() -> Embedder | None:
    """Load only an explicitly provisioned local BGE model; no implicit cache lookup."""
    configured = os.environ.get("USAGETRIM_SEMANTIC_MODEL_PATH")
    if not configured:
        return None
    try:
        path = Path(configured)
        if not path.is_dir():
            return None
        # Reject incomplete/invalid local assets before initializing the backend.
        config = json.loads((path / "config.json").read_text(encoding="utf-8"))
        tokenizer_data = json.loads((path / "tokenizer.json").read_text(encoding="utf-8"))
        tokenizer_config = json.loads((path / "tokenizer_config.json").read_text(encoding="utf-8"))
        special_tokens = json.loads((path / "special_tokens_map.json").read_text(encoding="utf-8"))
        if not all(
            isinstance(value, dict)
            for value in (config, tokenizer_data, tokenizer_config, special_tokens)
        ):
            return None
        limits = [
            tokenizer_config[k] for k in ("model_max_length", "max_length") if k in tokenizer_config
        ]
        if not limits or any(type(limit) is not int or limit < 1 for limit in limits):
            return None
        if not isinstance(tokenizer_config.get("pad_token"), str):
            return None
        pad_id = config.get("pad_token_id", 0)
        if type(pad_id) is not int or not 0 <= pad_id < 2**32:
            return None
        for token in special_tokens.values():
            if isinstance(token, str):
                continue
            if not isinstance(token, dict) or not isinstance(token.get("content"), str):
                return None
            flags = {"single_word", "lstrip", "rstrip", "normalized", "special"}
            if token.keys() - (flags | {"content"}) or any(
                type(value) is not bool for key, value in token.items() if key in flags
            ):
                return None
        if not any(path.glob("*.onnx")):
            return None
        # Recent ORT wheels enable native telemetry at initialization. The API
        # alone is too late to suppress startup events, so set this before import.
        os.environ["ORT_DISABLE_TELEMETRY"] = "1"
        import onnxruntime

        onnxruntime.disable_telemetry_events()
        from fastembed import TextEmbedding
        from onnxruntime.capi import onnxruntime_pybind11_state as ort_errors
        from tokenizers import Tokenizer
    except (ImportError, OSError, ValueError, RuntimeError):
        return None
    errors = (OSError, ValueError, RuntimeError) + tuple(
        getattr(ort_errors, name)
        for name in (
            "Fail",
            "InvalidArgument",
            "InvalidGraph",
            "InvalidProtobuf",
            "NoSuchFile",
            "NotImplemented",
            "RuntimeException",
            "EPFail",
        )
        if hasattr(ort_errors, name)
    )
    # tokenizers' Rust file parser reports corrupt assets as an *exact* builtin
    # Exception, without a dedicated exception class. Limit this accommodation
    # to file parsing; derived/programmer exceptions still propagate.
    try:
        Tokenizer.from_file(str(path / "tokenizer.json"))
    except Exception as exc:
        if type(exc) is Exception:
            return None
        raise
    try:
        model = TextEmbedding(
            model_name="BAAI/bge-small-en-v1.5",
            specific_model_path=str(path.absolute()),
            local_files_only=True,
            cache_dir=str(path.absolute()),
            providers=["CPUExecutionProvider"],
            threads=1,
        )
        actual_tokenizer = model.model.tokenizer
        truncation = actual_tokenizer.truncation
        if not truncation or type(truncation.get("max_length")) is not int:
            return None
        max_tokens = min(512, truncation["max_length"])
        if max_tokens < 1:
            return None
        tokenizer = Tokenizer.from_str(actual_tokenizer.to_str())
        tokenizer.no_truncation()
        tokenizer.no_padding()
    except errors:
        return None
    return _FastEmbed(model, tokenizer, max_tokens, errors)


def _chunks(source, embedder):
    """Ten-line windows, split only between lines; never truncate a single line."""
    lines = source.splitlines()
    chunks, skipped = [], []
    for window in range(0, len(lines), 10):
        start, stop = window, min(window + 10, len(lines))
        while start < stop:
            end = stop
            while end > start and not embedder.can_embed("\n".join(lines[start:end])):
                end -= 1
            if end == start:
                skipped.append((start + 1, start + 1))
                start += 1
            else:
                chunks.append((start + 1, end, "\n".join(lines[start:end])))
                start = end
    return chunks, skipped


def rank_chunks(source: str, query: str, embedder: Embedder | None = None):
    """Return cosine-ranked candidates and skipped ranges, all within one snapshot."""
    embedder = embedder if embedder is not None else load_embedder()
    if embedder is None:
        return [], []
    try:
        if not embedder.can_embed(query):
            return [], []
        chunks, skipped = _chunks(source, embedder)
        if not chunks:
            return [], skipped
        vectors = list(embedder.embed([query, *(body for _, _, body in chunks)]))
    except SemanticUnavailable:
        return [], []
    if len(vectors) != len(chunks) + 1 or not vectors[0]:
        return [], []
    dimension = len(vectors[0])
    if any(
        len(vector) != dimension or not all(math.isfinite(value) for value in vector)
        for vector in vectors
    ):
        return [], []
    norms = [math.hypot(*vector) for vector in vectors]
    if any(not norm or not math.isfinite(norm) for norm in norms):
        return [], []
    scores = [
        math.fsum((a / norms[0]) * (b / norm) for a, b in zip(vectors[0], vector))
        for vector, norm in zip(vectors[1:], norms[1:])
    ]
    return [
        chunk for _, chunk in sorted(zip(scores, chunks), key=lambda item: (-item[0], item[1][0]))
    ], skipped
