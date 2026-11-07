"""Complete recovery must be bounded, redacted and published without overwrite."""

import os
import sqlite3
import zlib

import pytest

from usagetrim.core.cache import ContextCache
from usagetrim.tui import cache_content as content


def cache_payload(tmp_path, payload):
    cache = ContextCache(tmp_path / "cache.db")
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute("INSERT INTO output_cache(ref_id,content) VALUES (?,?)", ("tc_test", payload))
    return cache


@pytest.mark.parametrize("kind", ["text", "raw", "zlib", "legacy-zlib", "zstd"])
def test_complete_content_formats_search_and_export(tmp_path, kind):
    text = "first\r\nżółć diagnostic\nlast without newline"
    payload = text
    if kind == "raw":
        payload = text.encode()
    elif kind in {"zlib", "legacy-zlib"}:
        payload = (b"ZLIB" if kind == "zlib" else b"") + zlib.compress(text.encode())
    elif kind == "zstd":
        zstd = pytest.importorskip("zstandard")
        payload = b"ZSTD" + zstd.ZstdCompressor().compress(text.encode())
    cache = cache_payload(tmp_path, payload)
    assert content.search_content(cache, "tc_test", "diagnostic") == [(2, "żółć diagnostic")]
    target = tmp_path / "export.log"
    content.export_content(cache, "tc_test", target)
    assert target.read_bytes() == text.encode()
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".usagetrim-export-*"))


def test_redaction_precedes_search_and_complete_export(tmp_path):
    secret = "ghp_" + "A" * 36
    cache = cache_payload(tmp_path, f"credential={secret}\n" + "match\n" * 30)
    assert content.search_content(cache, "tc_test", secret) == []
    matches = content.search_content(cache, "tc_test", "match")
    assert len(matches) == 20 and matches[0][0] == 2 and matches[-1][0] == 21
    target = tmp_path / "redacted.log"
    content.export_content(cache, "tc_test", target)
    data = target.read_text()
    assert secret not in data and "REDACTED" in data
    assert data.count("match\n") == 30


@pytest.mark.parametrize("color", ["\x1b[31m", "\x9b31m"])
def test_ansi_cannot_hide_credentials_from_search_or_export(tmp_path, color):
    cache = cache_payload(tmp_path, "credential=ghp_" + color + "A" * 36 + "\r\n")
    assert content.search_content(cache, "tc_test", "A" * 36) == []
    target = tmp_path / "redacted.log"
    content.export_content(cache, "tc_test", target)
    exported = target.read_bytes()
    assert b"REDACTED" in exported and b"A" * 36 not in exported
    assert exported.endswith(b"\r\n")


@pytest.mark.parametrize(
    "payload", [b"ZLIBbroken", b"ZLIB" + zlib.compress(b"valid")[:-1], b"\xff"]
)
def test_corrupt_content_is_never_exported(tmp_path, payload):
    cache = cache_payload(tmp_path, payload)
    target = tmp_path / "refused.log"
    with pytest.raises(content.CacheContentError):
        content.export_content(cache, "tc_test", target)
    assert not target.exists() and not list(tmp_path.glob(".usagetrim-export-*"))


def test_compression_bomb_and_stored_limits_refuse_export(tmp_path):
    cache = cache_payload(tmp_path, b"ZLIB" + zlib.compress(b"A" * (content.CONTENT_LIMIT + 1)))
    target = tmp_path / "refused.log"
    with pytest.raises(content.CacheContentError, match="4 MiB"):
        content.export_content(cache, "tc_test", target)
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute("UPDATE output_cache SET content=?", (b"A" * (content.STORED_LIMIT + 1),))
    with pytest.raises(content.CacheContentError, match="Stored payload"):
        content.export_content(cache, "tc_test", target)
    assert not target.exists()


def test_exact_limit_and_missing_record(tmp_path):
    cache = cache_payload(tmp_path, b"ZLIB" + zlib.compress(b"A" * content.CONTENT_LIMIT))
    assert len(content.read_redacted_content(cache, "tc_test")) == content.CONTENT_LIMIT
    with pytest.raises(content.CacheContentError, match="missing"):
        content.read_redacted_content(cache, "missing")


def test_export_refuses_existing_file_and_publication_race(tmp_path, monkeypatch):
    cache = cache_payload(tmp_path, "complete log")
    target = tmp_path / "existing.log"
    target.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        content.export_content(cache, "tc_test", target)
    assert target.read_bytes() == b"original"
    target.unlink()
    real_link = content.os.link

    def race(source, destination):
        target.write_bytes(b"other writer")
        return real_link(source, destination)

    monkeypatch.setattr(content.os, "link", race)
    with pytest.raises(FileExistsError):
        content.export_content(cache, "tc_test", target)
    assert target.read_bytes() == b"other writer"
    assert not list(tmp_path.glob(".usagetrim-export-*"))


def test_failed_export_cleans_staged_file(tmp_path, monkeypatch):
    cache = cache_payload(tmp_path, "complete log")

    def fail(*args):
        raise OSError("fsync failed")

    monkeypatch.setattr(content.os, "fsync", fail)
    target = tmp_path / "failed.log"
    with pytest.raises(OSError, match="fsync failed"):
        content.export_content(cache, "tc_test", target)
    assert not target.exists() and not list(tmp_path.glob(".usagetrim-export-*"))


def test_search_matches_beyond_display_limit_and_export_keeps_full_line(tmp_path):
    line = "ż" * 1000 + "needle-at-the-end"
    cache = cache_payload(tmp_path, line + "\n")
    assert content.search_content(cache, "tc_test", "needle-at-the-end") == [(1, line)]
    target = tmp_path / "full-line.log"
    content.export_content(cache, "tc_test", target)
    assert target.read_bytes() == (line + "\n").encode("utf-8")
