from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Any

try:
    import zstandard as zstd
except ImportError:
    zstd = None

from usagetrim.core.redactor import redact_secrets

DEFAULT_CACHE_DIR = Path.home() / ".usagetrim"
DEFAULT_CACHE_DB = DEFAULT_CACHE_DIR / "cache.db"
COMPRESSION_THRESHOLD = 512
DEFAULT_CACHE_BUDGET_BYTES = 500 * 1024 * 1024  # 500 MB


def get_cache_budget_bytes() -> int:
    """Return maximum cache budget in bytes from env or default (500 MB)."""
    env_bytes = os.environ.get("USAGETRIM_CACHE_MAX_BYTES")
    if env_bytes:
        try:
            return int(env_bytes)
        except ValueError:
            pass
    env_mb = os.environ.get("USAGETRIM_CACHE_MAX_MB")
    if env_mb:
        try:
            return int(env_mb) * 1024 * 1024
        except ValueError:
            pass
    return DEFAULT_CACHE_BUDGET_BYTES


def compress_payload(data: str) -> bytes | str:
    """Compress payload if it exceeds the 512-byte threshold, using zstd or zlib."""
    raw = data.encode("utf-8")
    if len(raw) <= COMPRESSION_THRESHOLD:
        return data

    if zstd is not None:
        cctx = zstd.ZstdCompressor(level=3)
        return b"ZSTD" + cctx.compress(raw)
    return b"ZLIB" + zlib.compress(raw, level=6)


def decompress_payload(stored: str | bytes) -> str:
    """Transparently decompress stored payload if compressed with zstd or zlib."""
    if isinstance(stored, str):
        return stored
    if isinstance(stored, bytes):
        if stored.startswith(b"ZSTD"):
            global zstd
            if zstd is None:
                try:
                    import zstandard as zstd
                except ImportError:
                    pass
            if zstd is not None:
                dctx = zstd.ZstdDecompressor()
                return dctx.decompress(stored[4:]).decode("utf-8")
            raise RuntimeError("zstandard package is required to decompress ZSTD cache payload")
        if stored.startswith(b"ZLIB"):
            return zlib.decompress(stored[4:]).decode("utf-8")
        if stored.startswith(b"\x28\xb5\x2f\xfd"):  # raw zstd frame
            if zstd is not None:
                dctx = zstd.ZstdDecompressor()
                return dctx.decompress(stored).decode("utf-8")
        if stored.startswith(b"\x78"):  # raw zlib
            return zlib.decompress(stored).decode("utf-8")
        return stored.decode("utf-8", errors="replace")
    return str(stored)


