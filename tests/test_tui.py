"""Headless dashboard tests; install the tui extra to execute them."""

import asyncio
import sqlite3

import pytest

pytest.importorskip("textual")

from textual.widgets import Button, Collapsible, DataTable, Input, Select, Sparkline, Static

from tui_support import settle
from usagetrim.core.telemetry import TelemetryStore
from usagetrim.tui import app as dashboard
from usagetrim.tui.history import HistoryFilters


@pytest.mark.parametrize("raw,compact", [(0, 0), (1_000_000, 200_000), (100, 200)])
def test_tui_hypothetical_usd_uses_openai_savings_and_static_pricing(
    tmp_path, monkeypatch, raw, compact
):
    from usagetrim.metrics import pricing

    # Changing the reference rate proves the dashboard uses the pricing API.
    monkeypatch.setitem(pricing.PRICING_TABLE, "gpt-4o", pricing.ModelPricing("GPT-4o", 3.0, 10.0))
    store = TelemetryStore(tmp_path / "telemetry.db")
    if raw or compact:
        store.record(0, raw_openai=raw, compact_openai=compact)
    expected = pricing.calculate_cost("gpt-4o", input_tokens=max(0, raw - compact))
    claude_expected = pricing.calculate_cost(
        "claude-3-5-sonnet",
        input_tokens=max(0, raw - compact),
    )

    async def scenario():
        app = dashboard.UsageTrimDashboard(store)
        async with app.run_test() as pilot:
            await settle(pilot)
            summary = str(app.query_one("#summary", Static).render())
            assert f"GPT-4o ~${expected:.6f}" in summary
            assert "Hypothetical input-cost saving" in summary
            assert "static input rates; not actual billing" in summary
            assert f"Claude 3.5 Sonnet ~${claude_expected:.6f}" in summary

    asyncio.run(scenario())


