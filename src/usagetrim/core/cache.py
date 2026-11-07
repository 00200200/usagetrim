from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import zlib
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class RecoveryResult:
    """Text plus evidence that this call actually returned recovered content."""

    text: str = field(repr=False)
    recovered: bool
    error: bool = False


class _ExecutionArtifactSearchUnavailable(ValueError):
    """An execution artifact cannot be indexed as a legacy text snapshot."""


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

    def begin_artifact(
        self,
        *,
        quota_bytes: int | None = None,
        max_active: int = 4,
        execution_id: str | None = None,
        stream="merged",
    ):
        """Begin an internal bounded execution artifact in this CCR database."""
        from usagetrim.core.execution_artifacts import begin_artifact

        return begin_artifact(
            self,
            quota_bytes=quota_bytes,
            max_active=max_active,
            execution_id=execution_id,
            stream=stream,
        )

    def execution_recovery(self, execution_id: str):
        from usagetrim.core.execution_artifacts import execution_recovery

        return execution_recovery(self, execution_id)

    def iter_execution_observations(self, execution_id: str):
        from usagetrim.core.execution_artifacts import iter_execution_observations

        return iter_execution_observations(self, execution_id)

    def artifact_metadata(self, ref_id: str):
        from usagetrim.core.execution_artifacts import artifact_metadata

        return artifact_metadata(self, ref_id)

    def read_artifact(self, ref_id: str, *, first_line=1, last_line=None, max_bytes=65536):
        from usagetrim.core.execution_artifacts import read_artifact

        return read_artifact(
            self, ref_id, first_line=first_line, last_line=last_line, max_bytes=max_bytes
        )

    def iter_artifact(self, ref_id: str):
        from usagetrim.core.execution_artifacts import iter_artifact

        return iter_artifact(self, ref_id)

    def expire_artifact_writes(self, *, stale_before=None):
        from usagetrim.core.execution_artifacts import expire_writes

        return expire_writes(self, stale_before=stale_before)

    @staticmethod
    def _is_execution_artifact(conn, ref_id):
        # Do not require/migrate artifact tables for legacy-only cache users.
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_artifacts'"
        ).fetchone()
        if (
            exists
            and conn.execute(
                "SELECT 1 FROM execution_artifacts WHERE ref_id=?", (ref_id,)
            ).fetchone()
        ):
            return True
        # A damaged/missing artifact metadata table must not expose its empty
        # anchor as a successful legacy recovery. Ordinary legacy hashes are set.
        return bool(
            conn.execute(
                """SELECT 1 FROM output_cache WHERE ref_id=? AND source='execution-artifact'
            AND content_hash IS NULL""",
                (ref_id,),
            ).fetchone()
        )

    def retrieve(self, ref_id: str, lines_range: str | None = None) -> str:
        """Retrieve stored content by ref ID, optionally slicing a line range."""
        return self.retrieve_result(ref_id, lines_range).text

    def retrieve_result(self, ref_id: str, lines_range: str | None = None) -> RecoveryResult:
        """Read once and report recovery without interpreting user payload text."""
        with sqlite3.connect(self.db_path) as conn:
            artifact = self._is_execution_artifact(conn, ref_id)
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
            return RecoveryResult(
                f"Error: ref ID '{ref_id}' not found in usagetrim cache.", False, True
            )

        if artifact:
            # New internal refs must not look like empty/full legacy payloads.
            # Bounded output and explicit storage/response state are mandatory.
            first, last = 1, None
            if lines_range:
                try:
                    parts = lines_range.split("-", 1)
                    first = int(parts[0])
                    last = int(parts[-1])
                    if first < 1 or last < first:
                        raise ValueError
                except ValueError:
                    return RecoveryResult(
                        "Error: lines must be a positive line number or an ascending range (e.g. 10-40).",
                        False,
                        True,
                    )
            result = self.read_artifact(ref_id, first_line=first, last_line=last)
            return RecoveryResult(
                f"# [Execution artifact {ref_id}; state={result.metadata.state.value}; "
                "view=redacted-normalized; "
                f"storage_complete={result.metadata.complete}; "
                f"response_truncated={result.response_truncated}]\n" + result.text,
                result.metadata.available
                and not result.response_truncated
                and (bool(result.text) if lines_range else result.metadata.complete),
            )

        # Also protect entries written by older versions before cache redaction.
        content, source = redact_secrets(decompress_payload(row[0])), row[1]
        if not lines_range:
            return RecoveryResult(content, True)

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
            return RecoveryResult(
                f"# [Retrieved {ref_id} ({source}) lines {start}-{end} of {len(all_lines)}]\n"
                + "\n".join(selected),
                bool(selected),
            )
        except ValueError:
            return RecoveryResult(
                "Error: lines must be a positive line number or an ascending range (e.g. 10-40).",
                False,
                True,
            )

    def _search_snapshot(self, ref_id, query, limit, *, check_complete=False):
        """Search only the requested recovery record; never search conversations.

        Index the already-redacted snapshot lazily. FTS data is recovery content,
        kept in cache.db rather than the metadata-only telemetry database.
        """
        from usagetrim.core.code_index import terms

        expression = terms(query)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be 1–20")
        with sqlite3.connect(self.db_path) as conn:
            artifact = self._is_execution_artifact(conn, ref_id)
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
            return None, [], False
        if artifact:
            raise _ExecutionArtifactSearchUnavailable(
                "Execution artifact search is not indexed; use bounded line-range recovery."
            )
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
            # terms() emits quoted words joined by OR; retain FTS token semantics.
            complete = False
            if check_complete:
                full = conn.execute(
                    "SELECT first,last,body FROM recovery_search WHERE recovery_search MATCH ? AND ref=? ORDER BY bm25(recovery_search),CAST(first AS INTEGER) LIMIT ?",
                    (expression.replace(" OR ", " AND "), ref_id, limit + 1),
                ).fetchall()
                # Completeness must refer to retained candidates, not an unseen
                # match elsewhere in the record. Keep full matches first.
                full_ranges = {(first, last) for first, last, _ in full}
                rows = (full + [r for r in rows if (r[0], r[1]) not in full_ranges])[: limit + 1]
                complete = bool(full)
        return original, rows, complete

    @staticmethod
    def _lexical_result(ref_id, rows, limit):
        result = "\n".join(
            f"# {ref_id} lines {first}-{last}\n{body}" for first, last, body in rows[:limit]
        )
        if len(rows) > limit:
            result += "\n[More matching chunks; narrow query or raise limit.]"
        return result or f"No matches in {ref_id}; the original remains available with retrieve."

    def search(self, ref_id: str, query: str, limit: int = 5) -> str:
        """Lexical search of one redacted recovery record, with legacy output."""
        return self.search_result(ref_id, query, limit).text

    def search_result(self, ref_id: str, query: str, limit: int = 5) -> RecoveryResult:
        """Read lexical candidates once; notices alone are not recovered content."""
        try:
            original, rows, _ = self._search_snapshot(ref_id, query, limit)
        except _ExecutionArtifactSearchUnavailable as exc:
            return RecoveryResult(str(exc), False)
        if original is None:
            return RecoveryResult(
                f"Error: ref ID '{ref_id}' not found in usagetrim cache.", False, True
            )
        return RecoveryResult(self._lexical_result(ref_id, rows, limit), bool(rows))

    def hybrid_search(
        self,
        ref_id: str,
        query: str,
        budget: int = 1000,
        *,
        limit: int = 3,
        token_provider: str = "openai",
        embedder=None,
    ) -> str:
        """Opt-in, offline semantic fallback; vectors live only for this call."""
        return self.hybrid_search_result(
            ref_id, query, budget, limit=limit, token_provider=token_provider, embedder=embedder
        ).text

    def hybrid_search_result(
        self,
        ref_id: str,
        query: str,
        budget: int = 1000,
        *,
        limit: int = 3,
        token_provider: str = "openai",
        embedder=None,
    ) -> RecoveryResult:
        """Report emitted chunks separately from budget/availability notices."""
        from usagetrim.metrics.tokenizer import count_tokens_offline

        if type(budget) is not int or budget < 1:
            raise ValueError("budget must be a positive integer")
        if token_provider not in {"openai", "claude", "gemini"}:
            raise ValueError("token_provider must be openai, claude or gemini")

        def fits(text):
            # Include the newline that CLI _emit adds to its stdout.
            emitted = text + ("\n" if text and not text.endswith("\n") else "")
            counts, _ = count_tokens_offline(emitted)
            return getattr(counts, token_provider) <= budget

        try:
            original, lexical, complete = self._search_snapshot(
                ref_id, query, limit, check_complete=True
            )
        except _ExecutionArtifactSearchUnavailable as exc:
            notice = str(exc)
            if not fits(notice):
                raise ValueError("budget is too small for the recovery status") from None
            return RecoveryResult(notice, False)
        if original is None:
            missing = f"Error: ref ID '{ref_id}' not found in usagetrim cache."
            if not fits(missing):
                raise ValueError("budget is too small for the recovery status")
            return RecoveryResult(missing, False, True)
        candidates = lexical
        semantic_ranges = set()
        skipped = []
        if not complete:
            from usagetrim.core.semantic_search import rank_chunks

            semantic, skipped = rank_chunks(original, redact_secrets(query), embedder)
            if semantic:
                semantic_ranges = {(first, last) for first, last, _ in semantic}
                candidates = semantic + [
                    row
                    for row in lexical
                    if not any(
                        row[0] <= last and first <= row[1] for first, last in semantic_ranges
                    )
                ]
        legacy = self._lexical_result(ref_id, lexical, limit)
        if not semantic_ranges and not skipped and fits(legacy):
            return RecoveryResult(legacy, bool(lexical))

        selected = []

        def render(rows):
            keys = {(first, last) for first, last, _ in rows}
            omitted = next(((a, b) for a, b, _ in candidates if (a, b) not in keys), None)
            omitted = omitted or (skipped[0] if skipped else None)
            blocks = [
                f"# {ref_id} lines {a}-{b}"
                + (" [semantic candidate]" if (a, b) in semantic_ranges else "")
                + "\n"
                + body
                for a, b, body in rows
            ]
            if omitted:
                a, b = omitted
                blocks.append(f"[Omitted; retrieve {ref_id} --lines {a}-{b}]")
            return "\n".join(blocks) or legacy

        for row in candidates:
            if len(selected) == limit:
                break
            if fits(render([*selected, row])):
                selected.append(row)
        result = render(selected)
        if not fits(result):
            raise ValueError("budget is too small for the recovery notice")
        return RecoveryResult(result, bool(selected))

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
            has_artifacts = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_artifacts'"
            ).fetchone()
            active_filter = (
                "WHERE ref_id NOT IN (SELECT ref_id FROM execution_artifacts WHERE state='writing')"
                if has_artifacts
                else ""
            )
            payload_size = "LENGTH(content)"
            if has_artifacts:
                payload_size = (
                    "COALESCE(LENGTH(content),0) + COALESCE((SELECT stored_bytes + "
                    "chunk_count * 256 + 1024 FROM execution_artifacts AS artifact "
                    "WHERE artifact.ref_id=output_cache.ref_id),0)"
                )
            cursor = conn.execute(
                f"""
                SELECT ref_id, {payload_size}
                FROM output_cache
                {active_filter}
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
