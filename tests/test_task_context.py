import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from typer.testing import CliRunner

from usagetrim.cli import app
from usagetrim.core.cleaner import strip_ansi
from usagetrim.core.companion_state import update_settings
from usagetrim.core.context_hooks import context_hook, install_context_hooks, run_context_hook
from usagetrim.core.monitor import Monitor
from usagetrim.core.task_context import (
    TaskContext,
    clean_task_memory,
    context_path,
    context_summary,
    dispatch_context,
    validate_checkpoint,
)
from usagetrim.core.telemetry import TelemetryStore
from usagetrim.mcp.server import _respond
from usagetrim.metrics.tokenizer import count_tokens


def note(goal="Ship the fix", **extra):
    return {
        "goal": goal,
        "constraints": ["Preserve user edits", "No paid API calls"],
        "next_steps": ["Run affected checks"],
        **extra,
    }


def event(tmp_path, name="SessionStart", **extra):
    return {
        "session_id": "session-1",
        "cwd": str(tmp_path),
        "hook_event_name": name,
        "source": "compact",
        **extra,
    }


@pytest.fixture
def legacy_context(tmp_path):
    path = context_path()
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE checkpoints (
                project TEXT NOT NULL, task TEXT NOT NULL, revision INTEGER NOT NULL,
                updated REAL NOT NULL, payload TEXT NOT NULL, tokens INTEGER NOT NULL,
                PRIMARY KEY(project, task, revision));
            CREATE TABLE lifecycle (
                project TEXT NOT NULL, task TEXT NOT NULL, client TEXT NOT NULL,
                last_hook REAL NOT NULL, last_compact REAL, last_restore REAL,
                prepared_tokens INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(project, task));
        """)
        for revision, goal in ((1, "Legacy initial"), (2, "Legacy latest")):
            payload, tokens = validate_checkpoint(note(goal))
            conn.execute(
                "INSERT INTO checkpoints VALUES (?,?,?,?,?,?)",
                (
                    str(tmp_path),
                    "legacy",
                    revision,
                    revision * 1000.0,
                    payload,
                    tokens,
                ),
            )
        conn.execute(
            "INSERT INTO lifecycle VALUES (?,?,?,?,?,?,?)",
            (
                str(tmp_path),
                "legacy",
                "codex",
                3000.0,
                2500.0,
                3000.0,
                42,
            ),
        )
    return path, str(tmp_path)


def test_legacy_schema_upgrade_preserves_rows_without_backfill(legacy_context):
    path, root = legacy_context
    with sqlite3.connect(path) as conn:
        before = {
            table: conn.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("checkpoints", "lifecycle")
        }
        schemas = conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    store = TaskContext(path)
    TaskContext(path)
    with store.connect() as conn:
        for table, rows in before.items():
            assert [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] == rows
        assert conn.execute("SELECT COUNT(*) FROM task_retention").fetchone()[0] == 0
        assert [row[2] for row in conn.execute("PRAGMA index_info(task_retention_activity)")] == [
            "retention",
            "last_activity",
        ]
        for name, sql in schemas:
            assert (
                conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
                == sql
            )
    snapshot = path.read_bytes()
    read = store.read(root, "legacy", revision=1)
    assert read["retention"] == "permanent" and read["last_activity"] == 2000.0
    assert store.list(root)["tasks"][0]["retention"] == "permanent"
    assert store.read(root, "missing") == {"task": "missing", "found": False}
    assert path.read_bytes() == snapshot


@pytest.mark.parametrize("retention", [None, "ephemeral", "permanent"])
def test_new_task_retention_and_activity(tmp_path, monkeypatch, retention):
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 1000.0)
    store = TaskContext()
    result = store.save(str(tmp_path), "new", note(), 0, retention=retention)
    assert result["retention"] == (retention or "ephemeral")
    assert result["last_activity"] == result["updated"] == 1000.0
    assert result["saved"] and result["revision"] == 1
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM task_retention").fetchone()
    assert dict(row) == {
        "project": str(tmp_path),
        "task": "new",
        "retention": result["retention"],
        "last_activity": 1000.0,
    }


@pytest.mark.parametrize("retention", ["invalid", True, False, 1, 1.5, {}, []])
def test_invalid_retention_has_no_partial_writes(tmp_path, retention):
    store = TaskContext()
    store.save(str(tmp_path), "existing", note(), 0)
    with store.connect() as conn:
        before = {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in ("checkpoints", "task_retention")
        }
    for task, revision in (("new", 0), ("existing", 1)):
        with pytest.raises(ValueError, match="retention"):
            store.save(str(tmp_path), task, note("Changed"), revision, retention=retention)
    with store.connect() as conn:
        for table, rows in before.items():
            assert [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] == rows


@pytest.mark.parametrize("retention", ["ephemeral", "permanent"])
def test_save_without_retention_preserves_existing_class(tmp_path, retention):
    store = TaskContext()
    store.save(str(tmp_path), "one", note(), 0, retention=retention)
    result = store.save(str(tmp_path), "one", note("Updated"), 1)
    assert result["retention"] == retention and result["revision"] == 2
    assert store.read(str(tmp_path), "one")["retention"] == retention


@pytest.mark.parametrize("before,after", [("ephemeral", "permanent"), ("permanent", "ephemeral")])
def test_retention_only_change_does_not_create_checkpoint_revision(tmp_path, before, after):
    store = TaskContext()
    first = store.save(str(tmp_path), "one", note(), 0, retention=before)
    with store.connect() as conn:
        checkpoints = [tuple(row) for row in conn.execute("SELECT * FROM checkpoints")]
    result = store.save(str(tmp_path), "one", note(), 1, retention=after)
    assert result["saved"] is False and result["retention_changed"] is True
    assert result["retention"] == after
    assert result["revision"] == first["revision"] == 1
    assert result["updated"] == first["updated"] and result["tokens"] == first["tokens"]
    with store.connect() as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM checkpoints")] == checkpoints


@pytest.mark.parametrize("change_payload", [False, True])
def test_stale_retention_change_does_not_mutate_task(tmp_path, change_payload):
    store = TaskContext()
    first = store.save(str(tmp_path), "one", note(), 0)
    with pytest.raises(ValueError, match="Revision conflict"):
        store.save(
            str(tmp_path),
            "one",
            note("Changed") if change_payload else note(),
            0,
            retention="permanent",
        )
    assert store.list(str(tmp_path))["tasks"][0]["last_activity"] == first["last_activity"]
    read = store.read(str(tmp_path), "one")
    assert read["retention"] == "ephemeral" and read["revision"] == 1
    assert read["checkpoint"]["goal"] == "Ship the fix"


@pytest.mark.parametrize("retention", [None, "ephemeral"])
def test_idempotent_stale_retry_refreshes_activity_without_moving_it_backwards(
    tmp_path, monkeypatch, retention
):
    store = TaskContext()
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 1000.0)
    first = store.save(str(tmp_path), "one", note(), 0)
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 2000.0)
    retry = store.save(str(tmp_path), "one", note(), 0, retention=retention)
    assert retry["saved"] is False and retry["retention_changed"] is False
    assert retry["revision"] == first["revision"] and retry["updated"] == first["updated"]
    assert retry["last_activity"] == 2000.0
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 500.0)
    assert store.save(str(tmp_path), "one", note(), 0)["last_activity"] == 2000.0
    snapshot = store.path.read_bytes()
    assert store.read(str(tmp_path), "one")["last_activity"] == 2000.0
    assert store.list(str(tmp_path))["tasks"][0]["last_activity"] == 2000.0
    assert store.path.read_bytes() == snapshot


@pytest.mark.parametrize("retention", ["ephemeral", "permanent"])
def test_legacy_task_can_be_explicitly_classified_without_new_revision(legacy_context, retention):
    path, root = legacy_context
    store = TaskContext(path)
    with pytest.raises(ValueError, match="Revision conflict"):
        store.save(root, "legacy", note("Legacy latest"), 1, retention=retention)
    result = store.save(root, "legacy", note("Legacy latest"), 2, retention=retention)
    assert result["retention"] == retention and result["revision"] == 2
    assert result["saved"] is False and result["retention_changed"] is True
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_retention").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 2


@pytest.mark.parametrize("change_payload", [False, True])
def test_legacy_save_without_classification_stays_protected(legacy_context, change_payload):
    path, root = legacy_context
    store = TaskContext(path)
    result = store.save(
        root,
        "legacy",
        note("Changed" if change_payload else "Legacy latest"),
        2 if change_payload else 0,
    )
    assert result["retention"] == "permanent"
    assert result["revision"] == (3 if change_payload else 2)
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_retention").fetchone()[0] == 0


def test_older_client_write_after_upgrade_remains_protected(tmp_path):
    store = TaskContext()
    payload, tokens = validate_checkpoint(note())
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO checkpoints VALUES (?,?,?,?,?,?)",
            (str(tmp_path), "older-client", 1, 1000.0, payload, tokens),
        )
    assert store.read(str(tmp_path), "older-client")["retention"] == "permanent"
    result = store.save(str(tmp_path), "older-client", note("Updated"), 1)
    assert result["retention"] == "permanent"
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_retention").fetchone()[0] == 0


def test_payload_and_retention_change_share_task_metadata(tmp_path, monkeypatch):
    store = TaskContext()
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 1000.0)
    store.save(str(tmp_path), "one", note(), 0)
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 2000.0)
    result = store.save(str(tmp_path), "one", note("Updated"), 1, retention="permanent")
    assert result["saved"] is True and result["retention_changed"] is True
    assert result["revision"] == 2 and result["last_activity"] == 2000.0
    assert store.read(str(tmp_path), "one")["checkpoint"]["goal"] == "Updated"
    previous = store.read(str(tmp_path), "one", revision=1)
    assert previous["updated"] == 1000.0 and previous["last_activity"] == 2000.0
    assert previous["retention"] == "permanent"


def test_failed_metadata_write_rolls_back_checkpoint_update(tmp_path, monkeypatch):
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 1000.0)
    store = TaskContext()
    store.save(str(tmp_path), "one", note(), 0)
    before = store.read(str(tmp_path), "one")
    with store.connect() as conn:
        conn.executescript("""
            CREATE TRIGGER reject_retention BEFORE INSERT ON task_retention
            BEGIN SELECT RAISE(ABORT, 'metadata unavailable'); END;
        """)
    with pytest.raises(sqlite3.IntegrityError, match="metadata unavailable"):
        store.save(str(tmp_path), "one", note("Changed"), 1, retention="permanent")
    assert store.read(str(tmp_path), "one") == before


def test_context_dispatch_forwards_retention_without_new_actions(tmp_path):
    result = dispatch_context(
        {
            "action": "save",
            "root": str(tmp_path),
            "task": "guidelines",
            "checkpoint": note(),
            "expected_revision": 0,
            "retention": "permanent",
        }
    )
    assert result["retention"] == "permanent"


def test_task_listing_retains_limit_order_and_effective_retention(legacy_context, monkeypatch):
    path, root = legacy_context
    store = TaskContext(path)
    for number in range(51):
        monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda n=number: 4000.0 + n)
        store.save(root, f"task-{number}", note(), 0)
    snapshot = path.read_bytes()
    result = store.list(root)
    assert len(result["tasks"]) == 50 and result["has_more"] is True
    assert [task["task"] for task in result["tasks"]] == [f"task-{n}" for n in range(50, 0, -1)]
    assert all(task["retention"] == "ephemeral" for task in result["tasks"])
    assert path.read_bytes() == snapshot


def test_revision_conflicts_history_and_idempotent_retry(tmp_path):
    store = TaskContext()
    root = str(tmp_path)
    first = store.save(root, "one", note(), 0)
    assert first["revision"] == 1
    assert store.save(root, "one", note(), 0)["saved"] is False
    store.save(root, "one", note("Second goal"), 1)
    with pytest.raises(ValueError, match="Revision conflict"):
        store.save(root, "one", note("Stale update"), 1)
    assert store.read(root, "one", 1)["checkpoint"]["goal"] == "Ship the fix"
    assert store.read(root, "one")["checkpoint"]["goal"] == "Second goal"
    for revision in range(2, 24):
        store.save(root, "one", note(str(revision)), revision)
    assert not store.read(root, "one", 1)["found"]
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 20


def test_sessions_and_projects_do_not_restore_each_others_notes(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    store = TaskContext()
    store.save(str(tmp_path), "codex:session-1", note("PRIVATE_TASK"), 0)
    matching = context_hook(event(tmp_path), "codex")
    assert "PRIVATE_TASK" in json.dumps(matching)
    for payload, client in (
        (event(other), "codex"),
        (event(tmp_path, session_id="session-2"), "codex"),
        (event(tmp_path), "claude-code"),
    ):
        assert "PRIVATE_TASK" not in json.dumps(context_hook(payload, client))
    assert "PRIVATE_TASK" not in json.dumps(store.list(str(tmp_path)))


@pytest.mark.parametrize(
    "checkpoint",
    [
        None,
        {},
        {"goal": ""},
        {"goal": True},
        {"goal": "g", "constraints": "text"},
        {"goal": "g", "unknown": "field"},
        {"goal": "g", "references": [False]},
    ],
)
def test_invalid_checkpoint_never_replaces_saved_constraints(tmp_path, checkpoint):
    store = TaskContext()
    store.save(str(tmp_path), "one", note(), 0)
    with pytest.raises(ValueError):
        store.save(str(tmp_path), "one", checkpoint, 1)
    assert store.read(str(tmp_path), "one")["checkpoint"]["constraints"] == note()["constraints"]


def test_oversized_notes_are_rejected_without_truncating_constraints(tmp_path):
    store = TaskContext()
    with pytest.raises(ValueError, match="Nothing saved"):
        store.save(str(tmp_path), "one", note(constraints=["🦄" * 800] * 3), 0)
    assert not store.read(str(tmp_path), "one")["found"]


def test_parallel_updates_cannot_overwrite_a_newer_revision(tmp_path):
    TaskContext().save(str(tmp_path), "one", note(), 0)

    def update(index):
        try:
            return TaskContext().save(str(tmp_path), "one", note(str(index)), 1)["revision"]
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(update, range(4)))
    assert outcomes.count(2) == 1
    assert outcomes.count("conflict") == 3


def test_forget_requires_current_revision_and_removes_all_retained_notes(tmp_path):
    store = TaskContext()
    store.save(str(tmp_path), "one", note("ERASE_ME"), 0)
    store.save(str(tmp_path), "one", note("ERASE_ME_TOO"), 1)
    store.observe(str(tmp_path), "one", "codex")
    with pytest.raises(ValueError):
        store.forget(str(tmp_path), "one", 1)
    assert store.forget(str(tmp_path), "one", 2)["forgotten"]
    assert not store.read(str(tmp_path), "one")["found"]
    with store.connect() as conn:
        for table in ("checkpoints", "lifecycle", "task_retention"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert b"ERASE_ME" not in context_path().read_bytes()


def test_hooks_do_not_open_transcripts_or_save_prompts_or_native_summaries(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("DO_NOT_READ_TRANSCRIPT")
    payload = event(
        tmp_path,
        "PostCompact",
        transcript_path=str(transcript),
        prompt="DO_NOT_STORE_PROMPT",
        compact_summary="DO_NOT_STORE_SUMMARY",
    )
    assert context_hook(payload, "claude-code") == {}
    assert b"DO_NOT_" not in context_path().read_bytes()
    assert transcript.read_text() == "DO_NOT_READ_TRANSCRIPT"
    summary = context_summary([context_path().parent])
    assert summary["last_compact"] is not None
    assert summary["tasks"] == 0


def test_restoration_is_prepared_overhead_not_savings_and_exports_are_private(tmp_path):
    store = TaskContext()
    store.save(str(tmp_path), "codex:session-1", note("PRIVATE_CONTENT"), 0)
    hook = context_hook(event(tmp_path), "codex")
    text = hook["hookSpecificOutput"]["additionalContext"]
    assert "Newer" in text or "newer" in text
    assert count_tokens(text).openai <= 2200
    snapshot = Monitor([context_path().parent], tmp_path / "monitor").dispatch({"method": "export"})
    assert "PRIVATE_CONTENT" not in json.dumps(snapshot)
    assert "session-1" not in json.dumps(snapshot)
    assert snapshot["today"] is None
    assert snapshot["prepared"]["net"] == -count_tokens(text).openai
    assert snapshot["context"]["last_restore"] is not None
    assert b"PRIVATE_CONTENT" not in TelemetryStore().db_path.read_bytes()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"hook_event_name": "UserPromptSubmit"},
        {"hook_event_name": "SessionStart", "source": "clear"},
    ],
)
def test_unsupported_hooks_are_silent_and_do_not_create_state(payload):
    assert context_hook(payload, "codex") == {}
    assert not context_path().exists()


def test_pause_and_unavailable_cache_never_block_conversation(tmp_path, monkeypatch):
    update_settings(paused=True)
    assert context_hook(event(tmp_path), "codex") == {}
    update_settings(paused=False)
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x")
    monkeypatch.setenv("USAGETRIM_CACHE_DIR", str(blocked))
    output = io.StringIO()
    run_context_hook("codex", io.StringIO(json.dumps(event(tmp_path))), output)
    assert json.loads(output.getvalue()) == {}
    output = io.StringIO()
    run_context_hook("codex", io.StringIO("{"), output)
    assert json.loads(output.getvalue()) == {}


@pytest.mark.parametrize("client", ["codex", "claude-code"])
def test_install_preserves_hooks_backs_up_and_is_idempotent(tmp_path, client):
    config = tmp_path / "settings.json"
    existing = {
        "permissions": {"deny": ["Bash(rm:*)"]},
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "existing-hook"}]}]},
    }
    config.write_text(json.dumps(existing))
    executable = tmp_path / "tool with spaces"
    executable.touch()
    install_context_hooks(client, executable, tmp_path / "cache", config)
    first = config.read_bytes()
    install_context_hooks(client, executable, tmp_path / "cache", config)
    assert first == config.read_bytes()
    installed = json.loads(first)
    assert installed["permissions"] == existing["permissions"]
    assert installed["hooks"]["SessionStart"][0] == existing["hooks"]["SessionStart"][0]
    backups = list(tmp_path.glob("*.pre-usagetrim-context-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == existing
    assert backups[0].stat().st_mode & 0o777 == 0o600


def test_install_rejects_malformed_hooks_without_modifying_config(tmp_path):
    config = tmp_path / "settings.json"
    original = '{"hooks":{"PostCompact":{}}}'
    config.write_text(original)
    executable = tmp_path / "usagetrim"
    executable.touch()
    with pytest.raises(ValueError):
        install_context_hooks("codex", executable, tmp_path, config)
    assert config.read_text() == original


def test_reconfigure_cache_replaces_owned_handlers_without_duplicate_injection(tmp_path):
    config = tmp_path / "hooks.json"
    executable = tmp_path / "usagetrim"
    executable.touch()
    install_context_hooks("codex", executable, tmp_path / "old", config)
    install_context_hooks("codex", executable, tmp_path / "new", config)
    data = json.loads(config.read_text())
    for entries in data["hooks"].values():
        assert len(entries) == 1
        assert len(entries[0]["hooks"]) == 1
        assert str(tmp_path / "new") in entries[0]["hooks"][0]["command"]


def test_monitor_handles_missing_corrupt_and_duplicate_sources(tmp_path):
    source = context_path().parent
    assert context_summary([source])["available"] is False
    assert not source.exists()
    TaskContext().save(str(tmp_path), "one", note(), 0)
    assert context_summary([source, source])["tasks"] == 1
    context_path().write_bytes(b"corrupt")
    assert context_summary([source])["issues"]


def test_mcp_and_cli_roundtrip_redaction_and_negative_accounting(tmp_path):
    secret = "ghp_" + "a" * 36
    request = {
        "action": "save",
        "root": str(tmp_path),
        "task": "one",
        "expected_revision": 0,
        "checkpoint": note(secret),
    }
    response = _respond(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "usagetrim_context", "arguments": request},
        }
    )
    assert not response["result"]["isError"]
    read = {"action": "read", "root": str(tmp_path), "task": "one"}
    result = CliRunner().invoke(app, ["context"], input=json.dumps(read))
    assert result.exit_code == 0, result.output
    assert secret not in result.output
    assert "REDACTED_GITHUB_TOKEN" in result.output
    assert context_path().stat().st_mode & 0o777 == 0o600
    assert secret.encode() not in context_path().read_bytes()
    with TelemetryStore().connect() as conn:
        assert conn.execute("SELECT SUM(raw_openai - compact_openai) FROM events").fetchone()[0] < 0


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "save", "root": ".", "task": "t"},
        {"action": "read", "root": "/", "task": "../escape"},
        {"action": "oops", "root": "/", "task": "t"},
    ],
)
def test_invalid_requests_do_not_create_a_store(payload):
    with pytest.raises(ValueError):
        dispatch_context(payload)
    assert not context_path().exists()


@pytest.fixture
def cleanup_store(tmp_path):
    store = TaskContext()
    with store.connect() as conn:
        for task, retention, updated in (
            ("expired", "ephemeral", 1000.0),
            ("young", "ephemeral", 1000000.0),
            ("permanent", "permanent", 1000.0),
            ("legacy", None, 1000.0),
        ):
            for revision in (1, 2):
                conn.execute(
                    "INSERT INTO checkpoints VALUES (?,?,?,?,?,?)",
                    (
                        str(tmp_path),
                        task,
                        revision,
                        updated,
                        json.dumps(
                            note("ERASE_PRIVATE_PAYLOAD" if task == "expired" else "Active task")
                        ),
                        10,
                    ),
                )
            conn.execute(
                "INSERT INTO lifecycle VALUES (?,?,?,?,?,?,?)",
                (str(tmp_path), task, "codex", updated, None, None, 0),
            )
            if retention:
                conn.execute(
                    "INSERT INTO task_retention VALUES (?,?,?,?)",
                    (str(tmp_path), task, retention, updated),
                )
    return store


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("age,expired", [(604740, False), (604800, False), (604801, True)])
def test_cleanup_strict_ttl_boundary(cleanup_store, age, expired, dry_run):
    report = clean_task_memory(path=cleanup_store.path, now=1000 + age, dry_run=dry_run)
    assert [c["task"] for c in report["candidates"]] == (["expired"] if expired else [])
    assert report["deleted_count"] == (int(expired) if not dry_run else 0)


@pytest.mark.parametrize("source", ["checkpoint", "last_hook", "last_restore", "last_compact"])
def test_cleanup_respects_effective_activity_from_older_clients(cleanup_store, source):
    with cleanup_store.connect() as conn:
        if source == "checkpoint":
            conn.execute(
                "INSERT INTO checkpoints SELECT project,task,3,1000000,payload,tokens "
                "FROM checkpoints WHERE task='expired' AND revision=2"
            )
        else:
            conn.execute(f"UPDATE lifecycle SET {source}=1000000 WHERE task='expired'")
    assert clean_task_memory(path=cleanup_store.path, now=1000000)["candidate_count"] == 0


def test_cleanup_dry_run_and_complete_secure_idempotent_delete(cleanup_store):
    path = cleanup_store.path
    before = path.read_bytes()
    mode = path.stat().st_mode
    report = clean_task_memory(path=path, now=1000000, dry_run=True)
    assert path.read_bytes() == before and path.stat().st_mode == mode
    assert report == {
        "dry_run": True,
        "deleted_count": 0,
        "candidate_count": 1,
        "candidates": [
            {
                "project": report["candidates"][0]["project"],
                "task": "expired",
                "retention": "ephemeral",
                "last_activity": 1000.0,
                "revision_count": 2,
                "stored_tokens": 20,
                "age_seconds": 999000.0,
            }
        ],
    }
    result = clean_task_memory(path=path, now=1000000)
    assert result["deleted_count"] == 1 and result["candidates"] == report["candidates"]
    with cleanup_store.connect() as conn:
        for table in ("checkpoints", "lifecycle", "task_retention"):
            assert (
                conn.execute(f"SELECT COUNT(*) FROM {table} WHERE task='expired'").fetchone()[0]
                == 0
            )
        assert {row[0] for row in conn.execute("SELECT DISTINCT task FROM checkpoints")} == {
            "young",
            "permanent",
            "legacy",
        }
    assert b"ERASE_PRIVATE_PAYLOAD" not in path.read_bytes()
    assert clean_task_memory(path=path, now=1000000)["candidate_count"] == 0


@pytest.mark.parametrize("dry_run", [False, True])
def test_cleanup_missing_database_does_not_create_directories(tmp_path, dry_run):
    path = tmp_path / "absent" / "context.db"
    assert clean_task_memory(path=path, dry_run=dry_run) == {
        "dry_run": dry_run,
        "deleted_count": 0,
        "candidate_count": 0,
        "candidates": [],
    }
    assert not path.parent.exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_cleanup_legacy_database_is_protected_without_schema_changes(legacy_context, dry_run):
    path, _ = legacy_context
    before = path.read_bytes()
    assert clean_task_memory(path=path, now=10000000, dry_run=dry_run)["candidate_count"] == 0
    assert path.read_bytes() == before
    with sqlite3.connect(path) as conn:
        assert not conn.execute("PRAGMA table_info(task_retention)").fetchall()


@pytest.mark.parametrize("scoped", [False, True])
def test_cleanup_scope_and_deleted_project_directory(cleanup_store, tmp_path, scoped):
    removed = tmp_path / "removed"
    with cleanup_store.connect() as conn:
        conn.execute(
            "INSERT INTO task_retention VALUES (?,?,?,?)",
            (str(removed), "another", "ephemeral", 1000),
        )
        conn.execute(
            "INSERT INTO checkpoints VALUES (?,?,?,?,?,?)",
            (str(removed), "another", 1, 1000, "PRIVATE", 4),
        )
    report = clean_task_memory(
        path=cleanup_store.path, root=str(removed / ".") if scoped else None, now=1000000
    )
    assert report["deleted_count"] == (1 if scoped else 2)
    assert [c["project"] for c in report["candidates"]] == sorted(
        c["project"] for c in report["candidates"]
    )
    assert cleanup_store.read(str(tmp_path), "expired")["found"] is scoped


@pytest.mark.parametrize("now", [True, False, float("nan"), float("inf"), -float("inf"), "123"])
def test_cleanup_rejects_invalid_clock_without_creating_store(tmp_path, now):
    path = tmp_path / "missing.db"
    with pytest.raises(ValueError, match="now"):
        clean_task_memory(path=path, now=now)
    assert not path.exists()


@pytest.mark.parametrize("root", ["relative", 1, True])
def test_cleanup_rejects_invalid_root(root):
    with pytest.raises(ValueError, match="root"):
        clean_task_memory(root=root)


def test_cleanup_redacts_report_but_deletes_raw_keys(cleanup_store):
    secret = "ghp_" + "a" * 36
    with cleanup_store.connect() as conn:
        for table in ("checkpoints", "lifecycle", "task_retention"):
            conn.execute(
                f"UPDATE {table} SET project=?, task=? WHERE task='expired'",
                ("/projects/" + secret, secret),
            )
    report = clean_task_memory(path=cleanup_store.path, now=1000000)
    assert secret not in json.dumps(report) and "REDACTED" in json.dumps(report)
    assert report["deleted_count"] == 1
    with cleanup_store.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM checkpoints WHERE task=?", (secret,)).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("missing", ["lifecycle", "task_retention"])
def test_cleanup_incomplete_schema_is_fail_safe(cleanup_store, missing):
    with cleanup_store.connect() as conn:
        conn.execute(f"DROP TABLE {missing}")
    before = cleanup_store.path.read_bytes()
    for dry_run in (True, False):
        assert (
            clean_task_memory(path=cleanup_store.path, now=1000000, dry_run=dry_run)[
                "candidate_count"
            ]
            == 0
        )
        assert cleanup_store.path.read_bytes() == before


def test_cleanup_rolls_back_all_deletes_on_failure(cleanup_store):
    with cleanup_store.connect() as conn:
        conn.executescript("""
            CREATE TRIGGER reject_cleanup BEFORE DELETE ON task_retention
            BEGIN SELECT RAISE(ABORT, 'cleanup unavailable'); END;
        """)
        before = {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in ("checkpoints", "lifecycle", "task_retention")
        }
    with pytest.raises(sqlite3.IntegrityError, match="cleanup unavailable"):
        clean_task_memory(path=cleanup_store.path, now=1000000)
    with cleanup_store.connect() as conn:
        for table, rows in before.items():
            assert [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] == rows


def test_cleanup_selects_after_writer_reactivates_task(cleanup_store, monkeypatch):
    import threading

    import usagetrim.core.task_context as module

    writer = sqlite3.connect(cleanup_store.path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE lifecycle SET last_hook=1000000 WHERE task='expired'")
    waiting = threading.Event()
    connect = sqlite3.connect

    def observed_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(
            lambda statement: waiting.set() if statement == "BEGIN IMMEDIATE" else None
        )
        return conn

    monkeypatch.setattr(module.sqlite3, "connect", observed_connect)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(clean_task_memory, path=cleanup_store.path, now=1000000)
            try:
                assert waiting.wait(timeout=5), "Cleanup did not request the write lock"
            finally:
                writer.commit()
            assert future.result(timeout=5)["candidate_count"] == 0
    finally:
        writer.close()


def test_cleanup_protects_permanent_and_legacy_after_one_hundred_days(cleanup_store):
    report = clean_task_memory(path=cleanup_store.path, now=1000 + 100 * 86400)
    assert {candidate["task"] for candidate in report["candidates"]} == {"expired", "young"}
    with cleanup_store.connect() as conn:
        assert {row[0] for row in conn.execute("SELECT DISTINCT task FROM checkpoints")} == {
            "permanent",
            "legacy",
        }


@pytest.mark.parametrize("revision", [None, 1])
def test_read_touches_entire_task_and_never_moves_activity_backwards(
    tmp_path, monkeypatch, revision
):
    store = TaskContext()
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 1000.0)
    store.save(str(tmp_path), "one", note(), 0)
    store.save(str(tmp_path), "one", note("Second"), 1)
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 2000.0)
    result = store.read(str(tmp_path), "one", revision)
    assert result["last_activity"] == 2000.0
    assert result["revision"] == (revision or 2)
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 500.0)
    assert store.read(str(tmp_path), "one", revision)["last_activity"] == 2000.0
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 3000.0)
    assert store.read(str(tmp_path), "one", 99) == {"task": "one", "found": False}
    assert store.list(str(tmp_path))["tasks"][0]["last_activity"] == 2000.0
    assert store.read(str(tmp_path), "missing") == {"task": "missing", "found": False}
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_retention").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM lifecycle").fetchone()[0] == 0


@pytest.mark.parametrize("retention", ["ephemeral", "permanent"])
def test_observe_touches_existing_metadata_without_changing_retention(
    tmp_path, monkeypatch, retention
):
    store = TaskContext()
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 1000.0)
    store.save(str(tmp_path), "one", note(), 0, retention=retention)
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 2000.0)
    store.observe(str(tmp_path), "one", "codex", compact=True, restored=True, tokens=7)
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 500.0)
    store.observe(str(tmp_path), "one", "codex", tokens=3)
    with store.connect() as conn:
        metadata = conn.execute("SELECT * FROM task_retention").fetchone()
        lifecycle = conn.execute("SELECT * FROM lifecycle").fetchone()
    assert metadata["retention"] == retention and metadata["last_activity"] == 2000.0
    assert lifecycle["last_hook"] == 500.0
    assert lifecycle["last_restore"] == lifecycle["last_compact"] == 2000.0
    assert lifecycle["prepared_tokens"] == 10


def test_observe_legacy_checkpoint_stays_protected(legacy_context):
    path, root = legacy_context
    store = TaskContext(path)
    store.observe(root, "legacy", "codex")
    assert store.read(root, "legacy")["retention"] == "permanent"
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_retention").fetchone()[0] == 0


def test_observe_lifecycle_only_task_can_expire(tmp_path, monkeypatch):
    store = TaskContext()
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 1000.0)
    store.observe(str(tmp_path), "codex:session", "codex")
    assert store.read(str(tmp_path), "codex:session")["found"] is False
    report = clean_task_memory(path=store.path, now=1000 + 604801)
    assert report["deleted_count"] == 1
    candidate = report["candidates"][0]
    assert candidate["retention"] == "ephemeral"
    assert candidate["revision_count"] == candidate["stored_tokens"] == 0
    with store.connect() as conn:
        for table in ("checkpoints", "lifecycle", "task_retention"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_observe_metadata_failure_rolls_back_lifecycle(tmp_path):
    store = TaskContext()
    with store.connect() as conn:
        conn.executescript("""
            CREATE TRIGGER reject_touch BEFORE INSERT ON task_retention
            BEGIN SELECT RAISE(ABORT, 'touch unavailable'); END;
        """)
    with pytest.raises(sqlite3.IntegrityError, match="touch unavailable"):
        store.observe(str(tmp_path), "one", "codex")
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM lifecycle").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_retention").fetchone()[0] == 0


@pytest.mark.parametrize("legacy", [False, True])
def test_session_start_hook_retention_without_hook_changes(tmp_path, legacy):
    store = TaskContext()
    if legacy:
        payload, tokens = validate_checkpoint(note())
        with store.connect() as conn:
            conn.execute(
                "INSERT INTO checkpoints VALUES (?,?,?,?,?,?)",
                (str(tmp_path), "codex:session-1", 1, 1000, payload, tokens),
            )
    result = context_hook(event(tmp_path), "codex")
    assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    with store.connect() as conn:
        metadata = conn.execute("SELECT * FROM task_retention").fetchone()
        assert conn.execute("SELECT COUNT(*) FROM lifecycle").fetchone()[0] == 1
    if legacy:
        assert metadata is None
    else:
        assert metadata["retention"] == "ephemeral"


@pytest.mark.parametrize("operation", ["save", "observe"])
def test_automatic_cleanup_is_after_commit_and_project_scoped(
    cleanup_store, tmp_path, monkeypatch, operation
):
    store = cleanup_store
    other = tmp_path / "other"
    other.mkdir()
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO task_retention VALUES (?,?,?,?)",
            (str(other), "expired-other", "ephemeral", 1000),
        )
        conn.execute(
            "INSERT INTO checkpoints VALUES (?,?,?,?,?,?)",
            (str(other), "expired-other", 1, 1000, json.dumps(note()), 10),
        )
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 1000000.0)
    if operation == "save":
        result = store.save(str(tmp_path), "young", note("Active"), 2)
        assert result["saved"] and result["last_activity"] == 1000000.0
    else:
        store.observe(str(tmp_path), "young", "codex")
    with store.connect() as conn:
        assert {row[0] for row in conn.execute("SELECT DISTINCT task FROM checkpoints")} == {
            "young",
            "permanent",
            "legacy",
            "expired-other",
        }
        assert (
            conn.execute("SELECT last_activity FROM task_retention WHERE task='young'").fetchone()[
                0
            ]
            == 1000000.0
        )


@pytest.mark.parametrize("operation", ["save", "observe"])
def test_current_expired_task_is_reactivated_before_automatic_cleanup(
    cleanup_store, tmp_path, monkeypatch, operation
):
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 1000000.0)
    if operation == "save":
        result = cleanup_store.save(str(tmp_path), "expired", note("Reactivated"), 2)
        assert result["revision"] == 3
    else:
        cleanup_store.observe(str(tmp_path), "expired", "codex", restored=True)
    assert cleanup_store.read(str(tmp_path), "expired")["found"] is True
    assert clean_task_memory(path=cleanup_store.path, now=1000000)["candidate_count"] == 0


@pytest.mark.parametrize("operation", ["save", "observe"])
@pytest.mark.parametrize(
    "error", [sqlite3.OperationalError("database is locked"), OSError("unavailable")]
)
def test_automatic_cleanup_failure_does_not_undo_committed_activity(
    tmp_path, monkeypatch, operation, error
):
    import usagetrim.core.task_context as module

    store = TaskContext()
    monkeypatch.setattr(module.time, "time", lambda: 1000.0)
    calls = []

    def fail_cleanup(**kwargs):
        calls.append(kwargs)
        with sqlite3.connect(store.path) as conn:
            assert conn.execute("SELECT last_activity FROM task_retention").fetchone()[0] == 1000.0
            table = "checkpoints" if operation == "save" else "lifecycle"
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
        raise error

    monkeypatch.setattr(module, "_clean_task_memory", fail_cleanup)
    if operation == "save":
        assert store.save(str(tmp_path), "one", note(), 0)["saved"] is True
    else:
        store.observe(str(tmp_path), "one", "codex")
    assert calls == [{"root": str(tmp_path), "path": store.path, "timeout": 0}]


def test_gc_failure_does_not_break_native_hook(tmp_path, monkeypatch):
    import usagetrim.core.task_context as module

    def fail_cleanup(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(module, "_clean_task_memory", fail_cleanup)
    output = io.StringIO()
    run_context_hook("codex", io.StringIO(json.dumps(event(tmp_path))), output)
    assert json.loads(output.getvalue())["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_read_list_summary_and_constructor_do_not_trigger_cleanup(
    cleanup_store, tmp_path, monkeypatch
):
    import usagetrim.core.task_context as module

    def unexpected_cleanup(**kwargs):
        pytest.fail("Read-only maintenance must not trigger cleanup")

    monkeypatch.setattr(module, "_clean_task_memory", unexpected_cleanup)
    store = TaskContext(cleanup_store.path)
    assert store.read(str(tmp_path), "young")["found"]
    store.list(str(tmp_path))
    context_summary([store.path.parent])
    with store.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM checkpoints WHERE task='expired'").fetchone()[0] == 2
        )


@pytest.mark.parametrize("operation", ["save", "observe"])
def test_automatic_cleanup_skips_actual_sqlite_writer_lock(tmp_path, monkeypatch, operation):
    import usagetrim.core.task_context as module

    store = TaskContext()
    clean = module._clean_task_memory
    lock_errors = []

    def locked_cleanup(**kwargs):
        with sqlite3.connect(store.path) as writer:
            writer.execute("BEGIN IMMEDIATE")
            try:
                return clean(**kwargs)
            except sqlite3.OperationalError as error:
                lock_errors.append(str(error))
                raise

    monkeypatch.setattr(module, "_clean_task_memory", locked_cleanup)
    if operation == "save":
        assert store.save(str(tmp_path), "one", note(), 0)["saved"] is True
    else:
        store.observe(str(tmp_path), "one", "codex")
    assert lock_errors == ["database is locked"]
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_retention").fetchone()[0] == 1


def test_automatic_cleanup_does_not_hide_programmer_errors(tmp_path, monkeypatch):
    import usagetrim.core.task_context as module

    store = TaskContext()

    def broken_cleanup(**kwargs):
        raise TypeError("unexpected implementation error")

    monkeypatch.setattr(module, "_clean_task_memory", broken_cleanup)
    with pytest.raises(TypeError, match="implementation error"):
        store._prune_best_effort(str(tmp_path))


def test_first_checkpoint_preserves_existing_lifecycle_task_retention(tmp_path):
    store = TaskContext()
    store.observe(str(tmp_path), "one", "codex")
    with store.connect() as conn:
        conn.execute("UPDATE task_retention SET retention='permanent'")
    result = store.save(str(tmp_path), "one", note(), 0)
    assert result["saved"] is True and result["retention"] == "permanent"


@pytest.mark.parametrize("profile", ["full", "coding", "desktop"])
def test_context_retention_schema_is_optional_in_all_profiles(profile):
    from usagetrim.mcp.server import tool_definitions

    tool = next(tool for tool in tool_definitions(profile) if tool["name"] == "usagetrim_context")
    schema = tool["inputSchema"]
    retention = schema["properties"]["retention"]
    assert retention["type"] == "string"
    assert retention["enum"] == ["permanent", "ephemeral"]
    assert "retention" not in schema["required"]
    assert schema["properties"]["action"]["enum"] == ["save", "read", "list", "forget"]
    assert all(word in retention["description"] for word in ("ephemeral", "permanent", "save"))
    if profile == "full":
        assert "default" in retention["description"] and "preserve" in retention["description"]


def context_tool_call(arguments):
    response = _respond(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "usagetrim_context", "arguments": arguments},
        }
    )
    return response["result"]


@pytest.mark.parametrize("retention", [None, "permanent"])
def test_mcp_retention_save_read_and_preserve_class(tmp_path, retention):
    request = {
        "action": "save",
        "root": str(tmp_path),
        "task": "one",
        "expected_revision": 0,
        "checkpoint": note(),
    }
    if retention:
        request["retention"] = retention
    result = context_tool_call(request)
    assert not result.get("isError", False)
    assert json.loads(result["content"][0]["text"])["retention"] == (retention or "ephemeral")
    request.pop("retention", None)
    request.update(checkpoint=note("Updated"), expected_revision=1)
    result = context_tool_call(request)
    assert not result.get("isError", False)
    assert json.loads(result["content"][0]["text"])["retention"] == (retention or "ephemeral")
    read = context_tool_call({"action": "read", "root": str(tmp_path), "task": "one"})
    assert json.loads(read["content"][0]["text"])["retention"] == (retention or "ephemeral")


def test_mcp_invalid_retention_has_no_partial_writes(tmp_path):
    result = context_tool_call(
        {
            "action": "save",
            "root": str(tmp_path),
            "task": "one",
            "checkpoint": note(),
            "expected_revision": 0,
            "retention": "forever",
        }
    )
    assert result["isError"]
    with TaskContext().connect() as conn:
        for table in ("checkpoints", "task_retention"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_memory_cli_help():
    runner = CliRunner()
    result = runner.invoke(app, ["memory", "--help"], terminal_width=160)
    assert result.exit_code == 0 and "clean" in strip_ansi(result.output)
    result = runner.invoke(app, ["memory", "clean", "--help"], terminal_width=160)
    assert result.exit_code == 0
    output = strip_ansi(result.output)
    assert "--dry-run" in output and "--root" in output


@pytest.mark.parametrize("dry_run", [True, False])
def test_memory_cli_cleanup_reports_candidates_without_payloads(
    cleanup_store, monkeypatch, dry_run
):
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 1000000.0)
    before = cleanup_store.path.read_bytes()
    result = CliRunner().invoke(app, ["memory", "clean"] + (["--dry-run"] if dry_run else []))
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["dry_run"] is dry_run
    assert report["candidate_count"] == 1
    assert report["deleted_count"] == (0 if dry_run else 1)
    assert report["candidates"][0]["task"] == "expired"
    assert "ERASE_PRIVATE_PAYLOAD" not in result.output
    assert not set(report["candidates"][0]) & {
        "goal",
        "payload",
        "constraints",
        "progress",
        "references",
    }
    if dry_run:
        assert cleanup_store.path.read_bytes() == before
    with cleanup_store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM checkpoints WHERE task='expired'").fetchone()[0]
    assert count == (2 if dry_run else 0)


@pytest.mark.parametrize("dry_run", [True, False])
def test_memory_cli_scopes_to_deleted_project_path(cleanup_store, tmp_path, monkeypatch, dry_run):
    removed = str(tmp_path / "deleted-project")
    with cleanup_store.connect() as conn:
        conn.execute(
            "INSERT INTO task_retention VALUES (?,?,?,?)", (removed, "removed", "ephemeral", 1000)
        )
        conn.execute(
            "INSERT INTO checkpoints VALUES (?,?,?,?,?,?)",
            (removed, "removed", 1, 1000, "PRIVATE", 10),
        )
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 1000000.0)
    result = CliRunner().invoke(
        app, ["memory", "clean", "--root", removed] + (["--dry-run"] if dry_run else [])
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["candidate_count"] == 1
    assert report["candidates"][0]["project"] == removed
    with cleanup_store.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM checkpoints WHERE task='expired'").fetchone()[0] == 2
        )
        assert conn.execute("SELECT COUNT(*) FROM checkpoints WHERE task='removed'").fetchone()[
            0
        ] == int(dry_run)


def test_memory_cli_dry_run_without_database_creates_nothing():
    path = context_path()
    assert not path.parent.exists()
    result = CliRunner().invoke(app, ["memory", "clean", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "dry_run": True,
        "candidate_count": 0,
        "deleted_count": 0,
        "candidates": [],
    }
    assert not path.parent.exists()


def test_memory_cli_redacts_report_and_deletes_raw_keys(cleanup_store, monkeypatch):
    secret = "ghp_" + "a" * 36
    with cleanup_store.connect() as conn:
        for table in ("checkpoints", "lifecycle", "task_retention"):
            conn.execute(
                f"UPDATE {table} SET project=?,task=? WHERE task='expired'",
                ("/projects/" + secret, secret),
            )
    monkeypatch.setattr("usagetrim.core.task_context.time.time", lambda: 1000000.0)
    result = CliRunner().invoke(app, ["memory", "clean"])
    assert result.exit_code == 0 and secret not in result.output
    assert "REDACTED" in result.output
    assert json.loads(result.output)["deleted_count"] == 1
    with cleanup_store.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM task_retention WHERE task=?", (secret,)).fetchone()[
                0
            ]
            == 0
        )


def test_memory_cli_rejects_relative_root_without_creating_storage():
    result = CliRunner().invoke(app, ["memory", "clean", "--root", "relative/path"])
    assert result.exit_code == 2 and "absolute" in result.output
    assert not context_path().exists()


@pytest.mark.parametrize("error_type", [OSError, sqlite3.OperationalError, ValueError])
def test_memory_cli_redacts_expected_errors(monkeypatch, error_type):
    import usagetrim.core.task_context as module

    secret = "ghp_" + "a" * 36

    def fail_cleanup(**kwargs):
        raise error_type("Cannot clean " + secret)

    monkeypatch.setattr(module, "clean_task_memory", fail_cleanup)
    result = CliRunner().invoke(app, ["memory", "clean"])
    assert result.exit_code == 2
    assert secret not in result.output and "REDACTED" in result.output