def test_tui_empty_database_and_quit(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")

    async def scenario():
        app = dashboard.UsageTrimDashboard(store)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.query_one("#history", DataTable).row_count == 0
            assert "Operations: 0" in str(app.query_one("#summary", Static).render())
            assert "No operations yet" in str(app.query_one("#status", Static).render())
            await pilot.press("q")
            await settle(pilot)
            assert not app.is_running

    asyncio.run(scenario())


def test_tui_uses_gain_report_totals_and_latest_fifty(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    for index in range(55):
        store.record(
            0,
            raw_openai=100,
            compact_openai=25,
            operation=f"operation-{index}",
            event_id=str(index),
        )
    with store.connect() as conn:
        conn.execute("UPDATE events SET timestamp=CAST(event_id AS INTEGER)")
    store.record(0, raw_openai=9999, engine="none", operation="excluded-engine")
    store.record(0, raw_openai=9999, delivery="prepared", operation="excluded-preview")

    async def scenario():
        app = dashboard.UsageTrimDashboard(store)
        async with app.run_test() as pilot:
            await settle(pilot)
            table = app.query_one("#history", DataTable)
            assert table.row_count == 50
            assert str(table.get_row_at(0)[1]) == "operation-54"
            assert str(table.get_row_at(49)[1]) == "operation-5"
            summary = str(app.query_one("#summary", Static).render())
            for expected in ("Operations: 55", "5,500", "1,375", "4,125", "75.0%"):
                assert expected in summary
            with store.connect() as conn:
                assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 57
            await pilot.press("down")
            await settle(pilot)
            assert table.cursor_row == 1

    asyncio.run(scenario())


def test_tui_refresh_timer_updates_rows_without_losing_cursor(tmp_path, monkeypatch):
    store = TelemetryStore(tmp_path / "telemetry.db")
    scheduled = []
    app = dashboard.UsageTrimDashboard(store)
    original_set_interval = app.set_interval

    def schedule(interval, callback, *args, **kwargs):
        if callback == app.refresh_dashboard:
            scheduled.append((interval, callback))
        return original_set_interval(interval, callback, *args, **kwargs)

    monkeypatch.setattr(app, "set_interval", schedule)

    async def scenario():
        async with app.run_test() as pilot:
            await settle(pilot)
            assert scheduled == [(5, app.refresh_dashboard)]
            for _ in range(3):
                store.record(0, raw_openai=100, compact_openai=20)
            scheduled[0][1]()
            await settle(pilot)
            table = app.query_one("#history", DataTable)
            assert table.row_count == 3
            await pilot.press("down")
            await settle(pilot)
            app.refresh_dashboard()
            await settle(pilot)
            assert table.cursor_row == 1

    asyncio.run(scenario())


def test_tui_ignores_stale_and_unmounted_table_highlights(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    for event_id in ("first", "second"):
        store.record(0, raw_openai=100, compact_openai=20, event_id=event_id)

    async def scenario():
        app = dashboard.UsageTrimDashboard(store)
        async with app.run_test() as pilot:
            await settle(pilot)
            table = app.query_one("#history", DataTable)
            delayed = DataTable.RowHighlighted(
                data_table=table,
                cursor_row=table.cursor_row,
                row_key=table.coordinate_to_cell_key(table.cursor_coordinate).row_key,
            )
            await pilot.press("down")
            await settle(pilot)
            selected_id = app._selected_event_id
            assert selected_id != delayed.row_key.value
            app.on_data_table_row_highlighted(delayed)
            assert app._selected_event_id == selected_id

            table.clear()
            app.on_data_table_row_highlighted(delayed)
            assert app._selected_event_id == selected_id

            await table.remove()
            await settle(pilot)
            assert not table.is_attached
            app.on_data_table_row_highlighted(delayed)
            assert app._selected_event_id == selected_id

        # The same queued message must also be harmless after the app exits.
        app.on_data_table_row_highlighted(delayed)
        assert app._selected_event_id == selected_id

    asyncio.run(scenario())


def test_tui_ignores_highlights_when_details_are_unmounted(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record(0, raw_openai=100, event_id="selected")

    async def scenario():
        app = dashboard.UsageTrimDashboard(store)
        async with app.run_test() as pilot:
            await settle(pilot)
            table = app.query_one("#history", DataTable)
            delayed = DataTable.RowHighlighted(
                data_table=table,
                cursor_row=table.cursor_row,
                row_key=table.coordinate_to_cell_key(table.cursor_coordinate).row_key,
            )
            await app.query_one("#details", Static).remove()
            await settle(pilot)
            app.on_data_table_row_highlighted(delayed)
            app._show_details()
            assert app._selected_event_id == "selected"

    asyncio.run(scenario())


def test_tui_telemetry_error_keeps_dashboard_open(tmp_path, monkeypatch):
    app = dashboard.UsageTrimDashboard(TelemetryStore(tmp_path / "telemetry.db"))

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(dashboard, "build_gain_report", unavailable)

    async def scenario():
        async with app.run_test() as pilot:
            await settle(pilot)
            assert "Unable to refresh telemetry" in str(app.query_one("#status", Static).render())
            assert app.query_one("#history", DataTable).row_count == 0
            await pilot.press("q")
            await settle(pilot)

    asyncio.run(scenario())


def test_tui_filters_apply_from_keyboard_and_clear_empty_results(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record(
        0, raw_openai=100, compact_openai=25, operation="read", client="codex", project="/repo"
    )
    store.record(
        0, raw_openai=200, compact_openai=50, operation="diff", client="claude", project="/other"
    )

    async def scenario():
        app = dashboard.UsageTrimDashboard(store)
        async with app.run_test(size=(120, 45)) as pilot:
            await settle(pilot)
            await pilot.press("ctrl+f")
            await settle(pilot)
            assert app.query_one("#operation-filter", Input).has_focus
            app.query_one("#operation-filter", Input).value = "read"
            app.query_one("#client-filter", Input).value = "codex"
            app.query_one("#project-filter", Input).value = "/repo"
            app.query_one("#time-filter", Select).value = "24h"
            await pilot.press("enter")
            await settle(pilot)
            assert app.filters == HistoryFilters("read", "codex", "/repo", "24h")
            table = app.query_one("#history", DataTable)
            assert table.has_focus and table.row_count == 1
            assert str(table.get_row_at(0)[1]) == "read"
            # Global gain totals remain global even when the history is filtered.
            summary = str(app.query_one("#summary", Static).render())
            assert "Operations: 2" in summary and "filters apply to history and chart" in summary
            app.query_one("#operation-filter", Input).value = "missing"
            app.query_one("#apply-filters", Button).press()
            await settle(pilot)
            assert table.row_count == 0
            assert "No operations match" in str(app.query_one("#status", Static).render())
            assert str(app.query_one("#details", Static).render()) == "No operation selected."
            app.query_one("#clear-filters", Button).press()
            await settle(pilot)
            assert app.filters == HistoryFilters()
            assert app.query_one("#operation-filter", Input).value == ""
            assert app.query_one("#time-filter", Select).value == "all"
            assert table.row_count == 2

    asyncio.run(scenario())


def test_tui_refresh_preserves_active_filters_and_selected_event(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record(
        0,
        raw_openai=100,
        compact_openai=25,
        event_id="selected",
        operation="read",
        client="codex",
        project="/repo",
    )

    async def scenario():
        app = dashboard.UsageTrimDashboard(store)
        async with app.run_test(size=(120, 45)) as pilot:
            await settle(pilot)
            app.query_one("#operation-filter", Input).value = "read"
            app.query_one("#client-filter", Input).value = "codex"
            app.query_one("#project-filter", Input).value = "/repo"
            app.query_one("#time-filter", Select).value = "7d"
            app.action_apply_filters()
            await settle(pilot)
            active = app.filters
            store.record(
                0,
                raw_openai=50,
                event_id="new-match",
                operation="read",
                client="codex",
                project="/repo",
            )
            store.record(
                0,
                raw_openai=90,
                event_id="noise",
                operation="diff",
                client="codex",
                project="/repo",
            )
            # Unsubmitted edits must not silently alter the active timer query.
            app.query_one("#operation-filter", Input).value = "unfinished draft"
            app.refresh_dashboard()
            await settle(pilot)
            table = app.query_one("#history", DataTable)
            assert app.filters == active
            assert app.query_one("#operation-filter", Input).value == "unfinished draft"
            assert app.query_one("#time-filter", Select).value == "7d"
            assert table.row_count == 2
            assert app._selected_event_id == "selected"
            assert table.cursor_row == 1
            assert 'event_id: "selected"' in str(app.query_one("#details", Static).render())

    asyncio.run(scenario())


def test_tui_details_follow_selection_and_show_only_recorded_metadata(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    for event_id in ("older", "newer"):
        store.record(
            12,
            5,
            raw_openai=10,
            compact_openai=4,
            event_id=event_id,
            operation="[bold]read[/bold]",
            client="codex",
            project="/repo/żółć",
            method="o200k_base",
            delivery="returned",
            raw_bytes=40,
            compact_bytes=16,
            duration_s=0.25,
        )
    with store.connect() as conn:
        conn.execute("UPDATE events SET timestamp=CASE event_id WHEN 'older' THEN 1 ELSE 2 END")
        conn.execute(
            "UPDATE events SET client=NULL, project=NULL, duration_s=NULL WHERE event_id='older'"
        )

    async def scenario():
        app = dashboard.UsageTrimDashboard(store)
        async with app.run_test(size=(120, 45)) as pilot:
            await settle(pilot)
            details = str(app.query_one("#details", Static).render())
            for value in (
                'event_id: "newer"',
                'operation: "[bold]read[/bold]"',
                'client: "codex"',
                'project: "/repo/żółć"',
                'method: "o200k_base"',
                'delivery: "returned"',
                "raw_openai: 10",
                "compact_openai: 4",
                "raw_bytes: 40",
                "compact_bytes: 16",
                "duration_s: 0.25",
                "timestamp: 2.0",
                "sequence:",
            ):
                assert value in details
            assert (
                "command:" not in details and "status:" not in details and "output:" not in details
            )
            await pilot.press("down")
            await settle(pilot)
            details = str(app.query_one("#details", Static).render())
            assert 'event_id: "older"' in details
            assert "client: null" in details and "project: null" in details
            assert "duration_s: null" in details
            # Applying a filter removes the selected record and its stale details.
            app.query_one("#client-filter", Input).value = "codex"
            app.action_apply_filters()
            await settle(pilot)
            assert app._selected_event_id == "newer"
            assert 'event_id: "older"' not in str(app.query_one("#details", Static).render())

    asyncio.run(scenario())


def test_tui_chart_uses_active_filters_and_the_refresh_timer(tmp_path, monkeypatch):
    now = 1_700_000_123.5
    monkeypatch.setattr(dashboard.time, "time", lambda: now)
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record(
        0,
        raw_openai=100,
        compact_openai=40,
        event_id="selected",
        operation="read",
        client="codex",
        project="/repo",
    )
    store.record(0, raw_openai=900, operation="diff", client="codex", project="/repo")
    app = dashboard.UsageTrimDashboard(store)
    scheduled = []
    original_set_interval = app.set_interval

    def schedule(interval, callback, *args, **kwargs):
        if callback == app.refresh_dashboard:
            scheduled.append((interval, callback))
        return original_set_interval(interval, callback, *args, **kwargs)

    monkeypatch.setattr(app, "set_interval", schedule)

    async def scenario():
        async with app.run_test(size=(120, 50)) as pilot:
            await settle(pilot)
            app.query_one("#operation-filter", Input).value = "read"
            app.query_one("#client-filter", Input).value = "codex"
            app.query_one("#project-filter", Input).value = "/repo"
            app.query_one("#time-filter", Select).value = "24h"
            app.action_apply_filters()
            await settle(pilot)
            chart = app.query_one("#savings-chart", Sparkline)
            assert list(chart.data) == [0] * 23 + [60]
            assert scheduled == [(5, app.refresh_dashboard)]
            active = app.filters
            assert app._selected_event_id == "selected"
            store.record(
                0,
                raw_openai=100,
                compact_openai=40,
                operation="read",
                client="codex",
                project="/repo",
            )
            store.record(0, raw_openai=9999, operation="diff", client="codex", project="/repo")
            app.query_one("#operation-filter", Input).value = "unsubmitted"
            scheduled[0][1]()
            await settle(pilot)
            assert list(chart.data) == [0] * 23 + [120]
            assert app.filters == active
            assert app._selected_event_id == "selected"
            assert app.query_one("#history", DataTable).row_count == 2
            assert 'event_id: "selected"' in str(app.query_one("#details", Static).render())

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "period,count,label",
    [
        ("24h", 24, "Last 24 hours · hourly"),
        ("7d", 7, "Last 7 days · daily"),
        ("all", 30, "All time selected; chart shows last 30 days"),
    ],
)
def test_tui_chart_empty_ranges_are_labeled(tmp_path, period, count, label):
    store = TelemetryStore(tmp_path / "telemetry.db")

    async def scenario():
        app = dashboard.UsageTrimDashboard(store)
        async with app.run_test(size=(100, 45)) as pilot:
            await settle(pilot)
            app.query_one("#time-filter", Select).value = period
            app.action_apply_filters()
            await settle(pilot)
            assert list(app.query_one("#savings-chart", Sparkline).data) == [0] * count
            caption = str(app.query_one("#savings-caption", Static).render())
            assert label in caption and "UTC" in caption
            assert "empty intervals = 0" in caption
            assert "No matching operations in this chart window" in caption

    asyncio.run(scenario())


def test_tui_chart_can_collapse_in_small_terminal_without_losing_navigation(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    for _ in range(2):
        store.record(0, raw_openai=100, compact_openai=20)

    async def scenario():
        app = dashboard.UsageTrimDashboard(store)
        async with app.run_test(size=(48, 20)) as pilot:
            await settle(pilot)
            panel = app.query_one("#savings-panel", Collapsible)
            panel.collapsed = True
            app.action_focus_history()
            await settle(pilot)
            await pilot.press("down")
            await settle(pilot)
            assert app.query_one("#history", DataTable).cursor_row == 1
            app.refresh_dashboard()
            await settle(pilot)
            assert panel.collapsed is True
            assert len(app.query_one("#savings-chart", Sparkline).data) == 30
            assert app.query_one("#history", DataTable).cursor_row == 1

    asyncio.run(scenario())


def test_tui_vim_navigation(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")

    for _ in range(3):
        store.record(0, raw_openai=100, compact_openai=20)

    async def scenario():
        app = dashboard.UsageTrimDashboard(store)

        async with app.run_test() as pilot:
            await settle(pilot)
            table = app.query_one("#history", DataTable)
            table.focus()

            assert table.cursor_row == 0

            await pilot.press("j")
            await settle(pilot)
            assert table.cursor_row == 1

            await pilot.press("k")
            await settle(pilot)
            assert table.cursor_row == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("timestamp", [1e300, "invalid", None, float("nan")])
def test_tui_history_uses_placeholder_for_invalid_timestamp(tmp_path, monkeypatch, timestamp):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record(0, raw_openai=100, compact_openai=20, event_id="invalid-time")
    original_read = dashboard.read_history

    def history(*args, **kwargs):
        # None/NaN cannot be stored in the NOT NULL timestamp column, but the
        # display formatter must also tolerate these values from an adapter.
        return [{**row, "timestamp": timestamp} for row in original_read(*args, **kwargs)]

    monkeypatch.setattr(dashboard, "read_history", history)

    async def scenario():
        app = dashboard.UsageTrimDashboard(store)
        async with app.run_test() as pilot:
            await settle(pilot)
            table = app.query_one("#history", DataTable)
            assert table.row_count == 1
            assert str(table.get_row_at(0)[0]) == "—"
            app.refresh_dashboard()
            await settle(pilot)
            assert table.row_count == 1

    asyncio.run(scenario())


def test_tui_history_handles_out_of_range_timestamp_from_sqlite(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record(0, raw_openai=100, event_id="out-of-range")
    with store.connect() as conn:
        conn.execute("UPDATE events SET timestamp=?", (1e300,))

    async def scenario():
        app = dashboard.UsageTrimDashboard(store)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert str(app.query_one("#history", DataTable).get_row_at(0)[0]) == "—"

    asyncio.run(scenario())
