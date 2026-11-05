"""Transactional logical CCR accounting; no payload scans on the write hot path.

The first migration inspects legacy BLOB lengths without reading their bodies.
Triggers then track legacy stores, artifact publication and eviction in the same
transaction as the underlying change. No connection-local/global usage cache.
"""

from __future__ import annotations

import sqlite3

ARTIFACT_CHARGE = 1024
CHUNK_CHARGE = 256
EXECUTION_CHARGE = 512
OBSERVATION_CHARGE = 256


def _seed_sizes(conn):
    conn.execute("DELETE FROM execution_quota_sizes")
    total = 0
    for rowid, ref_id, kind in conn.execute(
        "SELECT rowid,ref_id,typeof(content) FROM output_cache"
    ):
        size = 0
        if kind in {"blob", "text"}:
            with conn.blobopen("output_cache", "content", rowid, readonly=True) as blob:
                size = len(blob)
        elif kind != "null":
            size = 8
        conn.execute("INSERT INTO execution_quota_sizes VALUES (?,?)", (ref_id, size))
        total += size
    total += conn.execute(
        "SELECT COALESCE(SUM(stored_bytes+chunk_count*?+?),0) FROM execution_artifacts",
        (CHUNK_CHARGE, ARTIFACT_CHARGE),
    ).fetchone()[0]
    total += conn.execute("SELECT COUNT(*) FROM execution_groups").fetchone()[0] * EXECUTION_CHARGE
    total += (
        conn.execute("SELECT COUNT(*) FROM execution_observations").fetchone()[0]
        * OBSERVATION_CHARGE
    )
    return total


def ensure_accounting(conn):
    """Called inside BEGIN IMMEDIATE, after artifact schema migration."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS execution_quota(id INTEGER PRIMARY KEY CHECK(id=1),bytes INTEGER NOT NULL,version INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS execution_quota_sizes(ref_id TEXT PRIMARY KEY,bytes INTEGER NOT NULL)"
    )
    # OLD.content is deliberately never read during eviction. Saved byte lengths
    # avoid materializing old uncompressed TEXT in an AFTER DELETE trigger.
    triggers = {
        "output_insert": """AFTER INSERT ON output_cache BEGIN
            UPDATE execution_quota SET bytes=bytes-COALESCE((SELECT bytes FROM execution_quota_sizes WHERE ref_id=NEW.ref_id),0)+COALESCE(length(CAST(NEW.content AS BLOB)),0) WHERE id=1;
            INSERT OR REPLACE INTO execution_quota_sizes VALUES(NEW.ref_id,COALESCE(length(CAST(NEW.content AS BLOB)),0));
        END""",
        "output_update": """AFTER UPDATE OF content,ref_id ON output_cache BEGIN
            UPDATE execution_quota SET bytes=bytes-COALESCE((SELECT bytes FROM execution_quota_sizes WHERE ref_id=OLD.ref_id),0)+COALESCE(length(CAST(NEW.content AS BLOB)),0) WHERE id=1;
            DELETE FROM execution_quota_sizes WHERE ref_id=OLD.ref_id;
            INSERT INTO execution_quota_sizes VALUES(NEW.ref_id,COALESCE(length(CAST(NEW.content AS BLOB)),0));
        END""",
        "output_delete": """AFTER DELETE ON output_cache BEGIN
            UPDATE execution_quota SET bytes=bytes-COALESCE((SELECT bytes FROM execution_quota_sizes WHERE ref_id=OLD.ref_id),0) WHERE id=1;
            DELETE FROM execution_quota_sizes WHERE ref_id=OLD.ref_id;
        END""",
        "artifact_insert": f"""AFTER INSERT ON execution_artifacts BEGIN
            UPDATE execution_quota SET bytes=bytes+{ARTIFACT_CHARGE} WHERE id=1;
        END""",
        "artifact_update": f"""AFTER UPDATE OF stored_bytes,chunk_count ON execution_artifacts BEGIN
            UPDATE execution_quota SET bytes=bytes+NEW.stored_bytes-OLD.stored_bytes+(NEW.chunk_count-OLD.chunk_count)*{CHUNK_CHARGE} WHERE id=1;
        END""",
        "artifact_delete": f"""AFTER DELETE ON execution_artifacts BEGIN
            UPDATE execution_quota SET bytes=bytes-OLD.stored_bytes-OLD.chunk_count*{CHUNK_CHARGE}-{ARTIFACT_CHARGE} WHERE id=1;
            DELETE FROM execution_chunk_origins WHERE ref_id=OLD.ref_id;
            DELETE FROM execution_observations WHERE ref_id=OLD.ref_id;
            DELETE FROM execution_groups WHERE execution_id=OLD.execution_id AND NOT EXISTS(SELECT 1 FROM execution_artifacts WHERE execution_id=OLD.execution_id);
        END""",
        "group_insert": f"""AFTER INSERT ON execution_groups BEGIN
            UPDATE execution_quota SET bytes=bytes+{EXECUTION_CHARGE} WHERE id=1;
        END""",
        "group_delete": f"""AFTER DELETE ON execution_groups BEGIN
            UPDATE execution_quota SET bytes=bytes-{EXECUTION_CHARGE} WHERE id=1;
            DELETE FROM execution_observations WHERE execution_id=OLD.execution_id;
        END""",
        "observation_insert": f"""AFTER INSERT ON execution_observations BEGIN
            UPDATE execution_quota SET bytes=bytes+{OBSERVATION_CHARGE} WHERE id=1;
            UPDATE execution_groups SET retained_observations=retained_observations+1 WHERE execution_id=NEW.execution_id;
        END""",
        "observation_delete": f"""AFTER DELETE ON execution_observations BEGIN
            UPDATE execution_quota SET bytes=bytes-{OBSERVATION_CHARGE} WHERE id=1;
            UPDATE execution_groups SET retained_observations=retained_observations-1 WHERE execution_id=OLD.execution_id;
        END""",
    }
    for name, body in triggers.items():
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS execution_quota_{name} {body}")
    row = conn.execute("SELECT version FROM execution_quota WHERE id=1").fetchone()
    if row is None:
        total = _seed_sizes(conn)
        conn.execute("INSERT INTO execution_quota VALUES (1,?,1)", (total,))
    elif row != (1,):
        raise sqlite3.DatabaseError("Unsupported execution accounting version")


def usage(conn):
    row = conn.execute("SELECT bytes FROM execution_quota WHERE id=1 AND version=1").fetchone()
    if row is None or type(row[0]) is not int or row[0] < 0:
        raise sqlite3.DatabaseError("Invalid execution accounting")
    return row[0]
