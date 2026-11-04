"""Deterministic thread-worker lifecycle tests for the Textual dashboard."""

import asyncio
import sqlite3
import threading

import pytest

pytest.importorskip("textual")

from textual.widgets import Button, DataTable, Input, Static

from tui_support import settle
from usagetrim.core.cache import ContextCache
from usagetrim.core.telemetry import TelemetryStore
from usagetrim.tui import app as dashboard
from usagetrim.tui import cache_screen


async def signaled(event):
    assert await asyncio.to_thread(event.wait, 3), "Worker did not reach the synchronization point"


def make_cache(tmp_path):
    cache = ContextCache(tmp_path / "cache.db")
    first = cache.store("first log")
    second = cache.store("second log")
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute("UPDATE output_cache SET created_at=1 WHERE ref_id=?", (first,))
        conn.execute("UPDATE output_cache SET created_at=2 WHERE ref_id=?", (second,))
    return cache, first, second


def test_dashboard_remains_responsive_during_real_sqlite_lock(tmp_path, monkeypatch):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record(0, raw_openai=100, operation="read")
    started = threading.Event()
    original = dashboard.build_gain_report

    def report(*args, **kwargs):
        started.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(dashboard, "build_gain_report", report)

    async def scenario():
        locker = store.connect()
        locker.execute("BEGIN EXCLUSIVE")
        try:
            app = dashboard.UsageTrimDashboard(store)
            async with app.run_test() as pilot:
                await signaled(started)
                # This key must be processed while the real SQLite query is blocked.
                await pilot.press("ctrl+f")
                assert app.query_one("#operation-filter", Input).has_focus
                assert app._refresh_lane.busy
                assert app.query_one("#history", DataTable).row_count == 0
                locker.rollback()
                await settle(pilot)
                assert app.query_one("#history", DataTable).row_count == 1
        finally:
            locker.rollback()
            locker.close()

    asyncio.run(scenario())


def test_dashboard_coalesces_refreshes_and_discards_cancelled_stale_result(tmp_path, monkeypatch):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record(0, raw_openai=100, operation="read", event_id="selected")
    store.record(0, raw_openai=100, operation="diff", event_id="other")
    started, release = threading.Event(), threading.Event()
    calls = []
    original = dashboard.build_gain_report

    def blocked(*args, **kwargs):
        calls.append(threading.get_ident())
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
        return original(*args, **kwargs)

    async def scenario():
        app = dashboard.UsageTrimDashboard(store)
        try:
            async with app.run_test() as pilot:
                await settle(pilot)
                table = app.query_one("#history", DataTable)
                table.move_cursor(row=list(app._history).index("selected"))
                await pilot.pause()
                assert app._selected_event_id == "selected"
                monkeypatch.setattr(dashboard, "build_gain_report", blocked)
                applied = []
                apply = app._apply_dashboard

                def record(result, error):
                    assert threading.get_ident() == threading.main_thread().ident
                    applied.append(result)
                    apply(result, error)

                monkeypatch.setattr(app, "_apply_dashboard", record)
                app.refresh_dashboard()
                await signaled(started)
                worker = next(w for w in app.workers if w.name == "telemetry-refresh")
                worker.cancel()  # The actual synchronous read is still running.
                app.query_one("#operation-filter", Input).value = "read"
                app.action_apply_filters()
                for _ in range(5):
                    app.refresh_dashboard()
                await pilot.press("ctrl+f")
                assert len(calls) == 1 and app._refresh_lane.busy
                assert applied == []
                release.set()
                await settle(pilot)
                assert len(calls) == 2
                assert len(applied) == 1
                assert app.filters.operation == "read"
                assert app._selected_event_id == "selected"
                assert app.query_one("#history", DataTable).row_count == 1
                assert 'event_id: "selected"' in str(app.query_one("#details", Static).render())
        finally:
            release.set()

    asyncio.run(scenario())