class ContextCache:
    """Local SQLite-backed cache for Compress-Cache-Retrieve (CCR) architecture.

    Stored content is recoverable after recognized secrets have been redacted.
    An agent or developer can retrieve slices using the generated ref ID.
    """

    def __init__(self, db_path: Path | None = None):
        cache_dir = os.environ.get("USAGETRIM_CACHE_DIR")
        self.db_path = db_path or (Path(cache_dir) / "cache.db" if cache_dir else DEFAULT_CACHE_DB)
        self._ensure_db()

    def _ensure_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS output_cache (
                    ref_id TEXT PRIMARY KEY,
                    content_hash TEXT,
                    source TEXT,
                    content TEXT,
                    created_at REAL,
                    hit_count INTEGER DEFAULT 0,
                    last_inquired_at REAL DEFAULT 0.0
                )
                """
            )
            # Ensure LRI tracking columns exist for older schemas
            cursor = conn.execute("PRAGMA table_info(output_cache)")
            columns = {row[1] for row in cursor.fetchall()}
            if "hit_count" not in columns:
                conn.execute("ALTER TABLE output_cache ADD COLUMN hit_count INTEGER DEFAULT 0")
            if "last_inquired_at" not in columns:
                conn.execute(
                    "ALTER TABLE output_cache ADD COLUMN last_inquired_at REAL DEFAULT 0.0"
                )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hash ON output_cache(content_hash)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lri ON output_cache("
                "hit_count, last_inquired_at, created_at)"
            )
            conn.commit()

    def store(self, content: str, source: str = "exec", *, namespace: str = "") -> str:
        """Store redacted content and return a human-readable reference ID."""
        content = redact_secrets(content)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        # Separate measurement owners without breaking existing stable refs.
        # Hash the digest, not a raw-text prefix (which could alias another log).
        ref_hash = (
            hashlib.sha256(f"{namespace}:{content_hash}".encode()).hexdigest()
            if namespace
            else content_hash
        )
        ref_id = f"tc_{ref_hash[:16]}"

        stored_payload = compress_payload(content)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO output_cache (
                    ref_id, content_hash, source, content, created_at, hit_count, last_inquired_at
                )
                VALUES (?, ?, ?, ?, ?, 0, 0.0)
                ON CONFLICT(ref_id) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    source = excluded.source,
                    content = excluded.content,
                    created_at = excluded.created_at
                """,
                (ref_id, content_hash, source, stored_payload, time.time()),
            )
            conn.commit()

        # Enforce budget in background if exceeded
        budget = get_cache_budget_bytes()
        if self.db_path.exists() and self.db_path.stat().st_size > budget:
            self.evict_lri(max_bytes=budget)

        return ref_id

    def retrieve(self, ref_id: str, lines_range: str | None = None) -> str:
        """Retrieve stored content by ref ID, optionally slicing a line range."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT content, source FROM output_cache WHERE ref_id = ?",
                (ref_id,),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE output_cache
                    SET hit_count = hit_count + 1,
                        last_inquired_at = ?
                    WHERE ref_id = ?
                    """,
                    (time.time(), ref_id),
                )
                conn.commit()

        if not row:
            return f"Error: ref ID '{ref_id}' not found in usagetrim cache."

        # Also protect entries written by older versions before cache redaction.
        content, source = redact_secrets(decompress_payload(row[0])), row[1]
        if not lines_range:
            return content

        all_lines = content.splitlines()
        try:
            if "-" in lines_range:
                start_s, end_s = lines_range.split("-", 1)
                start, end = int(start_s), int(end_s)
            else:
                start = int(lines_range)
                end = start
            if start < 1 or end < start:
                raise ValueError
            end = min(len(all_lines), end)
            selected = all_lines[start - 1 : end]
            return (
                f"# [Retrieved {ref_id} ({source}) lines {start}-{end} of {len(all_lines)}]\n"
                + "\n".join(selected)
            )
        except ValueError:
            return "Error: lines must be a positive line number or an ascending range (e.g. 10-40)."

    def search(self, ref_id: str, query: str, limit: int = 5) -> str:
        """Search only the requested recovery record; never search conversations.

        Index the already-redacted snapshot lazily. FTS data is recovery content,
        kept in cache.db rather than the metadata-only telemetry database.
        """
        from usagetrim.core.code_index import terms

        expression = terms(query)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be 1–20")
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT content FROM output_cache WHERE ref_id=?", (ref_id,)
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE output_cache
                    SET hit_count = hit_count + 1,
                        last_inquired_at = ?
                    WHERE ref_id = ?
                    """,
                    (time.time(), ref_id),
                )
                conn.commit()
        if row is None:
            return f"Error: ref ID '{ref_id}' not found in usagetrim cache."
        original = redact_secrets(decompress_payload(row[0]))
        checksum = hashlib.sha256(original.encode()).hexdigest()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS recovery_search USING fts5(ref UNINDEXED, first UNINDEXED, last UNINDEXED, body)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS recovery_indexed (ref TEXT PRIMARY KEY, checksum TEXT)"
            )
            existing = conn.execute(
                "SELECT checksum FROM recovery_indexed WHERE ref=?", (ref_id,)
            ).fetchone()
            if not existing or existing[0] != checksum:
                conn.execute("DELETE FROM recovery_search WHERE ref=?", (ref_id,))
                lines = original.splitlines()
                conn.executemany(
                    "INSERT INTO recovery_search(ref,first,last,body) VALUES (?,?,?,?)",
                    [
                        (ref_id, i + 1, min(i + 30, len(lines)), "\n".join(lines[i : i + 30]))
                        for i in range(0, len(lines), 30)
                    ],
                )
                conn.execute(
                    "INSERT OR REPLACE INTO recovery_indexed VALUES (?,?)", (ref_id, checksum)
                )
            rows = conn.execute(
                "SELECT first,last,body FROM recovery_search WHERE recovery_search MATCH ? AND ref=? ORDER BY bm25(recovery_search),CAST(first AS INTEGER) LIMIT ?",
                (expression, ref_id, limit + 1),
            ).fetchall()
        result = "\n".join(
            f"# {ref_id} lines {first}-{last}\n{body}" for first, last, body in rows[:limit]
        )
        if len(rows) > limit:
            result += "\n[More matching chunks; narrow query or raise limit.]"
        return result or f"No matches in {ref_id}; the original remains available with retrieve."

    def check_duplicate(self, content: str) -> str | None:
        """Check if identical content was cached recently within last 15 minutes."""
        content_hash = hashlib.sha256(redact_secrets(content).encode("utf-8")).hexdigest()
        cutoff = time.time() - 900  # 15 mins
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT ref_id FROM output_cache WHERE content_hash = ? AND created_at > ? ORDER BY created_at DESC LIMIT 1",
                (content_hash, cutoff),
            ).fetchone()
        return row[0] if row else None

    def dedup_notice(self, content: str, *, min_chars: int = 512) -> str | None:
        """If identical content was just cached, return a short session-dedup notice.

        Inspired by sqz-style session refs: do not re-emit a large blob the model
        already saw. Small payloads stay inline so agents are not forced to retrieve.
        """
        if not content or len(content) < min_chars:
            return None
        ref_id = self.check_duplicate(content)
        if not ref_id:
            return None
        return f"[UsageTrim: identical output already cached — usagetrim retrieve {ref_id}]\n"

    def session_view(self, content: str, *, source: str = "read", min_chars: int = 512) -> str:
        """Return a short ref if this payload was already emitted; else remember and pass through.

        Used by ``usagetrim cat`` and MCP ``usagetrim_read`` so identical file views in
        the same session do not re-spend context. Store failures never block the read.
        """
        if not content or len(content) < min_chars:
            return content
        notice = self.dedup_notice(content, min_chars=min_chars)
        if notice is not None:
            return notice
        try:
            self.store(content, source=source)
        except Exception:
            pass
        return content

    def clear(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM output_cache")
            for table in ("recovery_search", "recovery_indexed"):
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                    conn.execute(f"DELETE FROM {table}")
            conn.commit()

    def evict_lri(
        self,
        max_bytes: int | None = None,
        target_bytes: int | None = None,
        max_items_to_remove: int | None = None,
    ) -> list[str]:
        """Evict cached blocks using Least Recently Inquired (LRI) policy.

        Weight retention by frequency (hit_count) and recency (last_inquired_at).
        Unreferenced blocks (hit_count == 0) are evicted before any inquired blocks.
        Automatic VACUUM reclaims freed SQLite pages safely in <50ms.
        """
        if not self.db_path.exists():
            return []

        budget = max_bytes if max_bytes is not None else get_cache_budget_bytes()
        current_size = self.db_path.stat().st_size

        if max_items_to_remove is None and current_size <= budget:
            return []

        target = target_bytes if target_bytes is not None else int(budget * 0.85)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT ref_id, LENGTH(content)
                FROM output_cache
                ORDER BY
                    CASE WHEN hit_count > 0 THEN 1 ELSE 0 END ASC,
                    hit_count ASC,
                    last_inquired_at ASC,
                    created_at ASC
                """
            )
            candidates = cursor.fetchall()

            evicted_refs: list[str] = []
            freed_bytes = 0

            for ref_id, payload_len in candidates:
                if max_items_to_remove is not None:
                    evicted_refs.append(ref_id)
                    if len(evicted_refs) >= max_items_to_remove:
                        break
                else:
                    evicted_refs.append(ref_id)
                    freed_bytes += payload_len or 0
                    if (current_size - freed_bytes) <= target:
                        break

            if evicted_refs:
                placeholders = ",".join("?" for _ in evicted_refs)
                conn.execute(
                    f"DELETE FROM output_cache WHERE ref_id IN ({placeholders})",
                    evicted_refs,
                )
                for table, col in (("recovery_search", "ref"), ("recovery_indexed", "ref")):
                    if conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE name=?", (table,)
                    ).fetchone():
                        conn.execute(
                            f"DELETE FROM {table} WHERE {col} IN ({placeholders})",
                            evicted_refs,
                        )
                conn.commit()
                conn.execute("VACUUM")

        return evicted_refs

    def get_stats(self) -> dict[str, Any]:
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM output_cache").fetchone()[0]
            hits = conn.execute("SELECT SUM(hit_count) FROM output_cache").fetchone()[0] or 0
            inquired_count = (
                conn.execute("SELECT COUNT(*) FROM output_cache WHERE hit_count > 0").fetchone()[0]
                or 0
            )
        size_kb = self.db_path.stat().st_size / 1024 if self.db_path.exists() else 0
        return {
            "count": count,
            "size_kb": size_kb,
            "path": str(self.db_path),
            "compression": "zstd" if zstd is not None else "zlib",
            "total_inquiries": hits,
            "inquired_entries": inquired_count,
            "budget_mb": get_cache_budget_bytes() // (1024 * 1024),
        }
