"""Coalesced atomic preference writes independent of monitor latency.

The dedicated executor drains its final write during normal Python shutdown.
It owns no widgets and never calls Monitor.snapshot(). Crash durability and
successful writes to an unavailable filesystem cannot be guaranteed.
"""

from __future__ import annotations

from concurrent.futures import Executor, ThreadPoolExecutor

from usagetrim.companion.controller import CompanionController


class PreferencesWriter:
    def __init__(self, controller: CompanionController, *, executor: Executor | None = None):
        self._controller = controller
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="companion-settings"
        )
        self._owns_executor = executor is None
        initial = controller.window_preferences()
        self._saved = self._value(
            initial["mode"], initial["position"], controller.animations_enabled()
        )
        self._latest = self._saved
        self._pending = None
        self._inflight = None
        self._closed = False

    @staticmethod
    def _value(mode, position, animations):
        return mode, None if position is None else tuple(position), animations

    def queue(self, mode: str, position: list[int] | None, *, animations: bool | None = None):
        if not self._closed:
            enabled = self._latest[2] if animations is None else animations
            self._latest = self._value(mode, position, enabled)

    def poll(self):
        """Observe write success only; results are preferences, never telemetry."""
        if self._closed or self._pending is None or not self._pending.done():
            return
        pending, self._pending = self._pending, None
        value, self._inflight = self._inflight, None
        pending.result()
        self._saved = value
        if self._latest != self._saved:
            self.flush()

    def _submit(self, value):
        mode, position, animations = value
        # Only a controller and immutable argument snapshot reach the worker.
        return self._executor.submit(
            self._controller.save_window_preferences,
            mode,
            None if position is None else list(position),
            animations,
        )

    def flush(self):
        if self._closed:
            return
        self.poll()
        if self._pending is None and self._latest != self._saved:
            self._inflight = self._latest
            self._pending = self._submit(self._latest)

    def close(self):
        """Queue the final state without joining any thread on the GUI thread."""
        if self._closed:
            return
        # A pending write can be running or failed but not yet observed. Append
        # the latest state after it, so the final successful write wins.
        if self._pending is not None or self._latest != self._saved:
            self._pending = self._submit(self._latest)
        self._closed = True
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=False)
