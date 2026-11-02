"""Headless CCR metadata browsing, independent of the telemetry view."""

import asyncio
import sqlite3

import pytest

pytest.importorskip("textual")

from textual.widgets import Button, DataTable, Input, Sparkline, Static

from usagetrim.core import cache as cache_module
from usagetrim.core.cache import ContextCache
from usagetrim.core.telemetry import TelemetryStore
from usagetrim.tui.app import UsageTrimDashboard
from usagetrim.tui.cache_screen import CacheScreen


def cache_with_entries(tmp_path, count):
    cache = ContextCache(tmp_path / "cache.db")
    refs = []
    for index in range(count):
        ref = cache.store(f"cached output {index}", source=f"source-{index}")
        refs.append(ref)
        with sqlite3.connect(cache.db_path) as conn:
            conn.execute("UPDATE output_cache SET created_at=? WHERE ref_id=?", (index, ref))
    return cache, refs


def test_cache_screen_search_paging_and_metadata_without_content(tmp_path, monkeypatch):
    cache, refs = cache_with_entries(tmp_path, 60)

    def forbidden(*args, **kwargs):
        raise AssertionError("cache explorer must never retrieve or decompress output")

    monkeypatch.setattr(ContextCache, "retrieve", forbidden)
    monkeypatch.setattr(cache_module, "decompress_payload", forbidden)
    store = TelemetryStore(tmp_path / "telemetry.db")

    async def scenario():
        app = UsageTrimDashboard(store, cache=cache)
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.press("f2")
            view = app.screen
            assert isinstance(view, CacheScreen)
            table = view.query_one("#cache-table", DataTable)
            assert table.row_count == 50
            assert str(table.get_row_at(0)[0]) == refs[59]
            details = str(view.query_one("#cache-details", Static).render())
            assert f'ref_id: "{refs[59]}"' in details
            assert 'source: "source-59"' in details
            assert "hit_count: 0" in details and "created_at: 59.0" in details
            assert "content:" not in details and "cached output" not in details
            await pilot.press("ctrl+right")
            await pilot.pause()
            assert view.page == 1 and table.row_count == 10
            assert str(table.get_row_at(0)[0]) == refs[9]
            assert view.query_one("#cache-next", Button).disabled
            await pilot.press("ctrl+f")
            field = view.query_one("#cache-search", Input)
            assert field.has_focus
            field.value = refs[0]
            await pilot.press("enter")
            assert view.ref_query == refs[0] and view.page == 0
            assert table.row_count == 1 and str(table.get_row_at(0)[0]) == refs[0]
            field.value = "missing-ref"
            view.action_search()
            await pilot.pause()
            assert table.row_count == 0
            assert "No matching cache entries" in str(
                view.query_one("#cache-status", Static).render()
            )
            assert (
                str(view.query_one("#cache-details", Static).render()) == "No cache entry selected."
            )
            view.query_one("#cache-clear", Button).press()
            await pilot.pause()
            assert view.ref_query == "" and table.row_count == 50

    asyncio.run(scenario())
    with sqlite3.connect(cache.db_path) as conn:
        assert conn.execute("SELECT SUM(hit_count) FROM output_cache").fetchone()[0] == 0


def test_cache_screen_return_preserves_telemetry_filters_chart_and_selection(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record(0, raw_openai=100, compact_openai=40, operation="read", event_id="event")
    cache, _ = cache_with_entries(tmp_path, 1)

    async def scenario():
        app = UsageTrimDashboard(store, cache=cache)
        async with app.run_test(size=(120, 45)) as pilot:
            app.query_one("#operation-filter", Input).value = "read"
            app.action_apply_filters()
            await pilot.pause()
            active = app.filters
            selected = app._selected_event_id
            values = list(app.query_one("#savings-chart", Sparkline).data)
            await pilot.press("f2")
            view = app.screen
            view.query_one("#cache-search", Input).value = "no-match"
            view.action_search()
            app.refresh_dashboard()  # Background telemetry timer must not target this screen.
            assert app.filters == active and app._selected_event_id == selected
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, CacheScreen)
            assert app.filters == active and app._selected_event_id == selected
            assert app.query_one("#operation-filter", Input).value == "read"
            assert list(app.query_one("#savings-chart", Sparkline).data) == values
            assert app.query_one("#history", DataTable).has_focus

    asyncio.run(scenario())


def test_cache_screen_handles_deleted_selection_refresh_and_late_events(tmp_path):
    cache, refs = cache_with_entries(tmp_path, 2)
    store = TelemetryStore(tmp_path / "telemetry.db")

    async def scenario():
        app = UsageTrimDashboard(store, cache=cache)
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.press("f2")
            view = app.screen
            table = view.query_one("#cache-table", DataTable)
            assert view._selected_ref == refs[1]
            delayed = DataTable.RowHighlighted(
                data_table=table,
                cursor_row=table.cursor_row,
                row_key=table.coordinate_to_cell_key(table.cursor_coordinate).row_key,
            )
            with sqlite3.connect(cache.db_path) as conn:
                conn.execute("DELETE FROM output_cache WHERE ref_id=?", (refs[1],))
            view._show_metadata()
            assert "removed" in str(view.query_one("#cache-details", Static).render())
            view.refresh_cache()
            await pilot.pause()
            assert table.row_count == 1 and view._selected_ref == refs[0]
            view.on_data_table_row_highlighted(delayed)
            assert view._selected_ref == refs[0]
            with sqlite3.connect(cache.db_path) as conn:
                conn.execute("DELETE FROM output_cache")
            view.refresh_cache()
            await pilot.pause()
            assert table.row_count == 0 and view._selected_ref is None
            assert "empty" in str(view.query_one("#cache-status", Static).render())
            await pilot.press("escape")
            await pilot.pause()
            view.on_data_table_row_highlighted(delayed)
            view.refresh_cache()
            assert app._selected_event_id is None

    asyncio.run(scenario())


