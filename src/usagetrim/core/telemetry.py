from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from usagetrim.core.cache import DEFAULT_CACHE_DB
from usagetrim.core.companion_state import client_name, project_for
from usagetrim.metrics.pricing import estimate_savings


@dataclass
class LifetimeStats:
    total_runs: int
    raw_claude_tokens: int
    compact_claude_tokens: int
    raw_openai_tokens: int
    compact_openai_tokens: int
    raw_gemini_tokens: int
    compact_gemini_tokens: int

    @property
    def saved_claude(self) -> int:
        return self.raw_claude_tokens - self.compact_claude_tokens

    @property
    def saved_openai(self) -> int:
        return self.raw_openai_tokens - self.compact_openai_tokens

    @property
    def saved_gemini(self) -> int:
        return self.raw_gemini_tokens - self.compact_gemini_tokens

    @property
    def saved_avg(self) -> int:
        return (self.saved_claude + self.saved_openai + self.saved_gemini) // 3

    @property
    def reduction_pct(self) -> float:
        raw_avg = (self.raw_claude_tokens + self.raw_openai_tokens + self.raw_gemini_tokens) // 3
        if raw_avg == 0:
            return 0.0
        return round((self.saved_avg / raw_avg) * 100.0, 1)

    @property
    def estimated_usd_saved(self) -> float:
        savings = estimate_savings(self.saved_claude, self.saved_openai, self.saved_gemini)
        return savings.avg_saved_usd


# This database never stores commands, output, recovery references or conversations.
# Recovery content remains exclusively in cache.db.
EVENT_COLUMNS = (
    "event_id",
    "timestamp",
    "client",
    "project",
    "engine",
    "operation",
    "method",
    "delivery",
    "raw_openai",
    "compact_openai",
    "raw_claude",
    "compact_claude",
    "raw_gemini",
    "compact_gemini",
    "raw_bytes",
    "compact_bytes",
    "duration_s",
)


class TelemetryStore:
    def __init__(self, db_path: Path | None = None):
        cache_dir = Path(os.environ.get("USAGETRIM_CACHE_DIR", str(DEFAULT_CACHE_DB.parent)))
        self.db_path = db_path or cache_dir / "telemetry.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, timestamp REAL NOT NULL,
                client TEXT, project TEXT, engine TEXT NOT NULL, operation TEXT NOT NULL,
                method TEXT NOT NULL, delivery TEXT NOT NULL,
                raw_openai INTEGER, compact_openai INTEGER,
                raw_claude INTEGER, compact_claude INTEGER,
                raw_gemini INTEGER, compact_gemini INTEGER,
                raw_bytes INTEGER, compact_bytes INTEGER, duration_s REAL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS events_time ON events(timestamp)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS imports (source TEXT PRIMARY KEY, last_id INTEGER NOT NULL)"
            )
        if db_path is None:
            self.import_legacy(cache_dir / "cache.db")

    def connect(self):
        return sqlite3.connect(self.db_path, timeout=5)

    def import_legacy(self, path: Path):
        if not path.is_file():
            return
        try:
            with self.connect() as conn:
                previous = conn.execute(
                    "SELECT last_id FROM imports WHERE source=?", (str(path.resolve()),)
                ).fetchone()
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as old:
                rows = old.execute(
                    "SELECT id, timestamp, raw_claude, compact_claude, raw_openai, compact_openai, raw_gemini, compact_gemini FROM telemetry_events WHERE id > ? ORDER BY id",
                    (previous[0] if previous else 0,),
                ).fetchall()
            # Retain legacy history with unknown client/project, never guessed cwd.
            prefix = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24]
            with self.connect() as conn:
                conn.executemany(
                    """INSERT OR IGNORE INTO events
                    (event_id,timestamp,client,project,engine,operation,method,delivery,
                     raw_claude,compact_claude,raw_openai,compact_openai,raw_gemini,compact_gemini)
                    VALUES (?,?,NULL,NULL,'usagetrim','legacy','legacy-estimate','legacy',?,?,?,?,?,?)""",
                    [(f"legacy:{prefix}:{r[0]}", r[1] or 0, *r[2:]) for r in rows],
                )
                if rows:
                    conn.execute(
                        "INSERT OR REPLACE INTO imports VALUES (?, ?)",
                        (str(path.resolve()), rows[-1][0]),
                    )
        except (OSError, sqlite3.Error):
            pass

    def record(
        self,
        raw_claude: int,
        compact_claude: int = 0,
        raw_openai: int = 0,
        compact_openai: int = 0,
        raw_gemini: int = 0,
        compact_gemini: int = 0,
        saved_claude: int | None = None,
        saved_openai: int | None = None,
        saved_gemini: int | None = None,
        command: str | None = None,
        duration_s: float | None = None,
        *,
        event_id: str | None = None,
        client: str | None = None,
        project: str | None = None,
        engine: str = "usagetrim",
        operation: str = "exec",
        method: str = "o200k_base",
        delivery: str = "returned",
        raw_bytes: int | None = None,
        compact_bytes: int | None = None,
        **kwargs,
    ):
        # `command` is accepted for compatibility, deliberately never persisted.
        if saved_claude is not None:
            compact_claude = max(0, raw_claude - saved_claude)
        if saved_openai is not None:
            compact_openai = max(0, raw_openai - saved_openai)
        if saved_gemini is not None:
            compact_gemini = max(0, raw_gemini - saved_gemini)
        event = (
            event_id or str(uuid.uuid4()),
            time.time(),
            client or client_name(),
            project,
            engine,
            operation,
            method,
            delivery,
            raw_openai,
            compact_openai,
            raw_claude,
            compact_claude,
            raw_gemini,
            compact_gemini,
            raw_bytes,
            compact_bytes,
            duration_s,
        )
        with self.connect() as conn:
            conn.execute(
                f"INSERT OR IGNORE INTO events ({','.join(EVENT_COLUMNS)}) VALUES ({','.join('?' for _ in EVENT_COLUMNS)})",
                event,
            )

    def get_stats(self) -> LifetimeStats:
        with self.connect() as conn:
            row = conn.execute("""SELECT COUNT(*), COALESCE(SUM(raw_claude),0),
                COALESCE(SUM(compact_claude),0), COALESCE(SUM(raw_openai),0),
                COALESCE(SUM(compact_openai),0), COALESCE(SUM(raw_gemini),0),
                COALESCE(SUM(compact_gemini),0) FROM events
                WHERE engine='usagetrim' AND delivery != 'prepared'""").fetchone()
        return LifetimeStats(*row)

    def clear(self):
        with self.connect() as conn:
            conn.execute("DELETE FROM events")


