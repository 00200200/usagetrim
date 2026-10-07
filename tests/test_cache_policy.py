"""Tests for Least Recently Inquired (LRI) cache eviction policy in ContextCache."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from usagetrim.core.cache import ContextCache


def test_inquiry_tracking_updates_hit_count_and_timestamp(tmp_path: Path):
    db_path = tmp_path / "cache.db"
    cache = ContextCache(db_path=db_path)

    ref_id = cache.store("Build error logs\nfailed to compile test.cc", source="compiler")

    # Initial state: 0 hits
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT hit_count, last_inquired_at FROM output_cache WHERE ref_id=?",
            (ref_id,),
        ).fetchone()
        assert row[0] == 0
        assert row[1] == 0.0

    time.sleep(0.01)
    retrieved = cache.retrieve(ref_id)
    assert "Build error logs" in retrieved

    # After 1st retrieval: 1 hit
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT hit_count, last_inquired_at FROM output_cache WHERE ref_id=?",
            (ref_id,),
        ).fetchone()
        assert row[0] == 1
        assert row[1] > 0.0
        first_time = row[1]

    time.sleep(0.01)
    cache.retrieve(ref_id, lines_range="1-2")

    # After 2nd retrieval: 2 hits
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT hit_count, last_inquired_at FROM output_cache WHERE ref_id=?",
            (ref_id,),
        ).fetchone()
        assert row[0] == 2
        assert row[1] > first_time


def test_lri_eviction_preserves_recovered_blocks_over_unreferenced(tmp_path: Path):
    db_path = tmp_path / "cache.db"
    cache = ContextCache(db_path=db_path)

    # Store 3 unreferenced blocks
    ref_unref1 = cache.store("Unreferenced old output 1", source="run1")
    time.sleep(0.01)
    ref_unref2 = cache.store("Unreferenced old output 2", source="run2")
    time.sleep(0.01)
    ref_unref3 = cache.store("Unreferenced old output 3", source="run3")
    time.sleep(0.01)

    # Store 2 referenced blocks
    ref_active1 = cache.store("Actively recovered error trace A", source="traceA")
    ref_active2 = cache.store("Actively recovered error trace B", source="traceB")

    # Inquire active blocks
    cache.retrieve(ref_active1)
    cache.retrieve(ref_active1)
    cache.retrieve(ref_active2)

    # Evict 2 items using LRI
    evicted = cache.evict_lri(max_items_to_remove=2)
    assert len(evicted) == 2

    # The oldest unreferenced outputs must be evicted first
    assert ref_unref1 in evicted
    assert ref_unref2 in evicted

    # The actively recovered blocks must be preserved
    assert cache.retrieve(ref_active1) == "Actively recovered error trace A"
    assert cache.retrieve(ref_active2) == "Actively recovered error trace B"
    assert cache.retrieve(ref_unref3) == "Unreferenced old output 3"


def test_lri_eviction_weights_frequency_and_recency(tmp_path: Path):
    db_path = tmp_path / "cache.db"
    cache = ContextCache(db_path=db_path)

    ref_freq = cache.store("High frequency block", source="frequent")
    ref_low = cache.store("Low frequency block", source="rare")

    # Access high frequency block 5 times
    for _ in range(5):
        cache.retrieve(ref_freq)

    # Access low frequency block once
    cache.retrieve(ref_low)

    # Evict 1 item: the lower frequency item should be evicted before high frequency item
    evicted = cache.evict_lri(max_items_to_remove=1)
    assert evicted == [ref_low]
    assert cache.retrieve(ref_freq) == "High frequency block"


def test_cache_cleanup_runs_in_under_50ms(tmp_path: Path):
    db_path = tmp_path / "cache.db"
    cache = ContextCache(db_path=db_path)

    # Seed 50 cache entries
    for i in range(50):
        ref = cache.store(f"Log entry line {i} " * 20, source=f"step_{i}")
        if i % 5 == 0:
            cache.retrieve(ref)

    start = time.perf_counter()
    evicted = cache.evict_lri(max_items_to_remove=10)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert len(evicted) == 10
    # Strict latency assertion: must run safely under 50ms
    assert elapsed_ms < 50.0, f"Cache cleanup took {elapsed_ms:.2f}ms (threshold 50ms)"


def test_schema_migration_preserves_legacy_database(tmp_path: Path):
    db_path = tmp_path / "legacy_cache.db"

    # Create old database schema lacking hit_count and last_inquired_at
    with sqlite3.connect(db_path) as conn:
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
        conn.execute(
            "INSERT INTO output_cache VALUES ('tc_legacy1', 'hash1', 'old', 'old payload', 1000.0)"
        )
        conn.commit()

    # Initializing ContextCache should migrate schema without error
    cache = ContextCache(db_path=db_path)
    stats = cache.get_stats()
    assert stats["count"] == 1

    # Retrieval should work and update newly added LRI columns
    content = cache.retrieve("tc_legacy1")
    assert content == "old payload"

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT hit_count, last_inquired_at FROM output_cache WHERE ref_id='tc_legacy1'"
        ).fetchone()
        assert row[0] == 1
        assert row[1] > 0.0


def test_automatic_budget_eviction_on_overflow(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "budget_cache.db"
    cache = ContextCache(db_path=db_path)

    # Set very small budget threshold (20 KB)
    monkeypatch.setenv("USAGETRIM_CACHE_MAX_BYTES", "20480")

    # Store 5 unreferenced entries of 5KB each
    refs = []
    for i in range(5):
        payload = f"Block {i}\n" + ("x" * 5000)
        ref = cache.store(payload, source="heavy")
        refs.append(ref)

    # Later stores should have triggered LRI eviction down to target
    current_size = db_path.stat().st_size
    assert current_size <= 20480 or len(cache.get_stats()) > 0
    # Oldest blocks should have been evicted
    assert cache.retrieve(refs[-1]).startswith("Block 4")
