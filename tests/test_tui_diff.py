"""Headless integration of real inspection metadata and bounded event diffs."""

import asyncio
import hashlib
import sqlite3
import threading

import pytest

pytest.importorskip("textual")

from textual.containers import VerticalScroll
from textual.widgets import DataTable, Input, Static

from tui_support import press, settle
from usagetrim.core.cache import ContextCache
from usagetrim.core.event_replay import EventInspection
from usagetrim.core.telemetry import TelemetryStore
from usagetrim.tui import app as dashboard
from usagetrim.tui import diff_screen
from usagetrim.tui.app import UsageTrimDashboard
from usagetrim.tui.diff_screen import DiffScreen
from usagetrim.tui.history import HistoryFilters, read_history, read_history_inspections


def pair(store, cache, event_id, before="old\n", after="new\n", **status):
    store.record(
        100,
        40,
        raw_openai=100,
        compact_openai=40,
        event_id=event_id,
        operation="read",
        inspection=EventInspection(
            before_ref=cache.store(before),
            after_ref=cache.store(after),
            snapshot_state="stored",
            **status,
        ),
    )


def setup(tmp_path):
    return TelemetryStore(tmp_path / "telemetry.db"), ContextCache(tmp_path / "cache.db")


async def signaled(event):
    assert await asyncio.to_thread(event.wait, 3), "Worker did not reach its synchronization point"


def test_history_statuses_and_separate_recovery_details(tmp_path):
    store, cache = setup(tmp_path)
    for key, metadata in (
        ("pass", EventInspection(child_returncode=0, termination_reason="exited")),
        ("fail", EventInspection(child_returncode=7, termination_reason="exited")),
        ("timeout", EventInspection(termination_reason="timed_out")),
        ("historical", None),
        ("full", EventInspection(recovery_ref="tc_0123456789abcdef", recovery_scope="full")),
        ("lines", EventInspection(recovery_ref="tc_0123456789abcdef", recovery_scope="lines")),
        ("query", EventInspection(recovery_ref="tc_0123456789abcdef", recovery_scope="query")),
    ):
        store.record(100, 10, event_id=key, operation="read", inspection=metadata)
    store.record(1, event_id="prepared", delivery="prepared")
    store.record(1, event_id="other-engine", engine="rtk")

    async def scenario():
        app = UsageTrimDashboard(store, cache=cache)
        async with app.run_test(size=(120, 45)) as pilot:
            await settle(pilot)
            table = app.query_one("#history", DataTable)
            for key, expected in (
                ("pass", "Pass (exit 0)"),
                ("fail", "Fail (exit 7)"),
                ("timeout", "Timed out"),
                ("historical", "Not recorded"),
            ):
                assert str(table.get_row(key)[-1]) == expected
            assert "prepared" not in app._history and "other-engine" not in app._history
            for key, text in (
                ("full", "Recovered (full redacted content)"),
                ("lines", "Retrieved line range"),
                ("query", "Retrieved search results"),
            ):
                table.move_cursor(row=list(app._history).index(key))
                await settle(pilot)
                rendered = str(app.query_one("#details", Static).render())
                assert f"Recovery: {text}" in rendered
                assert "Delivery: Returned" in rendered and 'event_id: "' + key + '"' in rendered
                assert "Execution: Not recorded" in rendered

    asyncio.run(scenario())


