"""CCR explorer with explicit bounded content actions and no telemetry joins."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static

from usagetrim.core.cache import ContextCache
from usagetrim.tui.cache_content import CacheContentError, export_content, search_content
from usagetrim.tui.cache_data import CachePage, read_cache_metadata, read_cache_page
from usagetrim.tui.cache_preview import read_preview
from usagetrim.tui.vim_table import VimDataTable


def _format_search_line(number: int, line: str) -> str:
    """Bound only display text, including its line-number prefix and ellipsis."""
    prefix = f"{number}: "
    available = 300 - len(prefix)
    body = line if len(line) <= available else line[: available - 1] + "…"
    return prefix + body


class CacheScreen(Screen):
    """Browse metadata without recording a cache hit or reading stored output."""

    BINDINGS = [
        ("escape", "close", "Telemetry"),
        ("f2", "close", "Telemetry"),
        ("ctrl+f", "focus_search", "Find ref"),
        ("ctrl+enter", "search", "Search"),
        ("ctrl+r", "refresh_cache", "Refresh"),
        ("ctrl+left", "previous_page", "Previous page"),
        ("ctrl+right", "next_page", "Next page"),
    ]
    CSS = """
    CacheScreen { overflow-y: auto; }
    #cache-heading, #cache-status { height: auto; padding: 0 1; }
    .cache-controls { height: 3; }
    #cache-search, #cache-page-label { width: 1fr; }
    .cache-controls Button { width: auto; min-width: 8; }
    #cache-page-label { content-align: center middle; height: 3; }
    #cache-table { height: 1fr; min-height: 5; }
    #cache-details-pane { height: 8; border-top: solid $primary; }
    #cache-details { height: auto; padding: 0 1; }
    """

    def __init__(self, cache: ContextCache | None = None):
        super().__init__(id="cache-screen")
        self.cache = cache
        self.ref_query = ""
        self.page = 0
        self._result = CachePage((), 0, 0)
        self._selected_ref: str | None = None
        self._table: DataTable | None = None
        self._details: Static | None = None
        self._status: Static | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "CCR Cache · content read only on request · Esc/F2 returns to telemetry",
            id="cache-heading",
            markup=False,
        )
        with Horizontal(classes="cache-controls"):
            yield Input(placeholder="Ref ID contains… (literal, case-sensitive)", id="cache-search")
            yield Button("Search", id="cache-find", variant="primary")
            yield Button("Clear", id="cache-clear")
        with Horizontal(classes="cache-controls"):
            yield Button("Previous", id="cache-previous")
            yield Static("", id="cache-page-label", markup=False)
            yield Button("Next", id="cache-next")
        with Horizontal(classes="cache-controls"):
            yield Button("Preview", id="cache-preview")
            yield Button("Copy ref ID", id="cache-copy")
        with Horizontal(classes="cache-controls"):
            yield Input(placeholder="Literal text in selected log", id="cache-content-query")
            yield Button("Find lines", id="cache-content-find")
        with Horizontal(classes="cache-controls"):
            yield Input(
                placeholder="Export path (relative to working directory)", id="cache-export-path"
            )
            yield Button("Export", id="cache-export")
        yield Static("", id="cache-status", markup=False)
        yield VimDataTable(id="cache-table", cursor_type="row")
        with VerticalScroll(id="cache-details-pane"):
            yield Static("No cache entry selected.", id="cache-details", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self._table = self.query_one("#cache-table", DataTable)
        self._details = self.query_one("#cache-details", Static)
        self._status = self.query_one("#cache-status", Static)
        self._table.add_columns("Ref ID", "Source", "Created", "Hits")
        self._table.focus()
        self.refresh_cache()
        self.set_interval(5, self.refresh_cache)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_focus_search(self) -> None:
        self.query_one("#cache-search", Input).focus()

    def action_search(self) -> None:
        self.ref_query = self.query_one("#cache-search", Input).value
        self.page = 0
        self._selected_ref = None
        self.refresh_cache()
        self.query_one("#cache-table", DataTable).focus()

    def action_refresh_cache(self) -> None:
        self.refresh_cache()

    def action_previous_page(self) -> None:
        if self._result.has_previous:
            self.page -= 1
            self._selected_ref = None
            self.refresh_cache()

    def action_next_page(self) -> None:
        if self._result.has_next:
            self.page += 1
            self._selected_ref = None
            self.refresh_cache()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if self.is_attached and self.app.is_running:
            if event.input.id == "cache-content-query":
                self.action_search_content()
            elif event.input.id == "cache-export-path":
                self.action_export_content()
            else:
                self.action_search()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if not self.is_attached or not self.app.is_running:
            return
        if event.button.id == "cache-find":
            self.action_search()
        elif event.button.id == "cache-clear":
            self.query_one("#cache-search", Input).value = ""
            self.action_search()
        elif event.button.id == "cache-previous":
            self.action_previous_page()
        elif event.button.id == "cache-next":
            self.action_next_page()
        elif event.button.id == "cache-preview":
            self.action_preview()
        elif event.button.id == "cache-copy":
            self.action_copy_ref()
        elif event.button.id == "cache-content-find":
            self.action_search_content()
        elif event.button.id == "cache-export":
            self.action_export_content()

    def action_search_content(self) -> None:
        if self._selected_ref is None:
            self._status.update("Select a cache entry first.")
            return
        try:
            matches = search_content(
                self.cache,
                self._selected_ref,
                self.query_one("#cache-content-query", Input).value,
            )
        except (CacheContentError, OSError, sqlite3.Error) as exc:
            self._status.update(f"Search refused: {exc}")
            return
        self._details.update(
            "Redacted matches (maximum 20; display lines limited to 300 characters):\n"
            + (
                "\n".join(_format_search_line(number, line) for number, line in matches)
                or "No matches."
            )
        )

    def action_export_content(self) -> None:
        path = self.query_one("#cache-export-path", Input).value
        if self._selected_ref is None or not path:
            self._status.update("Select an entry and enter an export path.")
            return
        try:
            export_content(self.cache, self._selected_ref, path)
        except (CacheContentError, OSError, sqlite3.Error) as exc:
            self._status.update(f"Export refused: {exc}")
            return
        self._status.update("Complete redacted log exported. Existing files were not overwritten.")

    def action_preview(self) -> None:
        if self._selected_ref is None:
            self._status.update("Select a cache entry first.")
            return
        try:
            text = read_preview(self.cache, self._selected_ref)
        except (OSError, sqlite3.Error):
            text = "Preview unavailable: cache could not be read."
        self._details.update(text)

    def action_copy_ref(self) -> None:
        from usagetrim.core.clip import set_clipboard

        if self._selected_ref is None:
            self._status.update("Select a cache entry first.")
            return
        copied = set_clipboard(self._selected_ref)
        self._status.update(
            "Ref ID copied." if copied else "Clipboard unavailable; copy the ref ID from the table."
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        event.stop()
        table = event.data_table
        if (
            table is not self._table
            or not self.is_attached
            or not table.is_attached
            or not self.app.is_running
            or table.row_count == 0
            or self._details is None
            or not self._details.is_attached
        ):
            return
        ref_id = event.row_key.value
        if event.cursor_row != table.cursor_row:
            return
        if table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value != ref_id:
            return
        self._selected_ref = ref_id
        self._show_metadata()

    def _show_metadata(self) -> None:
        if self._details is None or not self._details.is_attached:
            return
        if self._selected_ref is None:
            self._details.update("No cache entry selected.")
            return
        try:
            entry = read_cache_metadata(self.cache, self._selected_ref)
        except (OSError, sqlite3.Error) as exc:
            self._details.update(f"Cache metadata unavailable ({type(exc).__name__}).")
            return
        if entry is None:
            self._details.update(
                "This cache entry was removed or is no longer available. Refresh the list."
            )
            return
        self._details.update(
            "Recorded cache metadata (null = not recorded)\n"
            + "\n".join(
                f"{key}: {json.dumps(value, ensure_ascii=False)}"
                for key, value in asdict(entry).items()
            )
        )

    def refresh_cache(self) -> None:
        table = self._table
        if (
            not self.is_attached
            or table is None
            or not table.is_attached
            or not self.app.is_running
            or self._details is None
            or not self._details.is_attached
            or self._status is None
            or not self._status.is_attached
        ):
            return
        try:
            result = read_cache_page(self.cache, ref_query=self.ref_query, page=self.page)
        except (OSError, sqlite3.Error) as exc:
            self._status.update(
                f"Unable to refresh cache metadata ({type(exc).__name__}); retrying in 5 seconds."
            )
            self._details.update("Cache metadata could not be refreshed; list may be stale.")
            return
        self._result, self.page = result, result.page
        self.query_one("#cache-previous", Button).disabled = not result.has_previous
        self.query_one("#cache-next", Button).disabled = not result.has_next
        self.query_one("#cache-page-label", Static).update(
            f"Page {result.page + 1}/{result.page_count} · {result.total} matching records"
        )
        message = "Metadata only; refreshes every 5 seconds. Enter/Search applies ref ID search."
        if not result.available:
            message = "CCR cache is not initialized. No database was created."
        elif not result.entries:
            message = "No matching cache entries." if self.ref_query else "CCR cache is empty."
        self._status.update(message)
        selected = self._selected_ref
        table.clear()
        for entry in result.entries:
            created = "—"
            if entry.created_at is not None:
                try:
                    created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.created_at))
                except (ValueError, OverflowError, OSError):
                    created = str(entry.created_at)
            table.add_row(
                Text(entry.ref_id),
                Text(entry.source if entry.source is not None else "—"),
                created,
                str(entry.hit_count) if entry.hit_count is not None else "—",
                key=entry.ref_id,
            )
        refs = [entry.ref_id for entry in result.entries]
        self._selected_ref = selected if selected in refs else refs[0] if refs else None
        if self._selected_ref is not None:
            table.move_cursor(row=refs.index(self._selected_ref), scroll=False)
        self._show_metadata()
