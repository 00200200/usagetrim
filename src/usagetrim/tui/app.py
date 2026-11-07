"""A small, read-only view of the existing local gain report."""

from __future__ import annotations

import json
import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import (
    Button,
    Collapsible,
    DataTable,
    Footer,
    Header,
    Input,
    Select,
    Sparkline,
    Static,
)

from usagetrim.core.cache import ContextCache
from usagetrim.core.event_replay import snapshot_availability
from usagetrim.core.gain import build_gain_report
from usagetrim.core.telemetry import TelemetryStore
from usagetrim.metrics.pricing import calculate_cost
from usagetrim.tui.cache_screen import CacheScreen
from usagetrim.tui.diff_screen import DiffScreen
from usagetrim.tui.formatting import display_text, format_timestamp
from usagetrim.tui.history import HistoryFilters, read_history, read_history_inspections
from usagetrim.tui.replay_data import (
    InspectionData,
    ReplayState,
    format_event_status,
    read_event_inspection,
)
from usagetrim.tui.savings import SavingsSeries, read_savings
from usagetrim.tui.vim_table import VimDataTable
from usagetrim.tui.workers import IOLane, IoResult


class UsageTrimDashboard(App):
    """Show local token estimates and the latest 50 eligible telemetry events."""

    TITLE = "UsageTrim telemetry"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("ctrl+f", "focus_filters", "Filters"),
        ("escape", "focus_history", "History"),
        ("ctrl+enter", "apply_filters", "Apply"),
        ("ctrl+r", "refresh_history", "Refresh"),
        ("f2", "open_cache", "Cache"),
    ]
    CSS = """
    Screen { overflow-y: auto; }
    #summary { height: auto; padding: 0 1; }
    #filter-help, #status { height: auto; padding: 0 1; }
    .filter-row { height: 3; }
    .filter-row Input, #time-filter { width: 1fr; }
    .filter-row Button { width: auto; min-width: 8; }
    #savings-panel { height: auto; margin: 0; }
    #savings-chart { height: 2; }
    #savings-caption { height: auto; }
    #history { height: 1fr; min-height: 5; }
    #details-pane { height: 7; border-top: solid $primary; }
    #details { height: auto; padding: 0 1; }
    """

    def __init__(self, store: TelemetryStore | None = None, *, cache: ContextCache | None = None):
        super().__init__()
        self.store = store
        self.cache = cache
        self.filters = HistoryFilters()
        self._history: dict[str, dict] = {}
        self._selected_event_id: str | None = None
        self._details_widget: Static | None = None
        self._refresh_lane = IOLane(self, "telemetry-refresh")
        self._inspection_lane = IOLane(self, "event-details")
        self._history_inspections = {}
        self._inspection_detail = None
        self._store_holder = [store]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Waiting for telemetry…", id="summary", markup=False)
        yield Static(
            "History filters: exact values; blank = all. Enter/Apply to confirm, Esc for table.",
            id="filter-help",
            markup=False,
        )
        with Horizontal(classes="filter-row"):
            yield Input(placeholder="Operation", id="operation-filter")
            yield Input(placeholder="Client", id="client-filter")
            yield Input(placeholder="Project", id="project-filter")
        with Horizontal(classes="filter-row"):
            yield Select(
                [("All time", "all"), ("Last 24 hours", "24h"), ("Last 7 days", "7d")],
                value="all",
                allow_blank=False,
                id="time-filter",
            )
            yield Button("Apply", id="apply-filters", variant="primary")
            yield Button("Clear", id="clear-filters")
        with Collapsible(
            title="Token savings (expand/collapse)", collapsed=False, id="savings-panel"
        ):
            yield Sparkline([0], summary_function=max, id="savings-chart")
            yield Static("Waiting for telemetry…", id="savings-caption", markup=False)
        yield Static("", id="status", markup=False)
        yield VimDataTable(id="history", cursor_type="row")
        with VerticalScroll(id="details-pane"):
            yield Static("No operation selected.", id="details", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self._details_widget = self.query_one("#details", Static)
        table = self.query_one("#history", DataTable)
        table.add_columns(
            "When", "Operation", "Raw", "Compact", "Saved", "Reduction", "Delivery", "Execution"
        )
        table.focus()
        self.refresh_dashboard()
        self.set_interval(5, self.refresh_dashboard)

    def action_focus_filters(self) -> None:
        if self.screen.id in {"cache-screen", "diff-screen"}:
            return
        self.query_one("#operation-filter", Input).focus()

    def action_focus_history(self) -> None:
        if self.screen.id in {"cache-screen", "diff-screen"}:
            return
        self.query_one("#history", DataTable).focus()

    def action_refresh_history(self) -> None:
        self.refresh_dashboard()

    def action_open_cache(self) -> None:
        if isinstance(self.screen, DiffScreen):
            return
        if isinstance(self.screen, CacheScreen):
            self.screen.action_close()
        else:
            self._refresh_lane.invalidate()
            self._inspection_lane.invalidate()
            self.push_screen(CacheScreen(self.cache), self._cache_closed)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        table = event.data_table
        if (
            table.id == "history"
            and table.is_attached
            and table.has_focus
            and table.row_count
            and event.cursor_row == table.cursor_row
            and event.row_key.value in self._history
            and table.coordinate_to_cell_key(table.cursor_coordinate).row_key == event.row_key
            and self.screen.id not in {"cache-screen", "diff-screen"}
        ):
            self._selected_event_id = event.row_key.value
            self.action_open_diff()

    def action_open_diff(self) -> None:
        if (
            self.store is None
            or self._selected_event_id not in self._history
            or self.screen.id in {"cache-screen", "diff-screen"}
        ):
            return
        self._refresh_lane.invalidate()
        self._inspection_lane.invalidate()
        self.push_screen(
            DiffScreen(self.store, self._selected_event_id, cache=self.cache), self._cache_closed
        )

    def _cache_closed(self, result: None) -> None:
        if (
            self.is_running
            and self._details_widget is not None
            and self._details_widget.is_attached
            and self.screen.id not in {"cache-screen", "diff-screen"}
        ):
            self.refresh_dashboard()
            self.action_focus_history()

    def action_apply_filters(self) -> None:
        if self.screen.id in {"cache-screen", "diff-screen"}:
            return
        self.filters = HistoryFilters(
            operation=self.query_one("#operation-filter", Input).value or None,
            client=self.query_one("#client-filter", Input).value or None,
            project=self.query_one("#project-filter", Input).value or None,
            time_range=str(self.query_one("#time-filter", Select).value),
        )
        self.refresh_dashboard()
        self.action_focus_history()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_apply_filters()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply-filters":
            self.action_apply_filters()
        elif event.button.id == "clear-filters":
            for field in ("operation", "client", "project"):
                self.query_one(f"#{field}-filter", Input).value = ""
            self.query_one("#time-filter", Select).value = "all"
            self.action_apply_filters()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        table = event.data_table
        # Highlight messages can outlive their widgets during shutdown. Use the
        # sender directly instead of looking up a table that may be unmounted.
        if (
            not self.is_running
            or self.screen.id in {"cache-screen", "diff-screen"}
            or table.id != "history"
            or not table.is_attached
            or table.row_count == 0
            or self._details_widget is None
            or not self._details_widget.is_attached
        ):
            return
        event_id = event.row_key.value
        # Ignore highlights queued before a refresh or a subsequent cursor move.
        if event.cursor_row != table.cursor_row or event_id not in self._history:
            return
        if table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value != event_id:
            return
        if self._selected_event_id != event_id:
            self._inspection_lane.invalidate()
            self._inspection_detail = None
        self._selected_event_id = event_id
        self._show_details()

    def _show_details(self) -> None:
        details = self._details_widget
        if details is None or not details.is_attached:
            return
        event = self._history.get(self._selected_event_id)
        text = "No operation selected."
        if event is not None:
            event_id = event["event_id"]
            metadata = self._history_inspections.get(event_id)
            presence = None
            if self._inspection_detail is not None and self._inspection_detail[0] == event_id:
                _, metadata, presence = self._inspection_detail
            status = format_event_status(
                metadata.inspection if metadata else None,
                delivery=metadata.delivery if metadata else event.get("delivery"),
            )
            availability = "Not recorded"
            if metadata is not None:
                if metadata.state is ReplayState.EVENT_MISSING:
                    availability = "Event was removed"
                elif metadata.inspection is not None:
                    state = metadata.inspection.snapshot_state
                    availability = {
                        "disabled": "Not recorded (replay disabled)",
                        "cache_disabled": "Not recorded (cache disabled)",
                        "too_large": "Capture exceeded the snapshot limit",
                        "unavailable": "Capture unavailable",
                    }.get(state, "Not recorded")
                    if state == "stored":
                        availability = (
                            "Checking…"
                            if presence is None
                            else (
                                f"before: {'present' if presence[0] else 'unavailable'}; "
                                f"after: {'present' if presence[1] else 'unavailable'} (content not checked)"
                            )
                        )
                elif metadata.state not in {ReplayState.HISTORICAL, ReplayState.READY}:
                    availability = "Inspection unavailable"
            # JSON quoting preserves metadata strings without interpreting markup
            # or allowing embedded newlines to impersonate additional fields.
            text = (
                f"Execution: {status.execution}\nRecovery: {status.recovery}\n"
                f"Delivery: {status.delivery}\nBefore/after: {availability}\n"
                "Enter on the history row opens a comparison.\n"
                "Recorded telemetry metadata (null = not recorded)\n"
            ) + "\n".join(
                f"{key}: {json.dumps(display_text(value) if isinstance(value, str) else value, ensure_ascii=False)}"
                for key, value in event.items()
            )
            if self._inspection_detail is None or self._inspection_detail[0] != event_id:

                def load():
                    inspected = read_event_inspection(self.store, event_id)
                    available = (
                        snapshot_availability(inspected.inspection, cache=self.cache)
                        if inspected.inspection is not None
                        and inspected.inspection.snapshot_state == "stored"
                        else None
                    )
                    return inspected, available

                def apply(value, error):
                    if (
                        not self.is_running
                        or not details.is_attached
                        or self._selected_event_id != event_id
                        or self.screen.id in {"cache-screen", "diff-screen"}
                    ):
                        return
                    if error is None:
                        inspected, available = value
                    else:
                        inspected = InspectionData(event_id, ReplayState.METADATA_UNAVAILABLE)
                        available = None
                    self._inspection_detail = (event_id, inspected, available)
                    self._show_details()

                self._inspection_lane.submit(event_id, load, apply)
        details.update(text)

    def _show_savings(self, series: SavingsSeries) -> None:
        self.query_one("#savings-chart", Sparkline).data = list(series.saved_openai)
        label = {
            "24h": "Last 24 hours · hourly buckets",
            "7d": "Last 7 days · daily buckets",
            "all": "All time selected; chart shows last 30 days · daily buckets",
        }[self.filters.time_range]
        start = time.strftime("%Y-%m-%d %H:%M", time.gmtime(series.start))
        end = time.strftime("%Y-%m-%d %H:%M", time.gmtime(series.end))
        caption = (
            f"{label}\n{start} → {end} UTC (oldest → newest)\n"
            f"Saved OpenAI token estimates/bucket: {min(series.saved_openai):,}–"
            f"{max(series.saved_openai):,}; empty intervals = 0."
        )
        if series.event_count == 0:
            caption += "\nNo matching operations in this chart window."
        self.query_one("#savings-caption", Static).update(caption)

    def refresh_dashboard(self) -> None:
        """Refresh from telemetry without recording dashboard activity as savings."""
        if self._details_widget is None or not self._details_widget.is_attached:
            return
        if self.screen.id in {"cache-screen", "diff-screen"}:
            return
        filters = self.filters

        def load():
            # Only this serialized lane initializes/uses the holder. SQLite
            # connections are opened by the existing APIs inside this thread.
            if self._store_holder[0] is None:
                self._store_holder[0] = TelemetryStore()
            store = self._store_holder[0]
            report = build_gain_report(store, history_limit=50)
            now = time.time()
            history = read_history(store, filters, now=now)
            savings = read_savings(store, filters, now=now)
            inspections = read_history_inspections(store, history)
            return store, report, history, savings, inspections

        self._refresh_lane.submit(filters, load, self._apply_dashboard)

    def on_io_result(self, message: IoResult) -> None:
        message.stop()
        message.lane.finish(message)

    def on_unmount(self) -> None:
        self._refresh_lane.close()
        self._inspection_lane.close()

    def _apply_dashboard(self, result, error) -> None:
        if (
            not self.is_running
            or self._details_widget is None
            or not self._details_widget.is_attached
            or self.screen.id in {"cache-screen", "diff-screen"}
        ):
            return
        status = self.query_one("#status", Static)
        if error is not None:
            status.update(
                f"Unable to refresh telemetry ({type(error).__name__}); retrying in 5 seconds."
            )
            return
        self.store, report, history, savings, self._history_inspections = result
        self._inspection_lane.invalidate()
        self._inspection_detail = None
        self.query_one("#summary", Static).update(
            f"Operations: {report.total_events:,}\n"
            f"Estimated tokens — raw: {report.raw_openai:,} · compact: {report.compact_openai:,} · "
            f"saved: {report.saved_openai:,} · reduction: {report.reduction_pct:.1f}%\n"
            f"Hypothetical input-cost savings (OpenAI token-count proxy): "
            f"GPT-4o ~${calculate_cost('gpt-4o', input_tokens=report.saved_openai):.6f}; "
            f"Claude 3.5 Sonnet ~${calculate_cost('claude-3-5-sonnet', input_tokens=report.saved_openai):.6f} "
            "(static input rates; not actual billing).\n"
            "Global gain totals; filters apply to history and chart. Local OpenAI estimates, not billing."
        )
        self._show_savings(savings)
        if report.total_events == 0:
            message = (
                "No operations yet. Run usagetrim run -- … or use MCP tools. "
                "Refreshes every 5 seconds."
            )
        elif not history:
            message = "No operations match the active filters."
        else:
            message = (
                f"Showing {len(history)} operations (maximum 50) · refreshes every 5 seconds · "
                "arrows/j/k/Page Up/Page Down · Enter: compare."
            )
        status.update(message)
        table = self.query_one("#history", DataTable)
        row, column = table.cursor_coordinate
        scroll_x, scroll_y = table.scroll_x, table.scroll_y
        selected_id = self._selected_event_id
        self._history = {event["event_id"]: event for event in history}
        table.clear()
        for event in history:
            raw, compact = event["raw_openai"], event["compact_openai"]
            known = raw is not None and compact is not None
            saved = max(0, raw - compact) if known else None
            reduction = (100 * saved / raw if raw > 0 else 0) if known else None
            table.add_row(
                Text(format_timestamp(event["timestamp"])),
                Text(display_text(event["operation"])),
                f"{raw:,}" if raw is not None else "—",
                f"{compact:,}" if compact is not None else "—",
                f"{saved:,}" if saved is not None else "—",
                f"{reduction:.1f}%" if reduction is not None else "—",
                Text(display_text(event["delivery"])),
                Text(
                    format_event_status(
                        self._history_inspections[event["event_id"]].inspection
                    ).execution
                ),
                key=event["event_id"],
            )
        if table.row_count:
            ids = list(self._history)
            row = ids.index(selected_id) if selected_id in self._history else min(row, len(ids) - 1)
            self._selected_event_id = ids[row]
            table.move_cursor(row=row, column=column, scroll=False)
            table.scroll_to(x=scroll_x, y=scroll_y, animate=False)
        else:
            self._selected_event_id = None
        self._show_details()