def test_cache_pages_do_not_overlap_and_keep_only_latest_query(tmp_path, monkeypatch):
    cache, first, _ = make_cache(tmp_path)
    started, release = threading.Event(), threading.Event()
    queries = []
    original = cache_screen.read_cache_page

    def page(*args, **kwargs):
        queries.append(kwargs["ref_query"])
        if len(queries) == 1:
            started.set()
            assert release.wait(5)
        return original(*args, **kwargs)

    async def scenario():
        app = dashboard.UsageTrimDashboard(TelemetryStore(tmp_path / "telemetry.db"), cache=cache)
        try:
            async with app.run_test(size=(120, 55)) as pilot:
                await settle(pilot)
                await pilot.press("f2")
                await settle(pilot)
                view = app.screen
                monkeypatch.setattr(cache_screen, "read_cache_page", page)
                view.refresh_cache()
                await signaled(started)
                view.query_one("#cache-search", Input).value = first
                view.action_search()
                for _ in range(5):
                    view.refresh_cache()
                await pilot.press("ctrl+f")
                assert queries == [""]
                assert view._page_lane.busy
                release.set()
                await settle(pilot)
                assert queries == ["", first]
                assert view._selected_ref == first
                assert view.query_one("#cache-table", DataTable).row_count == 1
        finally:
            release.set()

    asyncio.run(scenario())


def test_export_failure_restores_controls_and_preserves_existing_file(tmp_path):
    cache, _, _ = make_cache(tmp_path)
    target = tmp_path / "existing.log"
    target.write_bytes(b"original")

    async def scenario():
        app = dashboard.UsageTrimDashboard(TelemetryStore(tmp_path / "telemetry.db"), cache=cache)
        async with app.run_test(size=(120, 55)) as pilot:
            await settle(pilot)
            await pilot.press("f2")
            await settle(pilot)
            view = app.screen
            view.query_one("#cache-export-path", Input).value = str(target)
            view.action_export_content()
            await settle(pilot)
            assert "Export refused" in str(view._status.render())
            assert not view._exporting
            assert not view.query_one("#cache-export", Button).disabled
            assert target.read_bytes() == b"original"

    asyncio.run(scenario())


def test_default_store_initializes_once_outside_ui_thread(tmp_path, monkeypatch):
    store = TelemetryStore(tmp_path / "telemetry.db")
    ui_thread = threading.get_ident()
    calls = []

    def initialize():
        assert threading.get_ident() != ui_thread
        calls.append(True)
        return store

    monkeypatch.setattr(dashboard, "TelemetryStore", initialize)

    async def scenario():
        app = dashboard.UsageTrimDashboard()
        async with app.run_test() as pilot:
            await settle(pilot)
            for _ in range(2):
                app.refresh_dashboard()
                await settle(pilot)
            assert calls == [True]
            assert app.store is store

    asyncio.run(scenario())


def test_old_preview_cannot_replace_new_selection(tmp_path, monkeypatch):
    cache, first, second = make_cache(tmp_path)
    started, release = threading.Event(), threading.Event()

    def preview(cache, ref):
        assert ref == second
        started.set()
        assert release.wait(5)
        return "STALE PREVIEW"

    monkeypatch.setattr(cache_screen, "read_preview", preview)

    async def scenario():
        app = dashboard.UsageTrimDashboard(TelemetryStore(tmp_path / "telemetry.db"), cache=cache)
        try:
            async with app.run_test(size=(120, 55)) as pilot:
                await settle(pilot)
                await pilot.press("f2")
                await settle(pilot)
                view = app.screen
                view.action_preview()
                await signaled(started)
                await pilot.press("down")
                assert view._selected_ref == first
                assert "STALE PREVIEW" not in str(view._details.render())
                release.set()
                await settle(pilot)
                details = str(view._details.render())
                assert f'ref_id: "{first}"' in details
                assert "STALE PREVIEW" not in details
        finally:
            release.set()

    asyncio.run(scenario())


