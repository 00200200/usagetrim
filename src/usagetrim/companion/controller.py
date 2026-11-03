"""Reuse the monitor's real aggregates and shared pause preferences."""

from __future__ import annotations

from threading import Lock

from usagetrim.core.companion_state import settings, update_settings
from usagetrim.core.monitor import Monitor

_SETTINGS_LOCK = Lock()


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
        # Serialize only the small settings transaction, never the monitor read.
        with _SETTINGS_LOCK:
            update_settings(paused=value)
        return self.snapshot()

    def always_on_top(self) -> bool:
        return settings().get("companion_qt_always_on_top") is True

    def set_always_on_top(self, value: bool) -> None:
        if type(value) is not bool:
            raise ValueError("always_on_top must be boolean")
        with _SETTINGS_LOCK:
            update_settings(companion_qt_always_on_top=value)

    def window_preferences(self) -> dict:
        data = settings()
        position = data.get("companion_qt_pet_position")
        if not (
            isinstance(position, list)
            and len(position) == 2
            and all(type(value) is int and abs(value) <= 100000 for value in position)
        ):
            position = None
        return {
            "mode": "pet" if data.get("companion_qt_mode") == "pet" else "panel",
            "position": position,
        }

    def save_window_preferences(self, mode: str, position: list[int] | None) -> dict:
        if mode not in {"panel", "pet"}:
            raise ValueError("invalid companion mode")
        changes = {"companion_qt_mode": mode}
        if position is not None:
            if not (
                isinstance(position, list)
                and len(position) == 2
                and all(type(value) is int and abs(value) <= 100000 for value in position)
            ):
                raise ValueError("invalid companion position")
            changes["companion_qt_pet_position"] = position
        with _SETTINGS_LOCK:
            data = update_settings(**changes)
        return {
            "mode": data["companion_qt_mode"],
            "position": data.get("companion_qt_pet_position"),
        }