def record_text(
    raw: str,
    output: str,
    *,
    operation="exec",
    project=None,
    duration_s=None,
    delivery="returned",
    event_id=None,
    client=None,
    engine="usagetrim",
    offline=False,
) -> None:
    """Best effort telemetry must never make a working tool fail."""
    try:
        from usagetrim.metrics.tokenizer import count_tokens, count_tokens_offline, get_o200k

        if offline:
            before, _ = count_tokens_offline(raw)
            after, method = count_tokens_offline(output)
        else:
            before, after = count_tokens(raw), count_tokens(output)
        if engine == "rtk":
            TelemetryStore().record(
                0,
                raw_openai=(len(raw.encode()) + 3) // 4,
                compact_openai=(len(output.encode()) + 3) // 4,
                engine="rtk",
                method="bytes/4",
                operation=operation,
                project=project_for(project),
                client=client,
                duration_s=duration_s,
                delivery=delivery,
                event_id=event_id,
                raw_bytes=len(raw.encode()),
                compact_bytes=len(output.encode()),
            )
            return
        TelemetryStore().record(
            before.claude,
            after.claude,
            before.openai,
            after.openai,
            before.gemini,
            after.gemini,
            operation=operation,
            project=project_for(project),
            duration_s=duration_s,
            delivery=delivery,
            event_id=event_id,
            client=client,
            engine=engine,
            method=method if offline else get_o200k().name,
            raw_bytes=len(raw.encode("utf-8")),
            compact_bytes=len(output.encode("utf-8")),
        )
    except Exception:
        pass


def recovery_engine(ref_id: str) -> str:
    try:
        path = (
            Path(os.environ.get("USAGETRIM_CACHE_DIR", str(DEFAULT_CACHE_DB.parent))) / "cache.db"
        )
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT source FROM output_cache WHERE ref_id=?", (ref_id,)
            ).fetchone()
        return "rtk" if row and row[0] == "rtk" else "usagetrim"
    except (OSError, sqlite3.Error):
        return "usagetrim"
