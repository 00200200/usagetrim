"""CCR metadata pages must never read stored output or change cache counters."""

import sqlite3

import pytest

from usagetrim.core import cache as cache_module
from usagetrim.core.cache import ContextCache
from usagetrim.tui import cache_data


def populated_cache(tmp_path, count=123):
    cache = ContextCache(tmp_path / "cache.db")
    with sqlite3.connect(cache.db_path) as conn:
        conn.executemany(
            "INSERT INTO output_cache "
            "(ref_id,source,content,created_at,hit_count,last_inquired_at) VALUES (?,?,?,?,?,?)",
            [
                (f"tc_{i:016x}", f"source-{i}", b"ZLIB-not-decodable", float(i), i, float(i))
                for i in range(count)
            ],
        )
    return cache


def test_cache_metadata_pages_are_bounded_newest_first_and_disjoint(tmp_path):
    cache = populated_cache(tmp_path)
    pages = [cache_data.read_cache_page(cache, page=index) for index in range(3)]
    assert [len(page.entries) for page in pages] == [50, 50, 23]
    assert all(page.total == 123 and page.page_count == 3 for page in pages)
    assert pages[0].entries[0].ref_id == f"tc_{122:016x}"
    assert pages[0].entries[-1].ref_id == f"tc_{73:016x}"
    assert not pages[0].has_previous and pages[0].has_next
    assert pages[2].has_previous and not pages[2].has_next
    refs = [entry.ref_id for page in pages for entry in page.entries]
    assert len(set(refs)) == 123
    assert refs == [f"tc_{i:016x}" for i in range(122, -1, -1)]
    assert cache_data.read_cache_page(cache, page=999) == pages[2]


def test_cache_metadata_ref_search_is_literal_and_parameterized(tmp_path):
    cache = populated_cache(tmp_path, 2)
    literal = "tc_%_' OR 1=1 --żółć"
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute("UPDATE output_cache SET ref_id=? WHERE created_at=0", (literal,))
    for query in (literal, "%_", "OR 1=1", "żółć"):
        page = cache_data.read_cache_page(cache, ref_query=query, page=99)
        assert page.total == 1 and page.page == 0
        assert page.entries[0].ref_id == literal
    assert cache_data.read_cache_page(cache, ref_query="missing").entries == ()
    assert cache_data.read_cache_page(cache, ref_query="ŻÓŁĆ").total == 0
    assert cache_data.read_cache_metadata(cache, literal).ref_id == literal


def test_cache_metadata_missing_and_empty_database_do_not_create_or_migrate(tmp_path, monkeypatch):
    directory = tmp_path / "not-created"
    monkeypatch.setenv("USAGETRIM_CACHE_DIR", str(directory))
    result = cache_data.read_cache_page()
    assert not result.available and result.total == 0 and result.entries == ()
    assert cache_data.read_cache_metadata(None, "tc_missing") is None
    assert not directory.exists()
    directory.mkdir()
    database = directory / "cache.db"
    database.write_bytes(b"")
    assert not cache_data.read_cache_page().available
    assert database.read_bytes() == b""
    cache = ContextCache(database)
    result = cache_data.read_cache_page(cache)
    assert result.available and result.total == 0 and result.page_count == 1
    assert not result.has_next and not result.has_previous


def test_cache_metadata_deletion_clamps_page_and_missing_selection(tmp_path):
    cache = populated_cache(tmp_path, 101)
    assert cache_data.read_cache_page(cache, page=2).page == 2
    ref = f"tc_{100:016x}"
    assert cache_data.read_cache_metadata(cache, ref) is not None
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute("DELETE FROM output_cache WHERE created_at>=50")
    page = cache_data.read_cache_page(cache, page=2)
    assert page.page == 0 and page.total == 50 and len(page.entries) == 50
    assert cache_data.read_cache_metadata(cache, ref) is None
    cache.db_path.unlink()
    assert not cache_data.read_cache_page(cache).available
    assert cache_data.read_cache_metadata(cache, ref) is None
    assert not cache.db_path.exists()


def test_cache_metadata_preserves_nulls_and_deterministic_timestamp_ties(tmp_path):
    cache = populated_cache(tmp_path, 3)
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute("UPDATE output_cache SET created_at=10")
        conn.execute(
            "UPDATE output_cache SET source=NULL,hit_count=NULL,last_inquired_at=NULL WHERE ref_id=?",
            (f"tc_{2:016x}",),
        )
    page = cache_data.read_cache_page(cache)
    assert [row.ref_id for row in page.entries] == [f"tc_{i:016x}" for i in (2, 1, 0)]
    entry = page.entries[0]
    assert entry.source is None and entry.hit_count is None and entry.last_inquired_at is None


def test_cache_metadata_never_reads_content_decompresses_or_records_hits(tmp_path, monkeypatch):
    cache = populated_cache(tmp_path, 3)
    original_connect = sqlite3.connect
    reads = []

    def authorizer(action, table, column, database, trigger):
        if action == sqlite3.SQLITE_READ:
            reads.append((table, column))
            if table == "output_cache" and column == "content":
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def connect(*args, **kwargs):
        assert kwargs.get("uri") is True and "mode=ro" in args[0]
        conn = original_connect(*args, **kwargs)
        conn.set_authorizer(authorizer)
        return conn

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "metadata browsing must not retrieve/decompress content or migrate cache"
        )

    monkeypatch.setattr(ContextCache, "retrieve", forbidden)
    monkeypatch.setattr(ContextCache, "_ensure_db", forbidden)
    monkeypatch.setattr(cache_module, "decompress_payload", forbidden)
    with monkeypatch.context() as guarded:
        guarded.setattr(cache_data.sqlite3, "connect", connect)
        page = cache_data.read_cache_page(cache)
        entry = cache_data.read_cache_metadata(cache, page.entries[0].ref_id)
        assert entry.hit_count == 2 and entry.last_inquired_at == 2.0
    with original_connect(cache.db_path) as conn:
        assert conn.execute(
            "SELECT hit_count,last_inquired_at FROM output_cache ORDER BY created_at DESC"
        ).fetchall() == [(2, 2.0), (1, 1.0), (0, 0.0)]
    assert ("output_cache", "content") not in reads


@pytest.mark.parametrize("page", [-1, True, 0.5])
def test_cache_metadata_rejects_invalid_page(tmp_path, page):
    cache = populated_cache(tmp_path, 0)
    with pytest.raises(ValueError, match="page"):
        cache_data.read_cache_page(cache, page=page)