def test_cache_screen_missing_default_cache_does_not_create_database(tmp_path, monkeypatch):
    directory = tmp_path / "missing-cache-directory"
    monkeypatch.setenv("USAGETRIM_CACHE_DIR", str(directory))
    store = TelemetryStore(tmp_path / "telemetry.db")

    async def scenario():
        app = UsageTrimDashboard(store)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.press("f2")
            view = app.screen
            assert view.query_one("#cache-table", DataTable).row_count == 0
            assert "not initialized" in str(view.query_one("#cache-status", Static).render())
            assert not directory.exists()

    asyncio.run(scenario())


def test_cache_screen_empty_existing_cache(tmp_path):
    cache, _ = cache_with_entries(tmp_path, 0)
    store = TelemetryStore(tmp_path / "telemetry.db")

    async def scenario():
        app = UsageTrimDashboard(store, cache=cache)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.press("f2")
            view = app.screen
            assert "CCR cache is empty" in str(view.query_one("#cache-status", Static).render())
            assert view.query_one("#cache-previous", Button).disabled
            assert view.query_one("#cache-next", Button).disabled
            assert (
                str(view.query_one("#cache-details", Static).render()) == "No cache entry selected."
            )

    asyncio.run(scenario())


def test_cache_preview_and_copy_are_explicit(tmp_path, monkeypatch):
    from usagetrim.core import clip
    from usagetrim.tui import cache_screen

    cache, refs = cache_with_entries(tmp_path, 1)
    calls = []
    monkeypatch.setattr(
        cache_screen, "read_preview", lambda cache, ref: calls.append(ref) or "preview"
    )
    monkeypatch.setattr(clip, "set_clipboard", lambda ref: calls.append(("copy", ref)) or False)

    async def scenario():
        app = UsageTrimDashboard(TelemetryStore(tmp_path / "telemetry.db"), cache=cache)
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.press("f2")
            view = app.screen
            assert calls == []
            view.action_preview()
            assert calls == [refs[0]]
            assert str(view.query_one("#cache-details", Static).render()) == "preview"
            view.action_copy_ref()
            assert calls[-1] == ("copy", refs[0])
            assert "Clipboard unavailable" in str(view.query_one("#cache-status", Static).render())

    asyncio.run(scenario())


@pytest.mark.parametrize("success", [True, False])
def test_cache_copy_button_uses_native_clipboard_result(tmp_path, monkeypatch, success):
    from usagetrim.core import clip

    cache, refs = cache_with_entries(tmp_path, 1)
    copied = []

    def copy(text):
        copied.append(text)
        return success

    monkeypatch.setattr(clip, "set_clipboard", copy)

    async def scenario():
        app = UsageTrimDashboard(TelemetryStore(tmp_path / "telemetry.db"), cache=cache)
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.press("f2")
            view = app.screen
            view.query_one("#cache-copy", Button).press()
            await pilot.pause()
            assert copied == [refs[0]]
            status = str(view.query_one("#cache-status", Static).render())
            assert ("Ref ID copied." if success else "Clipboard unavailable") in status
            if not success:
                assert "Ref ID copied." not in status

    asyncio.run(scenario())


def test_cache_content_actions_are_explicit_and_do_not_retrieve(tmp_path, monkeypatch):
    from usagetrim.tui import cache_screen

    cache, refs = cache_with_entries(tmp_path, 1)
    calls = []
    monkeypatch.setattr(
        cache_screen, "search_content", lambda c, r, q: calls.append((r, q)) or [(3, "match")]
    )
    monkeypatch.setattr(cache_screen, "export_content", lambda c, r, p: calls.append((r, p)))

    async def scenario():
        app = UsageTrimDashboard(TelemetryStore(tmp_path / "telemetry.db"), cache=cache)
        async with app.run_test(size=(120, 55)) as pilot:
            await pilot.press("f2")
            view = app.screen
            assert calls == []
            view.query_one("#cache-content-query", Input).value = "match"
            view.query_one("#cache-content-find", Button).press()
            await pilot.pause()
            assert calls == [(refs[0], "match")]
            assert "3: match" in str(view.query_one("#cache-details", Static).render())
            view.query_one("#cache-export-path", Input).value = "output.log"
            view.query_one("#cache-export", Button).press()
            await pilot.pause()
            assert calls[-1] == (refs[0], "output.log")
            assert "Complete redacted log exported" in str(
                view.query_one("#cache-status", Static).render()
            )

    asyncio.run(scenario())


def test_cache_search_display_caps_numbered_lines_without_changing_matching(tmp_path, monkeypatch):
    from usagetrim.tui import cache_screen

    line = "ż" * 500 + "needle"
    calls = []

    def search(cache, ref, query):
        calls.append(query)
        return [(123, line), (124, "short")]

    monkeypatch.setattr(cache_screen, "search_content", search)
    cache, _ = cache_with_entries(tmp_path, 1)

    async def scenario():
        app = UsageTrimDashboard(TelemetryStore(tmp_path / "telemetry.db"), cache=cache)
        async with app.run_test(size=(120, 55)) as pilot:
            await pilot.press("f2")
            view = app.screen
            view.query_one("#cache-content-query", Input).value = "needle"
            view.action_search_content()
            lines = str(view.query_one("#cache-details", Static).render()).splitlines()
            assert calls == ["needle"]
            assert len(lines[1]) == 300 and lines[1].startswith("123: ") and lines[1].endswith("…")
            assert lines[2] == "124: short"
            assert "300 characters" in lines[0]

    asyncio.run(scenario())
