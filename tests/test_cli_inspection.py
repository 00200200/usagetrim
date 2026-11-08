"""CLI inspection uses real child results and actual returned CCR content."""

import json
import os
import signal
import socket
import sqlite3
import sys

import pytest
from typer.testing import CliRunner

from usagetrim import cli
from usagetrim.core.cache import ContextCache
from usagetrim.core.telemetry import TelemetryStore
from usagetrim.metrics import tokenizer

runner = CliRunner()


@pytest.fixture(autouse=True)
def deterministic_local_counts(monkeypatch):
    class Encoding:
        name = "o200k_base"

        def encode(self, text, *, disallowed_special=()):
            return list(text.encode("utf-8"))

    encoding = Encoding()
    monkeypatch.setattr(tokenizer, "get_o200k", lambda: encoding)
    monkeypatch.setattr(tokenizer, "get_cl100k", lambda: encoding)
    monkeypatch.setattr(tokenizer, "_O200K_ENC", encoding)
    monkeypatch.setattr(cli, "paused", lambda: False)
    monkeypatch.setattr(
        cli, "select_engine", lambda requested: "none" if requested == "none" else "usagetrim"
    )
    monkeypatch.delenv("USAGETRIM_EVENT_REPLAY", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("CLI inspection tests must stay offline")

    monkeypatch.setattr(socket, "create_connection", forbidden)


def last_event(operation):
    store = TelemetryStore()
    with store.connect() as conn:
        row = conn.execute(
            "SELECT event_id FROM events WHERE operation=? ORDER BY sequence DESC LIMIT 1",
            (operation,),
        ).fetchone()
    assert row is not None
    return store.get_inspection(row[0])


def last_run():
    store = TelemetryStore()
    with store.connect() as conn:
        row = conn.execute(
            "SELECT event_id FROM events WHERE operation LIKE 'exec:%' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    return store.get_inspection(row[0])


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("code,text", [(0, "FAILED in a log"), (7, "PASSED in a log")])
def test_run_records_child_status_without_interpreting_logs(monkeypatch, replay, code, text):
    if replay:
        monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", "1")
    script = (
        f"import os; os.write(1, {text.encode()!r} + b'\\n'); "
        f"os.write(2, b'stderr-line\\n'); raise SystemExit({code})"
    )
    result = runner.invoke(cli.app, ["run", "--quiet", "--", sys.executable, "-c", script])
    assert result.exit_code == code
    assert result.stdout == text + "\nstderr-line\n"
    assert result.stderr == ""
    details = last_run()
    assert details.child_returncode == code
    assert details.termination_reason == "exited"
    assert details.tool_error is None
    assert details.snapshot_state == ("stored" if replay else "disabled")
    if replay:
        assert ContextCache().retrieve(details.before_ref) == result.stdout
        assert ContextCache().retrieve(details.after_ref) == result.stdout
    else:
        assert details.before_ref is None and details.after_ref is None


def test_run_without_replay_performs_no_snapshot_writes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Short unchanged output must not cause snapshot writes")

    monkeypatch.setattr(ContextCache, "store", forbidden)
    result = runner.invoke(cli.app, ["run", "--quiet", "--", sys.executable, "-c", "print('ok')"])
    assert result.exit_code == 0 and result.stdout == "ok\n"
    assert last_run().snapshot_state == "disabled"


def test_run_engine_none_does_not_enable_replay_storage(monkeypatch):
    monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", "1")
    result = runner.invoke(
        cli.app, ["run", "--engine", "none", "--quiet", "--", sys.executable, "-c", "print('ok')"]
    )
    assert result.exit_code == 0 and result.stdout == "ok\n"
    assert last_run().snapshot_state == "cache_disabled"
    assert last_run().child_returncode == 0


@pytest.mark.skipif(os.name != "posix", reason="Negative subprocess return codes are POSIX signals")
def test_run_records_real_signal_without_changing_exit_propagation():
    result = runner.invoke(
        cli.app,
        [
            "run",
            "--quiet",
            "--",
            sys.executable,
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
        ],
    )
    assert result.exit_code == -signal.SIGTERM
    assert last_run().child_returncode == -signal.SIGTERM
    assert last_run().termination_reason == "signaled"


def test_json_replay_links_actual_original_and_emitted_result(monkeypatch):
    monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", "1")
    raw = json.dumps([{"id": value, "message": "diagnostic"} for value in range(10)])
    result = runner.invoke(cli.app, ["json", "--max-items", "1"], input=raw)
    assert result.exit_code == 0
    details = last_event("json")
    assert details.snapshot_state == "stored"
    assert details.before_ref != details.after_ref
    assert ContextCache().retrieve(details.before_ref) == raw
    assert ContextCache().retrieve(details.after_ref) == result.stdout
    assert details.child_returncode is None


@pytest.mark.parametrize("operation", ["cat", "pipe"])
def test_cli_text_transforms_capture_actual_input_and_output(monkeypatch, tmp_path, operation):
    monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", "1")
    raw = "first line\nsecond line\n"
    if operation == "cat":
        source = tmp_path / "input.txt"
        source.write_text(raw, encoding="utf-8")
        result = runner.invoke(cli.app, ["cat", str(source)])
    else:
        result = runner.invoke(cli.app, ["pipe"], input=raw)
    assert result.exit_code == 0
    details = last_event("read" if operation == "cat" else "pipe")
    assert details.snapshot_state == "stored"
    assert ContextCache().retrieve(details.before_ref) == raw
    assert ContextCache().retrieve(details.after_ref) == result.stdout
    assert details.child_returncode is None and details.termination_reason is None


def test_json_no_cache_overrides_replay_and_keeps_stdout(monkeypatch):
    raw = '[{"id":1},{"id":2},{"id":3}]'
    baseline = runner.invoke(cli.app, ["json", "--no-cache", "--max-items", "1"], input=raw)
    assert baseline.exit_code == 0
    assert last_event("json") is None
    monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", "1")

    def forbidden(*args, **kwargs):
        pytest.fail("--no-cache must override replay")

    monkeypatch.setattr(ContextCache, "store", forbidden)
    result = runner.invoke(cli.app, ["json", "--no-cache", "--max-items", "1"], input=raw)
    assert result.exit_code == 0
    assert result.stdout == baseline.stdout and result.stderr == baseline.stderr
    assert last_event("json").snapshot_state == "cache_disabled"


def test_paused_json_does_not_capture_snapshots(monkeypatch):
    monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", "1")
    monkeypatch.setattr(cli, "paused", lambda: True)

    def forbidden(*args, **kwargs):
        pytest.fail("Paused operations must not enable replay writes")

    monkeypatch.setattr(ContextCache, "store", forbidden)
    raw = '[{"id":1},{"id":2}]'
    result = runner.invoke(cli.app, ["json"], input=raw)
    assert result.exit_code == 0 and result.stdout == raw + "\n"
    assert last_event("json").snapshot_state == "cache_disabled"


@pytest.mark.parametrize(
    "options,scope",
    [
        ([], "full"),
        (["--lines", "2-2"], "lines"),
        (["--query", "needle"], "query"),
        (["--query", "needle", "--semantic"], "query"),
    ],
)
@pytest.mark.parametrize("replay", [False, True])
def test_retrieve_records_real_scope_without_synthetic_snapshot_pair(
    monkeypatch, options, scope, replay
):
    if replay:
        monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", "1")
    cache = ContextCache()
    original = "Error: original diagnostic\nneedle here\nlast line\n"
    ref = cache.store(original)
    with sqlite3.connect(cache.db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM output_cache").fetchone()[0]
    result = runner.invoke(cli.app, ["retrieve", ref, *options])
    assert result.exit_code == 0
    if scope == "full":
        assert result.stdout == original
    else:
        assert "needle here" in result.stdout
    details = last_event("retrieve")
    assert details.recovery_ref == ref and details.recovery_scope == scope
    assert details.before_ref is None and details.after_ref is None
    with sqlite3.connect(cache.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM output_cache").fetchone()[0] == count


@pytest.mark.parametrize(
    "options", [["--lines", "bad"], ["--lines", "99"], ["--query", "absentword"]]
)
def test_retrieve_does_not_claim_recovery_for_error_or_empty_selection(options):
    ref = ContextCache().store("existing text\n")
    result = runner.invoke(cli.app, ["retrieve", ref, *options])
    assert result.exit_code == 0
    assert last_event("retrieve") is None


def test_retrieve_missing_ref_does_not_claim_recovery():
    result = runner.invoke(cli.app, ["retrieve", "tc_0000000000000000"])
    assert result.exit_code == 0 and "not found" in result.stdout
    assert last_event("retrieve") is None


def test_retrieve_query_is_not_persisted_in_inspection(monkeypatch):
    monkeypatch.setenv("USAGETRIM_EVENT_REPLAY", "1")
    ref = ContextCache().store("existing diagnostic")
    query = "uniquelyprivatequeryvalue"
    result = runner.invoke(cli.app, ["retrieve", ref, "--query", query])
    assert result.exit_code == 0
    details = last_event("retrieve")
    assert details.recovery_ref is None
    assert details.before_ref is None and details.after_ref is None
    assert query.encode() not in TelemetryStore().db_path.read_bytes()


def test_structured_recovery_methods_preserve_text_and_single_inquiry():
    cache = ContextCache()
    ref = cache.store("needle here\nsecond line\n")
    for name, arguments in (
        ("retrieve", (ref,)),
        ("search", (ref, "needle")),
        ("hybrid_search", (ref, "needle")),
    ):
        structured = getattr(cache, name + "_result")(*arguments)
        assert structured.recovered
        assert structured.text == getattr(cache, name)(*arguments)
    with sqlite3.connect(cache.db_path) as conn:
        assert (
            conn.execute("SELECT hit_count FROM output_cache WHERE ref_id=?", (ref,)).fetchone()[0]
            == 6
        )


def test_retrieve_empty_full_record_is_a_real_recovery():
    cache = ContextCache()
    ref = cache.store("")
    recovered = cache.retrieve_result(ref)
    assert recovered.text == "" and recovered.recovered is True
    assert cache.retrieve(ref) == ""
    result = runner.invoke(cli.app, ["retrieve", ref])
    assert result.exit_code == 0
    # _emit adds a newline only to nonempty text that lacks one.
    assert result.stdout == "" and result.stderr == ""
    details = last_event("retrieve")
    assert details is not None
    assert details.recovery_ref == ref
    assert details.recovery_scope == "full"
    assert details.before_ref is None and details.after_ref is None


def test_semantic_budget_notice_is_not_recovered_content():
    ref = ContextCache().store("needle " + "x" * 2000)
    result = runner.invoke(
        cli.app, ["retrieve", ref, "--query", "needle", "--semantic", "--budget", "96"]
    )
    assert result.exit_code == 0 and "Omitted" in result.stdout
    assert last_event("retrieve") is None


def test_cli_telemetry_failure_preserves_native_output_and_exit_code(monkeypatch):
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("telemetry unavailable")

    monkeypatch.setattr(TelemetryStore, "record", unavailable)
    result = runner.invoke(
        cli.app,
        ["run", "--quiet", "--", sys.executable, "-c", "print('output'); raise SystemExit(7)"],
    )
    assert result.exit_code == 7 and result.stdout == "output\n" and result.stderr == ""