def test_real_diff_is_plain_text_and_has_no_telemetry_or_hit_side_effects(tmp_path, monkeypatch):
    store, cache = setup(tmp_path)
    pair(store, cache, "event", "[bold]old[/bold]\n", "[bold]new[/bold]\n")

    def forbidden(*args, **kwargs):
        pytest.fail("Viewer must never use unbounded retrieve")

    monkeypatch.setattr(ContextCache, "retrieve", forbidden)

    async def scenario():
        app = UsageTrimDashboard(store, cache=cache)
        async with app.run_test(size=(100, 35)) as pilot:
            await settle(pilot)
            await press(pilot, "enter")
            await settle(pilot)
            assert isinstance(app.screen, DiffScreen)
            body = str(app.screen.query_one("#diff-body", Static).render())
            assert "--- before" in body and "+++ after" in body
            assert "-[bold]old[/bold]" in body and "+[bold]new[/bold]" in body
            assert "Complete comparison" in str(
                app.screen.query_one("#diff-status", Static).render()
            )
            await press(pilot, "escape")
            await settle(pilot)
            assert app.query_one("#history", DataTable).has_focus
        assert store.get_stats().total_runs == 1
        with sqlite3.connect(cache.db_path) as conn:
            assert conn.execute("SELECT SUM(hit_count) FROM output_cache").fetchone()[0] == 0

    asyncio.run(scenario())


def test_diff_redacts_legacy_secrets_and_loads_outside_ui_thread(tmp_path, monkeypatch):
    store, cache = setup(tmp_path)
    pair(store, cache, "event")
    inspection = store.get_inspection("event")
    legacy = "ghp_\x1b[31m" + "A" * 36 + "\n"
    digest = hashlib.sha256(legacy.encode("utf-8")).hexdigest()
    legacy_ref = "tc_" + digest[:16]
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute(
            "UPDATE output_cache SET ref_id=?,content_hash=?,content=? WHERE ref_id=?",
            (legacy_ref, digest, legacy, inspection.before_ref),
        )
    with store.connect() as conn:
        conn.execute(
            "UPDATE event_inspection SET before_ref=? WHERE event_id='event'", (legacy_ref,)
        )
    reader = diff_screen.load_event_replay
    called = threading.Event()

    def checked(*args, **kwargs):
        assert threading.get_ident() != threading.main_thread().ident
        called.set()
        return reader(*args, **kwargs)

    monkeypatch.setattr(diff_screen, "load_event_replay", checked)

    async def scenario():
        app = UsageTrimDashboard(store, cache=cache)
        async with app.run_test() as pilot:
            await settle(pilot)
            await press(pilot, "enter")
            await settle(pilot)
            assert called.is_set()
            text = str(app.screen.query_one("#diff-body", Static).render())
            assert "REDACTED" in text and "A" * 36 not in text and "\x1b" not in text

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case", ["identical", "empty", "evicted", "corrupt", "integrity", "event-deleted", "historical"]
)
def test_diff_screen_handles_unavailable_and_identical_data(tmp_path, case):
    store, cache = setup(tmp_path)
    if case == "historical":
        store.record(10, event_id="event")
    else:
        pair(
            store,
            cache,
            "event",
            "" if case == "empty" else "same",
            "" if case == "empty" else "same",
        )

    async def scenario():
        app = UsageTrimDashboard(store, cache=cache)
        async with app.run_test(size=(70, 30)) as pilot:
            await settle(pilot)
            if case in {"evicted", "corrupt", "integrity"}:
                with sqlite3.connect(cache.db_path) as conn:
                    statement = {
                        "evicted": "DELETE FROM output_cache",
                        "corrupt": "UPDATE output_cache SET content=x'ff'",
                        "integrity": "UPDATE output_cache SET content='modified valid UTF-8'",
                    }[case]
                    conn.execute(statement)
            if case == "event-deleted":
                store.clear()
            await press(pilot, "enter")
            await settle(pilot)
            assert isinstance(app.screen, DiffScreen)
            status = str(app.screen.query_one("#diff-status", Static).render())
            expected = {
                "identical": "No differences",
                "empty": "No differences",
                "evicted": "missing or were evicted",
                "corrupt": "damaged",
                "integrity": "integrity could not be verified",
                "event-deleted": "no longer exists",
                "historical": "not recorded",
            }[case]
            assert expected in status
            assert str(app.screen.query_one("#diff-body", Static).render()) == ""
            await press(pilot, "escape")
            await settle(pilot)
            if case == "event-deleted":
                assert app._selected_event_id is None

    asyncio.run(scenario())


