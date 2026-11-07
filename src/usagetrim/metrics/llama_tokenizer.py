"""Local plain-text BPE adapter for the April 2024 Llama 3 tokenization rules.

No Meta vocabulary or implementation is bundled. The token boundary definition
is specified by https://github.com/meta-llama/llama3/blob/main/llama/tokenizer.py.
User-supplied Meta artifacts remain subject to the Meta Llama 3 Community License.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import stat
from pathlib import Path

_MAX_FILE_BYTES = 16 * 1024 * 1024
_BASE_TOKENS = 128_000
_PATTERN = (
    r"(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}{1,3}"
    r"| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+"
)


def _read_snapshot(configured_path: str) -> bytes:
    """Read a bounded regular file without passing paths to a download loader."""
    if (
        not configured_path
        or configured_path.startswith(("//", "\\\\"))
        or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", configured_path)
        and not re.match(r"^[a-zA-Z]:[\\/]", configured_path)
    ):
        raise ValueError("A local tokenizer.model path is required")
    path = Path(configured_path).expanduser()
    if path.name != "tokenizer.model":
        raise ValueError("Expected tokenizer.model")
    # O_NONBLOCK prevents a FIFO substituted for the path from hanging on open.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= _MAX_FILE_BYTES:
            raise ValueError("Tokenizer must be a bounded regular file")
        snapshot = stream.read(_MAX_FILE_BYTES + 1)
    if not snapshot or len(snapshot) > _MAX_FILE_BYTES:
        raise ValueError("Tokenizer exceeds the size limit")
    return snapshot


def _parse_ranks(snapshot: bytes) -> dict[bytes, int]:
    """Validate the base64-token/integer-rank format, including byte coverage."""
    ranks: dict[bytes, int] = {}
    seen_ranks: set[int] = set()
    for line in io.BytesIO(snapshot):
        if not line.strip() or len(line) > 8192 or len(ranks) >= _BASE_TOKENS:
            raise ValueError("Invalid vocabulary record")
        fields = line.split()
        if len(fields) != 2 or not fields[1].isdigit() or len(fields[1]) > 6:
            raise ValueError("Invalid vocabulary record")
        token = base64.b64decode(fields[0], validate=True)
        rank = int(fields[1])
        if not token or token in ranks or rank in seen_ranks or not 0 <= rank < _BASE_TOKENS:
            raise ValueError("Invalid vocabulary token or rank")
        ranks[token] = rank
        seen_ranks.add(rank)
    if len(ranks) != _BASE_TOKENS or any(bytes([value]) not in ranks for value in range(256)):
        raise ValueError("Incomplete Llama 3 vocabulary")
    if any(len(token) != 1 for token, rank in ranks.items() if rank < 256):
        raise ValueError("Invalid byte fallback ranks")
    return ranks


class _LocalLlama3Encoding:
    """Plain-text encoding with Meta's long-input splitting limits; no BOS/EOS."""

    def __init__(self, encoding, name: str):
        self._encoding = encoding
        self.name = name

    def encode(self, text: str, *, disallowed_special=()) -> list[int]:
        tokens: list[int] = []
        for offset in range(0, len(text), 400_000):
            section = text[offset : offset + 400_000]
            start, run = 0, 0
            previous_space = None
            for index, character in enumerate(section):
                space = character.isspace()
                run = run + 1 if space == previous_space else 1
                previous_space = space
                if run > 25_000:
                    tokens.extend(
                        self._encoding.encode(section[start:index], disallowed_special=())
                    )
                    start, run = index, 1
            tokens.extend(self._encoding.encode(section[start:], disallowed_special=()))
        return tokens


def local_llama3_encoding(backend):
    """Return a validated local encoding, or None for an unavailable configuration.

    This is deliberately not a Hub/model-name resolver. A fresh operation reads
    one snapshot; its caller pins the resulting encoding for that whole operation.
    """
    configured_path = os.environ.get("USAGETRIM_LLAMA3_TOKENIZER_PATH", "")
    if backend is None or not configured_path:
        return None
    try:
        snapshot = _read_snapshot(configured_path)
        ranks = _parse_ranks(snapshot)
        name = "llama3-2024-04-local-sha256-" + hashlib.sha256(snapshot).hexdigest()
        encoding = backend.Encoding(
            name=name,
            pat_str=_PATTERN,
            mergeable_ranks=ranks,
            special_tokens={},
        )
        return _LocalLlama3Encoding(encoding, name)
    except (OSError, ValueError, OverflowError, RuntimeError):
        return None
