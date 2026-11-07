"""Cached Mint artwork, lightweight local reactions and desktop capabilities."""

from __future__ import annotations

import ctypes
import ctypes.util
import math
import sys
from enum import Enum
from importlib.resources import files

from PySide6.QtCore import (
    Property,
    QAbstractAnimation,
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPixmap
from PySide6.QtWidgets import QLabel


def supports_positioning() -> bool:
    return QGuiApplication.platformName().lower() in {"xcb", "windows", "offscreen"}


def supports_transparency() -> bool:
    """Require a compositor; unknown plugins fall back to a decorated window."""
    plugin = QGuiApplication.platformName().lower()
    try:
        if plugin == "windows" and sys.platform == "win32":
            enabled = ctypes.c_int()
            return ctypes.windll.dwmapi.DwmIsCompositionEnabled(
                ctypes.byref(enabled)
            ) == 0 and bool(enabled.value)
        if plugin == "xcb":
            library = ctypes.util.find_library("X11")
            if not library:
                return False
            x11 = ctypes.CDLL(library)
            x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
            x11.XDefaultScreen.restype = ctypes.c_int
            x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
            x11.XInternAtom.restype = ctypes.c_ulong
            x11.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            x11.XGetSelectionOwner.restype = ctypes.c_ulong
            x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            x11.XCloseDisplay.restype = ctypes.c_int
            display = x11.XOpenDisplay(None)
            if not display:
                return False
            try:
                selection = f"_NET_WM_CM_S{x11.XDefaultScreen(display)}".encode("ascii")
                atom = x11.XInternAtom(display, selection, True)
                return bool(atom and x11.XGetSelectionOwner(display, atom))
            finally:
                x11.XCloseDisplay(display)
    except (OSError, AttributeError):
        return False
    return False


class PetState(Enum):
    IDLE = "idle"
    PAUSED = "paused"
    ACTIVE = "active"
    UNAVAILABLE = "unavailable"


class MintArtwork(QLabel):
    position_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("Mint robot. Drag to move; use Panel to see statistics.")
        pixmap = QPixmap()
        pixmap.loadFromData(
            files("usagetrim.companion").joinpath("resources/mint.png").read_bytes()
        )
        self.setPixmap(
            pixmap.scaled(
                176,
                176,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(192, 184)
        self._offset = None
        self.state = PetState.UNAVAILABLE
        self.animations_enabled = True
        self._motion_visible = False
        self._shutdown = False
        self._vertical_offset = 0.0
        self._phase = 0
        self._baseline = None
        self.motion_timer = QTimer(self)
        self.motion_timer.setInterval(100)  # Ten small cached bitmap redraws per second.
        self.motion_timer.timeout.connect(self._advance_motion)
        self.activity_timer = QTimer(self)
        self.activity_timer.setSingleShot(True)
        self.activity_timer.setInterval(2000)
        self.activity_timer.timeout.connect(self._finish_activity)
        self.return_animation = QPropertyAnimation(self, b"vertical_offset", self)
        self.return_animation.setDuration(180)
        self.return_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._update_description()

    def _get_vertical_offset(self):
        return self._vertical_offset

    def _set_vertical_offset(self, value):
        value = float(value)
        if value != self._vertical_offset:
            self._vertical_offset = value
            self.update()

    vertical_offset = Property(float, _get_vertical_offset, _set_vertical_offset)

    def update_snapshot(self, snapshot: dict | None):
        if self._shutdown:
            return
        if snapshot is None:
            self.set_state(PetState.UNAVAILABLE)
            return
        today = snapshot.get("today")
        count = today["events"] if today is not None else 0
        days = snapshot.get("days", [])
        day = days[-1]["date"] if days else None
        previous = self._baseline
        self._baseline = (day, count)
        new_events = previous is not None and day == previous[0] and count > previous[1]
        if snapshot["paused"]:
            self.set_state(PetState.PAUSED)
        elif new_events:
            self.set_state(PetState.ACTIVE)
        elif (
            previous is None
            or day != previous[0]
            or count < previous[1]
            or self.state is not PetState.ACTIVE
            or not self.activity_timer.isActive()
        ):
            self.set_state(PetState.IDLE)

    def set_state(self, state: PetState):
        if self._shutdown:
            return
        self.state = state
        self._update_description()
        if state is not PetState.ACTIVE:
            self.activity_timer.stop()
        elif self._motion_visible:
            self.activity_timer.start()
        self._sync_motion()
        self.update()

    def _update_description(self):
        descriptions = {
            PetState.IDLE: "No new local measurements observed",
            PetState.PAUSED: "Optimization paused",
            PetState.ACTIVE: "New local measurements observed",
            PetState.UNAVAILABLE: "Local monitor unavailable",
        }
        description = f"Mint: {descriptions[self.state]}. Drag to move; right-click for controls."
        self.setToolTip(description)
        self.setAccessibleName(description)

    def _finish_activity(self):
        if not self._shutdown and self.state is PetState.ACTIVE:
            self.set_state(PetState.IDLE)

    def set_animations_enabled(self, enabled: bool):
        if not self._shutdown:
            self.animations_enabled = enabled
            self._sync_motion()

    def _sync_motion(self):
        moving = (
            not self._shutdown
            and self._motion_visible
            and self.animations_enabled
            and self.state in {PetState.IDLE, PetState.ACTIVE}
        )
        if moving:
            self.return_animation.stop()
            if not self.motion_timer.isActive():
                self.motion_timer.start()
        else:
            self.motion_timer.stop()
            self._phase = 0
            if self._motion_visible and self.animations_enabled and self._vertical_offset:
                if self.return_animation.state() != QAbstractAnimation.State.Running:
                    self.return_animation.setStartValue(self._vertical_offset)
                    self.return_animation.setEndValue(0.0)
                    self.return_animation.start()
            else:
                self.return_animation.stop()
                self.vertical_offset = 0.0

    def _advance_motion(self):
        if (
            self._shutdown
            or not self._motion_visible
            or not self.animations_enabled
            or self.state not in {PetState.IDLE, PetState.ACTIVE}
        ):
            return
        self._phase = (self._phase + 1) % 40
        amplitude = 3 if self.state is PetState.ACTIVE else 2
        self.vertical_offset = -amplitude * (1 - math.cos(self._phase * math.tau / 40)) / 2

    def resume_motion(self):
        if not self._shutdown:
            self._motion_visible = self.isVisible() and not self.window().isMinimized()
            if self._motion_visible and self.state is PetState.ACTIVE:
                self.activity_timer.start()
            self._sync_motion()

    def suspend_motion(self):
        self._motion_visible = False
        self.activity_timer.stop()
        self._sync_motion()

    def shutdown(self):
        self._shutdown = True
        self.suspend_motion()

    def showEvent(self, event):
        super().showEvent(event)
        self.resume_motion()

    def hideEvent(self, event):
        self.suspend_motion()
        super().hideEvent(event)

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        pixmap = self.pixmap()
        painter.setOpacity(0.6 if self.state is PetState.PAUSED else 1.0)
        painter.drawPixmap(
            (self.width() - pixmap.width()) // 2,
            (self.height() - pixmap.height()) // 2 + round(self._vertical_offset),
            pixmap,
        )
        painter.setOpacity(1.0)
        colors = {
            PetState.IDLE: "#31b987",
            PetState.PAUSED: "#b47818",
            PetState.ACTIVE: "#238ea8",
            PetState.UNAVAILABLE: "#7d8791",
        }
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colors[self.state]))
        painter.drawEllipse(self.width() - 26, self.height() - 20, 8, 8)

    def _start_system_move(self) -> bool:
        handle = self.window().windowHandle()
        return handle is not None and handle.startSystemMove()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._start_system_move():
                event.accept()
                return
            if supports_positioning():
                self._offset = event.globalPosition().toPoint() - self.window().pos()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._offset = None
            self.position_changed.emit()
        super().mouseReleaseEvent(event)