def test_diff_return_preserves_filters_drafts_selection_and_cursor(tmp_path):
    store, cache = setup(tmp_path)
    for key in ("first", "second", "third"):
        pair(store, cache, key)

    async def scenario():
        app = UsageTrimDashboard(store, cache=cache)
        async with app.run_test(size=(120, 45)) as pilot:
            await settle(pilot)
            app.query_one("#operation-filter", Input).value = "read"
            app.action_apply_filters()
            await settle(pilot)
            await press(pilot, "down")
            await settle(pilot)
            table = app.query_one("#history", DataTable)
            selected, cursor, filters = app._selected_event_id, table.cursor_coordinate, app.filters
            app.query_one("#client-filter", Input).value = "unapplied draft"
            await press(pilot, "enter")
            await settle(pilot)
            await press(pilot, "escape")
            await settle(pilot)
            assert app.filters == filters and app._selected_event_id == selected
            assert table.cursor_coordinate == cursor and table.has_focus
            assert app.query_one("#operation-filter", Input).value == "read"
            assert app.query_one("#client-filter", Input).value == "unapplied draft"
            await press(pilot, "j")
            await settle(pilot)
            assert table.cursor_row == cursor.row + 1
            await press(pilot, "k")
            await settle(pilot)
            assert table.cursor_coordinate == cursor

    asyncio.run(scenario())


def test_refresh_timer_updates_status_without_resetting_selection(tmp_path, monkeypatch):
    store, cache = setup(tmp_path)
    pair(store, cache, "event", child_returncode=0, termination_reason="exited")
    app = UsageTrimDashboard(store, cache=cache)
    timers = []
    monkeypatch.setattr(
        app, "set_interval", lambda seconds, callback: timers.append((seconds, callback))
    )

    async def scenario():
        async with app.run_test() as pilot:
            await settle(pilot)
            assert timers == [(5, app.refresh_dashboard)]
            with store.connect() as conn:
                conn.execute(
                    "UPDATE event_inspection SET child_returncode=7 WHERE event_id='event'"
                )
            timers[0][1]()
            await settle(pilot)
            assert str(app.query_one("#history", DataTable).get_row("event")[-1]) == "Fail (exit 7)"
            assert app._selected_event_id == "event"

    asyncio.run(scenario())


def test_diff_worker_discards_previous_target_and_keeps_ui_responsive(tmp_path, monkeypatch):
    store, cache = setup(tmp_path)
    pair(store, cache, "blocked", "old first\n", "new first\n")
    pair(store, cache, "latest", "old second\n", "new second\n")
    started, release = threading.Event(), threading.Event()
    original = diff_screen.load_event_replay

    def blocking(store, event_id, **kwargs):
        if event_id == "blocked":
            started.set()
            assert release.wait(3)
        return original(store, event_id, **kwargs)

    monkeypatch.setattr(diff_screen, "load_event_replay", blocking)

    async def scenario():
        app = UsageTrimDashboard(store, cache=cache)
        try:
            async with app.run_test() as pilot:
                await settle(pilot)
                app.query_one("#history", DataTable).move_cursor(
                    row=list(app._history).index("blocked")
                )
                await settle(pilot)
                await press(pilot, "enter")
                await signaled(started)
                view = app.screen
                assert view._diff_lane.busy
                # UI work can change the requested target while the read is blocked.
                view.show_event("latest")
                assert view.event_id == "latest" and view._diff_lane._pending is not None
                release.set()
                await settle(pilot)
                body = str(view.query_one("#diff-body", Static).render())
                assert "second" in body and "first" not in body
        finally:
            release.set()

    asyncio.run(scenario())


