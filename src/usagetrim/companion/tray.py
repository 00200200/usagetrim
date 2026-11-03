"""System tray and pet context menu; all actions target the existing window."""

from __future__ import annotations

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


class CompanionTray:
    def __init__(self, window):
        self.window = window
        self.menu = QMenu(window)
        self.icon = None
        self.available = False
        self._closed = False
        self.actions = {}
        for key, label, callback in (
            ("show_pet", "Show Desktop Pet", window.show_pet),
            ("hide_pet", "Hide Desktop Pet", window.hide_pet),
            ("statistics", "Show Statistics", window.show_statistics),
            ("controls", "Show Pet Controls", self._controls_triggered),
            ("pause", "Pause Optimization", window.toggle_pause),
            ("top", "Always on Top", self._top_triggered),
            ("animations", "Mint Animations", self._animations_triggered),
            ("quit", "Quit", window.close),
        ):
            action = QAction(label, self.menu)
            if key in {"controls", "top", "animations"}:
                action.setCheckable(True)
                # Explicit bool overload and Python handlers avoid Qt treating
                # a bound native method as a zero-argument callback.
                action.triggered[bool].connect(callback)
            else:
                # Discard QAction's checked argument for ordinary commands.
                action.triggered.connect(lambda checked=False, call=callback: call())
            self.menu.addAction(action)
            self.actions[key] = action
        self.menu.aboutToShow.connect(self.refresh_availability)
        self.refresh_availability()

    def _top_triggered(self, checked: bool):
        if not self._closed:
            # The checkbox's existing toggled handler performs the single save.
            self.window.top.setChecked(checked)

    def _controls_triggered(self, checked: bool):
        if not self._closed:
            self.window.set_pet_controls_visible(checked)

    def _animations_triggered(self, checked: bool):
        if not self._closed:
            self.window.animations.setChecked(checked)

    def refresh_availability(self):
        if self._closed:
            return
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        if self.available and self.icon is None:
            self.icon = QSystemTrayIcon(QIcon(self.window.artwork.pixmap()), self.window)
            self.icon.setContextMenu(self.menu)
            self.icon.show()
        elif not self.available and self.icon is not None:
            self.icon.hide()
            self.icon.setContextMenu(None)
            self.icon.deleteLater()
            self.icon = None
        self.window.tray_availability_changed(self.available)
        self.sync()

    def sync(self):
        if self._closed:
            return
        window = self.window
        visible = window.isVisible() and not window.isMinimized()
        visible_pet = visible and window._mode == "pet"
        self.actions["show_pet"].setEnabled(not window._closed and not visible_pet)
        self.actions["hide_pet"].setEnabled(not window._closed and visible_pet and self.available)
        self.actions["statistics"].setEnabled(
            not window._closed and not (visible and window._mode == "panel")
        )
        self.actions["controls"].setEnabled(
            not window._closed and window._mode == "pet" and self.available
        )
        self.actions["controls"].setChecked(not window.controls.isHidden())
        paused = window._snapshot is not None and window._snapshot["paused"]
        self.actions["pause"].setText("Resume Optimization" if paused else "Pause Optimization")
        self.actions["pause"].setEnabled(not window._closed and window.pause_button.isEnabled())
        self.actions["top"].setChecked(window.top.isChecked())
        self.actions["top"].setEnabled(not window._closed and window.top.isEnabled())
        self.actions["animations"].setChecked(window.animations.isChecked())
        self.actions["animations"].setEnabled(not window._closed)
        self.actions["quit"].setEnabled(not window._closed)
        if self.icon is not None:
            state = (
                "State unavailable"
                if window._snapshot is None
                else ("Optimization paused" if paused else "Local monitor active")
            )
            self.icon.setToolTip(f"UsageTrim Mint — {state}")

    def popup(self, global_position):
        if not self._closed:
            self.refresh_availability()
            self.menu.popup(global_position)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.menu.close()
        if self.icon is not None:
            self.icon.hide()
            self.icon.setContextMenu(None)
            self.icon.deleteLater()
            self.icon = None
        self.menu.deleteLater()
        self.window = None