def test_closing_cache_does_not_wait_for_blocked_page_or_apply_late_result(tmp_path, monkeypatch):
    cache, _, _ = make_cache(tmp_path)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = cache_screen.read_cache_page
    applied = []
    apply = cache_screen.CacheScreen._apply_page

    def page(*args, **kwargs):
        started.set()
        try:
            assert release.wait(5)
            return original(*args, **kwargs)
        finally:
            finished.set()

    def record(self, *args):
        assert threading.get_ident() == threading.main_thread().ident
        applied.append(True)
        apply(self, *args)

    monkeypatch.setattr(cache_screen, "read_cache_page", page)
    monkeypatch.setattr(cache_screen.CacheScreen, "_apply_page", record)

    async def scenario():
        app = dashboard.UsageTrimDashboard(TelemetryStore(tmp_path / "telemetry.db"), cache=cache)
        try:
            async with app.run_test(size=(120, 55)) as pilot:
                await settle(pilot)
                await pilot.press("f2")
                view = app.screen
                await signaled(started)
                await pilot.press("escape")
                assert app.screen is not view
                assert not release.is_set()
                assert view._io_closing
                release.set()
                await signaled(finished)
                await settle(pilot)
                assert applied == []
        finally:
            release.set()

    asyncio.run(scenario())


def test_dashboard_can_close_with_blocked_refresh(tmp_path, monkeypatch):
    store = TelemetryStore(tmp_path / "telemetry.db")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        started.set()
        try:
            assert release.wait(5)
            raise sqlite3.OperationalError("late read failure")
        finally:
            finished.set()

    monkeypatch.setattr(dashboard, "build_gain_report", blocked)

    async def scenario():
        app = dashboard.UsageTrimDashboard(store)
        try:
            async with app.run_test() as pilot:
                await signaled(started)
                await pilot.press("q")
            assert not app.is_running
            assert not release.is_set()
            assert app._refresh_lane.closed
        finally:
            release.set()
            await signaled(finished)

    asyncio.run(scenario())


def test_worker_exceptions_are_reported_without_terminating_ui(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("worker failure")

    cache, _, _ = make_cache(tmp_path)
    monkeypatch.setattr(dashboard, "build_gain_report", unavailable)

    async def scenario():
        app = dashboard.UsageTrimDashboard(TelemetryStore(tmp_path / "telemetry.db"), cache=cache)
        async with app.run_test(size=(120, 55)) as pilot:
            await settle(pilot)
            assert "RuntimeError" in str(app.query_one("#status", Static).render())
            await pilot.press("f2")
            await settle(pilot)
            view = app.screen
            monkeypatch.setattr(cache_screen, "read_preview", unavailable)
            view.action_preview()
            await settle(pilot)
            assert "Preview unavailable" in str(view._details.render())
            monkeypatch.setattr(cache_screen, "read_cache_page", unavailable)
            view.refresh_cache()
            await settle(pilot)
            assert "RuntimeError" in str(view._status.render())
            assert app.is_running

    asyncio.run(scenario())


def test_export_runs_off_loop_and_progress_survives_refresh(tmp_path, monkeypatch):
    cache, _, _ = make_cache(tmp_path)
    started, release = threading.Event(), threading.Event()
    original = cache_screen.export_content
    ui_thread = threading.get_ident()

    def export(*args):
        assert threading.get_ident() != ui_thread
        started.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(cache_screen, "export_content", export)
    target = tmp_path / "export.log"

    async def scenario():
        app = dashboard.UsageTrimDashboard(TelemetryStore(tmp_path / "telemetry.db"), cache=cache)
        try:
            async with app.run_test(size=(120, 55)) as pilot:
                await settle(pilot)
                await pilot.press("f2")
                await settle(pilot)
                view = app.screen
                view.query_one("#cache-export-path", Input).value = str(target)
                view.action_export_content()
                await signaled(started)
                button = view.query_one("#cache-export", Button)
                assert button.disabled and not target.exists()
                view.refresh_cache()
                await asyncio.wait_for(view._page_lane.wait_idle(), 3)
                await pilot.press("ctrl+f")
                assert view.query_one("#cache-search", Input).has_focus
                assert "Exporting" in str(view._status.render())
                assert view._exporting
                release.set()
                await settle(pilot)
                assert target.read_text() == "second log"
                assert not button.disabled
                assert "Complete redacted log exported" in str(view._status.render())
        finally:
            release.set()

    asyncio.run(scenario())