def test_diff_closes_without_waiting_for_blocked_io(tmp_path, monkeypatch):
    store, cache = setup(tmp_path)
    pair(store, cache, "event")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = diff_screen.load_event_replay

    def blocking(*args, **kwargs):
        started.set()
        try:
            assert release.wait(3)
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(diff_screen, "load_event_replay", blocking)

    async def scenario():
        app = UsageTrimDashboard(store, cache=cache)
        try:
            async with app.run_test() as pilot:
                await settle(pilot)
                await press(pilot, "enter")
                await signaled(started)
                view = app.screen
                await press(pilot, "escape")
                assert not release.is_set() and not isinstance(app.screen, DiffScreen)
                assert view._diff_lane.closed
                release.set()
                await signaled(finished)
                await settle(pilot)
                assert app.query_one("#history", DataTable).has_focus
        finally:
            release.set()

    asyncio.run(scenario())


def test_small_terminal_scroll_and_worker_failure_are_safe(tmp_path, monkeypatch):
    store, cache = setup(tmp_path)
    pair(store, cache, "event", "old\n" * 100, "new\n" * 100)

    async def scenario():
        app = UsageTrimDashboard(store, cache=cache)
        async with app.run_test(size=(50, 20)) as pilot:
            await settle(pilot)
            await press(pilot, "enter")
            await settle(pilot)
            scroll = app.screen.query_one("#diff-scroll", VerticalScroll)
            scroll.scroll_end(animate=False)
            await pilot.pause()
            assert scroll.scroll_y > 0
            await press(pilot, "escape")
            await settle(pilot)

            def fail(*args, **kwargs):
                raise sqlite3.OperationalError("private SQLite credentials")

            monkeypatch.setattr(diff_screen, "load_event_replay", fail)
            await press(pilot, "enter")
            await settle(pilot)
            text = str(app.screen.query_one("#diff-status", Static).render())
            assert "Unable to load comparison safely" in text and "credentials" not in text
            await press(pilot, "escape")
            await settle(pilot)

    asyncio.run(scenario())


def test_history_inspections_are_batched_and_old_schema_remains_supported(tmp_path, monkeypatch):
    store, cache = setup(tmp_path)
    for index in range(55):
        pair(store, cache, str(index), child_returncode=0, termination_reason="exited")
    history = read_history(store, HistoryFilters())
    statements = []
    original = store.connect

    def connect():
        connection = original()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store, "connect", connect)
    data = read_history_inspections(store, history)
    assert len(data) == 50
    assert len([sql for sql in statements if "FROM event_inspection" in sql]) == 1
    with store.connect() as conn:
        conn.execute("DROP TRIGGER event_inspection_cleanup")
        conn.execute("DROP TABLE event_inspection")
    assert all(
        value.inspection is None for value in read_history_inspections(store, history).values()
    )

    async def scenario():
        app = UsageTrimDashboard(store, cache=cache)
        async with app.run_test() as pilot:
            await settle(pilot)
            table = app.query_one("#history", DataTable)
            assert table.row_count == 50
            assert all(str(table.get_row_at(index)[-1]) == "Not recorded" for index in range(50))

    asyncio.run(scenario())


def test_details_discard_late_result_after_history_selection_changes(tmp_path, monkeypatch):
    store, cache = setup(tmp_path)
    pair(store, cache, "older", child_returncode=7, termination_reason="exited")
    pair(store, cache, "newer", child_returncode=0, termination_reason="exited")
    with store.connect() as conn:
        conn.execute("UPDATE events SET timestamp=CASE event_id WHEN 'older' THEN 1 ELSE 2 END")
    started, release = threading.Event(), threading.Event()
    reader = dashboard.read_event_inspection

    def blocking(store, event_id):
        if event_id == "newer":
            started.set()
            assert release.wait(3)
        return reader(store, event_id)

    monkeypatch.setattr(dashboard, "read_event_inspection", blocking)

    async def scenario():
        app = UsageTrimDashboard(store, cache=cache)
        try:
            async with app.run_test(size=(120, 45)) as pilot:
                await signaled(started)
                await press(pilot, "down")
                assert app._selected_event_id == "older"
                release.set()
                await settle(pilot)
                text = str(app.query_one("#details", Static).render())
                assert "Execution: Fail (exit 7)" in text
                assert 'event_id: "older"' in text and 'event_id: "newer"' not in text
        finally:
            release.set()

    asyncio.run(scenario())
