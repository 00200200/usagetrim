"""Reuse the monitor's real aggregates and shared pause preferences."""

from __future__ import annotations

from usagetrim.core.companion_state import settings, update_settings
from usagetrim.core.monitor import Monitor


class CompanionController:
    """Serialize monitor calls on the frontend's worker, never on the UI thread."""

    def __init__(self, monitor: Monitor | None = None):
        self._monitor = monitor

    def snapshot(self) -> dict:
        if self._monitor is None:
            self._monitor = Monitor()
        return self._monitor.dispatch({"method": "snapshot"})

    def set_paused(self, value: bool) -> dict:
        if type(value) is not bool:
            raise ValueError("paused must be boolean")
        if self._monitor is None:
            self._monitor = Monitor()
        return self._monitor.dispatch({"method": "pause", "paused": value})

    def always_on_top(self) -> bool:
        return settings().get("companion_qt_always_on_top") is True

    def set_always_on_top(self, value: bool) -> None:
        if type(value) is not bool:
            raise ValueError("always_on_top must be boolean")
        update_settings(companion_qt_always_on_top=value)
