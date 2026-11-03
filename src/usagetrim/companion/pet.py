"""Static Mint artwork and conservative native desktop capabilities."""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
from importlib.resources import files

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication, QPixmap
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
