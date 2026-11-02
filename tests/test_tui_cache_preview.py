"""Bounded CCR preview regression cases; no UI dependency required."""

import sqlite3
import zlib

import pytest

from usagetrim.core.cache import ContextCache
from usagetrim.tui.cache_preview import LIMIT, STORED_LIMIT, read_preview


def payload_cache(tmp_path, payload):
    cache = ContextCache(tmp_path / "cache.db")
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute("INSERT INTO output_cache(ref_id,content) VALUES (?,?)", ("tc_test", payload))
    return cache


@pytest.mark.parametrize("prefix", [b"ZLIB", b""])
def test_preview_zlib_and_legacy(tmp_path, prefix):
    cache = payload_cache(tmp_path, prefix + zlib.compress(b"diagnostic"))
    assert "diagnostic" in read_preview(cache, "tc_test")


def test_preview_redacts_before_line_limit(tmp_path):
    secret = "ghp_" + "A" * 36
    cache = payload_cache(tmp_path, secret + "\n" + "line\n" * 150)
    output = read_preview(cache, "tc_test")
    assert secret not in output and "REDACTED" in output
    assert len(output.splitlines()) == 101


def test_preview_rejects_compression_bomb_without_materializing_it(tmp_path):
    cache = payload_cache(tmp_path, b"ZLIB" + zlib.compress(b"X" * (LIMIT * 1000)))
    assert "unavailable" in read_preview(cache, "tc_test")


def test_preview_rejects_oversized_storage_before_payload_read(tmp_path, monkeypatch):
    cache = payload_cache(tmp_path, "X" * (STORED_LIMIT + 1))
    assert "stored payload exceeds" in read_preview(cache, "tc_test")


def test_preview_missing_and_damaged(tmp_path):
    cache = payload_cache(tmp_path, b"ZLIBbroken")
    assert "damaged" in read_preview(cache, "tc_test")
    assert "unavailable" in read_preview(cache, "missing")


def test_preview_optional_zstd(tmp_path):
    zstd = pytest.importorskip("zstandard")
    cache = payload_cache(tmp_path, b"ZSTD" + zstd.ZstdCompressor().compress(b"diagnostic"))
    assert "diagnostic" in read_preview(cache, "tc_test")
