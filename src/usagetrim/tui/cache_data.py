"""Read-only, metadata-only access to the existing CCR cache."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from usagetrim.core.cache import DEFAULT_CACHE_DB, ContextCache

PAGE_SIZE = 50
_COLUMNS = "ref_id,source,created_at,hit_count,last_inquired_at"


@dataclass(frozen=True)
class CacheEntry:
    ref_id: str
    source: str | None
    created_at: float | None
    hit_count: int | None
    last_inquired_at: float | None


@dataclass(frozen=True)
class CachePage:
    entries: tuple[CacheEntry, ...]
    page: int
    total: int
    available: bool = True

    @property
    def page_count(self) -> int:
        return max(1, (self.total + PAGE_SIZE - 1) // PAGE_SIZE)

    @property
    def has_previous(self) -> bool:
        return self.page > 0

    @property
    def has_next(self) -> bool:
        return (self.page + 1) * PAGE_SIZE < self.total


@contextmanager
def _metadata_connection(cache: ContextCache | None):
    # Do not construct ContextCache just to inspect it: its constructor creates
    # the database and may migrate schemas. Resolve the same configured location.
    cache_dir = os.environ.get("USAGETRIM_CACHE_DIR")
    path = (
        cache.db_path
        if cache is not None
        else (Path(cache_dir) / "cache.db" if cache_dir else DEFAULT_CACHE_DB)
    )
    if not path.is_file():
        yield None
        return
    try:
        conn = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError:
        if not path.is_file():
            yield None
            return
        raise
    try:
        conn.row_factory = sqlite3.Row
        # Keep page count and page rows in one read snapshot during eviction.
        conn.execute("BEGIN")
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type=? AND name=?", ("table", "output_cache")
        ).fetchone()
        yield conn if exists else None
    finally:
        conn.close()


def _is_execution_artifact(conn, ref_id):
    # Older text-only cache schemas may lack the anchor metadata columns.
    columns = {column[1] for column in conn.execute("PRAGMA table_info(output_cache)")}
    return {"source", "content_hash"} <= columns and ContextCache._is_execution_artifact(
        conn, ref_id
    )


def read_cache_page(
    cache: ContextCache | None = None,
    *,
    ref_query: str = "",
    page: int = 0,
) -> CachePage:
    """Read a newest-first page, with literal case-sensitive ref ID substring search.

    Only explicit metadata columns are selected. Never retrieve/decompress
    content or increment inquiry counters. Clamp pages after deletion/eviction.
    """
    if type(page) is not int or page < 0:
        raise ValueError("page must be a nonnegative integer")
    with _metadata_connection(cache) as conn:
        if conn is None:
            return CachePage((), 0, 0, available=False)
        total = conn.execute(
            "SELECT COUNT(*) FROM output_cache WHERE instr(ref_id, ?) > 0", (ref_query,)
        ).fetchone()[0]
        page = min(page, max(0, (total - 1) // PAGE_SIZE))
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM output_cache WHERE instr(ref_id, ?) > 0 "
            "ORDER BY created_at DESC, ref_id DESC LIMIT ? OFFSET ?",
            (ref_query, PAGE_SIZE, page * PAGE_SIZE),
        ).fetchall()
    return CachePage(tuple(CacheEntry(**dict(row)) for row in rows), page, total)


def read_cache_metadata(cache: ContextCache | None, ref_id: str) -> CacheEntry | None:
    """Refresh selected metadata so a removed reference is not shown as available."""
    with _metadata_connection(cache) as conn:
        if conn is None:
            return None
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM output_cache WHERE ref_id=? LIMIT 1", (ref_id,)
        ).fetchone()
    return CacheEntry(**dict(row)) if row is not None else None
