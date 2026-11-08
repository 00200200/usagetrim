"""Explicit, worker-backed viewing of complete redacted event comparisons."""

import json

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from usagetrim.core.cache import ContextCache
from usagetrim.core.telemetry import TelemetryStore
from usagetrim.tui.replay_data import format_event_status, load_event_replay
from usagetrim.tui.workers import IOLane, IoResult


class DiffScreen(Screen):
    """Keep one comparison target; no reads or decompression on the UI thread."""

    BINDINGS = [Binding("escape", "close", "History", priority=True)]
    CSS = """
    DiffScreen { layout: vertical; }
    #diff-heading, #diff-status { height: auto; padding: 0 1; }
    #diff-scroll { height: 1fr; min-height: 1; }
    #diff-body { height: auto; width: 1fr; padding: 0 1; }
    """

    def __init__(self, store: TelemetryStore, event_id: str, *, cache: ContextCache | None = None):
        super().__init__(id="diff-screen")
        self.store = store
        self.event_id = event_id
        self.cache = cache
        self._io_closing = False
        self._body = None
        self._status = None
        self._diff_lane = IOLane(self, "event-diff")

    def compose(self) -> ComposeResult:
        yield Static("Event comparison · Esc returns to history", id="diff-heading", markup=False)
        yield Static("Loading comparison…", id="diff-status", markup=False)
        with VerticalScroll(id="diff-scroll", can_focus=True):
            yield Static("", id="diff-body", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self._body = self.query_one("#diff-body", Static)
        self._status = self.query_one("#diff-status", Static)
        self.query_one("#diff-scroll", VerticalScroll).focus()
        self.show_event(self.event_id)

    def _ready(self) -> bool:
        return (
            not self._io_closing
            and self.is_attached
            and self.app.is_running
            and self._body is not None
            and self._body.is_attached
            and self._status is not None
            and self._status.is_attached
        )

    def show_event(self, event_id: str) -> None:
        """Invalidate a previous target before queuing the latest comparison."""
        if not self._ready():
            return
        self.event_id = event_id
        self._status.update("Loading comparison…")
        self._body.update("")

        def apply(result, error):
            if not self._ready() or self.event_id != event_id:
                return
            if error is not None:
                self._status.update("Unable to load comparison safely.")
                return
            status = format_event_status(
                result.metadata.inspection, delivery=result.metadata.delivery
            )
            self._status.update(
                f"Event: {json.dumps(event_id)}\n{result.message}\n"
                f"Execution: {status.execution} · Recovery: {status.recovery}\n"
                f"Delivery: {status.delivery}"
            )
            self._body.update(result.unified_diff if result.unified_diff is not None else "")

        self._diff_lane.submit(
            event_id,
            lambda: load_event_replay(self.store, event_id, cache=self.cache),
            apply,
            force=True,
        )

    def on_io_result(self, message: IoResult) -> None:
        message.stop()
        message.lane.finish(message)

    def action_close(self) -> None:
        if self._io_closing or not self.is_attached:
            return
        self._io_closing = True
        self._diff_lane.close()
        self.dismiss(None)

    def on_unmount(self) -> None:
        self._io_closing = True
        self._diff_lane.close()
