"""Tests for ContextCache zstd/zlib dictionary compression and retrieval."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from usagetrim.core.cache import (
    COMPRESSION_THRESHOLD,
    ContextCache,
    compress_payload,
    decompress_payload,
)


def test_compress_payload_threshold():
    small = "a" * 500
    assert compress_payload(small) == small

    large = "b" * 1000
    compressed = compress_payload(large)
    assert isinstance(compressed, bytes)
    assert compressed.startswith((b"ZSTD", b"ZLIB"))
    assert decompress_payload(compressed) == large


def test_roundtrip_byte_for_byte_identical(tmp_path: Path):
    db_path = tmp_path / "cache.db"
    cache = ContextCache(db_path=db_path)

    sample_log = "\n".join(
        [
            f"2026-10-06 23:45:{i:02d} INFO [compiler.cc:{i}] Building target //module/service:{i}"
            for i in range(100)
        ]
    )
    assert len(sample_log.encode("utf-8")) > COMPRESSION_THRESHOLD

    ref_id = cache.store(sample_log, source="build")
    retrieved = cache.retrieve(ref_id)
    assert retrieved == sample_log

    # Check slice
    sliced = cache.retrieve(ref_id, lines_range="5-10")
    expected_lines = sample_log.splitlines()[4:10]
    for line in expected_lines:
        assert line in sliced


def test_search_compressed_payload(tmp_path: Path):
    db_path = tmp_path / "cache.db"
    cache = ContextCache(db_path=db_path)

    content = "\n".join(
        [
            f"Line {i}: Normal execution log frame"
            if i != 42
            else "Line 42: CRITICAL_FAILURE_KERNEL_PANIC"
            for i in range(100)
        ]
    )
    ref_id = cache.store(content, source="kernel")
    search_res = cache.search(ref_id, "CRITICAL_FAILURE_KERNEL_PANIC")
    assert "Line 42" in search_res
    assert "CRITICAL_FAILURE_KERNEL_PANIC" in search_res


def test_legacy_uncompressed_row_backward_compat(tmp_path: Path):
    db_path = tmp_path / "cache.db"
    cache = ContextCache(db_path=db_path)

    raw_text = "Legacy uncompressed log data from older usagetrim version"
    ref_id = "tc_legacy_123"

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO output_cache (ref_id, content_hash, source, content, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ref_id, "hash123", "exec", raw_text, 12345.0),
        )
        conn.commit()

    retrieved = cache.retrieve(ref_id)
    assert retrieved == raw_text


def test_zlib_fallback_explicit(monkeypatch):
    import usagetrim.core.cache as cache_mod

    # Force zstd = None to exercise zlib path
    monkeypatch.setattr(cache_mod, "zstd", None)

    large_text = "ERROR: segmentation fault occurred at address 0xdeadbeef\n" * 50
    compressed = cache_mod.compress_payload(large_text)
    assert isinstance(compressed, bytes)
    assert compressed.startswith(b"ZLIB")
    assert cache_mod.decompress_payload(compressed) == large_text


def test_database_footprint_shrinkage(tmp_path: Path):
    db_compressed = tmp_path / "compressed.db"
    db_raw = tmp_path / "raw.db"

    cache_comp = ContextCache(db_path=db_compressed)

    # Setup uncompressed raw DB
    with sqlite3.connect(db_raw) as conn:
        conn.execute(
            """
            CREATE TABLE output_cache (
                ref_id TEXT PRIMARY KEY,
                content_hash TEXT,
                source TEXT,
                content TEXT,
                created_at REAL
            )
            """
        )

    log_entry = (
        "CMake Error at /usr/local/share/cmake/Modules/FindPackageHandleStandardArgs.cmake:230:\n"
        "  Could NOT find OpenSSL, try to set the path to OpenSSL root folder in the\n"
        "  system variable OPENSSL_ROOT_DIR (missing: OPENSSL_CRYPTO_LIBRARY OPENSSL_INCLUDE_DIR)\n"
        "Call Stack (most recent call first):\n"
        "  CMakeLists.txt:45 (find_package)\n"
    ) * 10

    for i in range(50):
        entry = f"Run {i}:\n{log_entry}"
        cache_comp.store(entry, namespace=f"run_{i}")

        with sqlite3.connect(db_raw) as conn:
            conn.execute(
                "INSERT INTO output_cache VALUES (?, ?, ?, ?, ?)",
                (f"raw_{i}", f"hash_{i}", "exec", entry, 1.0),
            )
            conn.commit()

    raw_size = db_raw.stat().st_size
    comp_size = db_compressed.stat().st_size

    # Compressed DB must be at least 60% smaller than raw uncompressed DB
    shrinkage = (raw_size - comp_size) / raw_size
    assert shrinkage > 0.60, f"Expected shrinkage > 60%, got {shrinkage:.2%}"


def test_get_stats_includes_compression(tmp_path: Path):
    cache = ContextCache(db_path=tmp_path / "stats.db")
    stats = cache.get_stats()
    assert "count" in stats
    assert "compression" in stats
    assert stats["compression"] in ("zstd", "zlib")
