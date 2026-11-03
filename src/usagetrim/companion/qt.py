"""Panel and static Mint pet sharing one monitor and a safe window fallback."""

from __future__ import annotations

import os
import platform
from concurrent.futures import Executor, ThreadPoolExecutor

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from usagetrim.companion import CompanionUnavailable
from usagetrim.companion.controller import CompanionController
from usagetrim.companion.pet import MintArtwork, supports_positioning, supports_transparency
from usagetrim.companion.preferences import PreferencesWriter


class CompanionWindow(QWidget):
    def __init__(
        self,
        controller: CompanionController | None = None,
        *,
        executor: Executor | None = None,
        preference_executor: Executor | None = None,
    ):
        super().__init__()
        self.controller = controller or CompanionController()
        self.executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="companion"
        )
        self._owns_executor = executor is None
        self._preferences = PreferencesWriter(self.controller, executor=preference_executor)
        self._pending = None
        self._closed = False
        self._snapshot = None
        self._mode = "panel"
        self._pet_position = None
        self._applying_mode = False
        self.setWindowTitle("UsageTrim Desktop Companion")
        self.resize(360, 260)
        root = QVBoxLayout(self)
        self.artwork = MintArtwork(self)
        root.addWidget(self.artwork, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.controls = QWidget(self)
        self.controls.setObjectName("pet-controls")
        root.addWidget(self.controls)
        layout = QVBoxLayout(self.controls)
        self.summary = QLabel("Loading local measurements…")
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.status = QLabel("OpenAI token estimates, not provider billing.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.pause_button = QPushButton("Pause optimization")
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self.toggle_pause)
        layout.addWidget(self.pause_button)
        self.top = QCheckBox("Always on top (window manager permitting)")
        self.top.setChecked(self.controller.always_on_top())
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.top.isChecked())
        self.top.toggled.connect(self.change_top)
        layout.addWidget(self.top)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        layout.addWidget(self.refresh_button)
        self.mode_button = QPushButton("Desktop Pet")
        self.mode_button.clicked.connect(self.toggle_mode)
        layout.addWidget(self.mode_button)
        self.quit_button = QPushButton("Quit")
        self.quit_button.clicked.connect(self.close)
        layout.addWidget(self.quit_button)
        self.pet_hint = QLabel()
        self.pet_hint.setWordWrap(True)
        layout.addWidget(self.pet_hint)
        self.position_timer = QTimer(self)
        self.position_timer.setSingleShot(True)
        self.position_timer.setInterval(200)
        self.position_timer.timeout.connect(self.save_pet_position)
        self.artwork.position_changed.connect(self.save_pet_position)
        self.preferences_timer = QTimer(self)
        self.preferences_timer.setSingleShot(True)
        self.preferences_timer.setInterval(200)
        self.preferences_timer.timeout.connect(self._flush_preferences)
        preferences = self.controller.window_preferences()
        self._pet_position = preferences["position"]
        self.set_mode(preferences["mode"], persist=False)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(5000)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start()
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(100)
        self.poll_timer.timeout.connect(self.poll)
        self.poll_timer.start()
        self.refresh()

    def _submit(self, function, *args):
        if self._closed or self._pending is not None:
            return
        self.pause_button.setEnabled(False)
        self.top.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self._pending = self.executor.submit(function, *args)

    def refresh(self):
        self._submit(self.controller.snapshot)

    def toggle_mode(self):
        self.set_mode("panel" if self._mode == "pet" else "pet")

    def set_mode(self, mode: str, *, persist: bool = True):
        if self._closed:
            return
        if mode not in {"panel", "pet"}:
            raise ValueError("invalid companion mode")
        self._applying_mode = True
        was_visible = self.isVisible()
        if self._mode == "pet" and supports_positioning():
            self._pet_position = [self.x(), self.y()]
        self._mode = mode
        pet = mode == "pet"
        transparent = pet and supports_transparency()
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, transparent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, transparent)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, transparent)
        self.setAutoFillBackground(not transparent)
        self.artwork.setVisible(pet)
        self.refresh_button.setVisible(not pet)
        self.status.setVisible(not pet)
        self.mode_button.setText("Statistics panel" if pet else "Desktop Pet")
        self.pet_hint.setVisible(pet and not transparent)
        self.pet_hint.setText(
            "Using a normal window: transparency or positioning is not confirmed. "
            "Drag the title bar if dragging Mint is unavailable."
        )
        self.controls.setStyleSheet(
            "#pet-controls { background: #ecf8f2; border-radius: 14px; }"
            "#pet-controls QLabel, #pet-controls QCheckBox { color: #193b30; }"
            if pet
            else ""
        )
        self.adjustSize()
        if pet and self._pet_position is not None:
            self.restore_pet_position(self._pet_position)
        if was_visible:
            self.show()
        self._applying_mode = False
        if persist:
            self._queue_preferences()

    def restore_pet_position(self, position: list[int]):
        if not supports_positioning():
            return
        point = QPoint(*position)
        screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(
            max(area.left(), min(point.x(), area.right() - self.width() + 1)),
            max(area.top(), min(point.y(), area.bottom() - self.height() + 1)),
        )

    def _queue_preferences(self):
        if self._closed:
            return
        position = self._pet_position if supports_positioning() else None
        if self._mode == "pet" and supports_positioning():
            position = [self.x(), self.y()]
            self._pet_position = position
        self._preferences.queue(self._mode, position)
        self.preferences_timer.start()

    def _flush_preferences(self):
        if self._closed:
            return
        try:
            self._preferences.flush()
        except Exception:
            self._preference_error()

    def _preference_error(self):
        message = "Window preferences could not be saved. Retry or check local storage."
        self.status.setText(message)
        if self._mode == "pet":
            self.pet_hint.setText(message)
            self.pet_hint.show()

    def save_pet_position(self):
        self.position_timer.stop()
        if self._mode == "pet" and supports_positioning():
            self._queue_preferences()

    def moveEvent(self, event):
        super().moveEvent(event)
        if (
            hasattr(self, "position_timer")
            and not self._closed
            and not self._applying_mode
            and self._mode == "pet"
            and self.isVisible()
            and supports_positioning()
        ):
            self.position_timer.start()

    def toggle_pause(self):
        if self._snapshot is not None:
            self._submit(self.controller.set_paused, not self._snapshot["paused"])

    def change_top(self, checked: bool):
        # A running task may outlive the window; do not retain the QWidget in it.
        controller = self.controller

        def update():
            controller.set_always_on_top(checked)
            return controller.snapshot()

        self._submit(update)

    def poll(self):
        if self._closed:
            return
        try:
            self._preferences.poll()
        except Exception:
            self._preference_error()
        if self._pending is None or not self._pending.done():
            return
        pending, self._pending = self._pending, None
        try:
            snapshot = pending.result()
        except Exception:
            # The GUI boundary must survive monitor/storage failures. Do not show
            # raw exception payloads, which may contain private filesystem paths.
            self._snapshot = None
            self.pause_button.setText("Pause unavailable")
            self.summary.setText("Local measurements unavailable until the next refresh.")
            self.status.setText("Local monitor unavailable. Retrying on the next refresh.")
            self.top.blockSignals(True)
            self.top.setChecked(self.controller.always_on_top())
            self.top.blockSignals(False)
        else:
            self._snapshot = snapshot
            today = snapshot.get("today")
            if today is None:
                self.summary.setText("No returned UsageTrim measurements today.")
            else:
                self.summary.setText(
                    f"Today: {today['events']:,} operations\n"
                    f"Raw: {today['before']:,} · Compact: {today['after']:,}\n"
                    f"Net token reduction: {today['net']:+,}"
                )
            self.pause_button.setText(
                "Resume optimization" if snapshot["paused"] else "Pause optimization"
            )
            self.status.setText(
                ("Optimization paused. " if snapshot["paused"] else "")
                + "OpenAI token estimates, not billing. "
                + (
                    "Some local sources are unavailable."
                    if snapshot.get("issues")
                    else "Local monitor updated."
                )
            )
            self.top.blockSignals(True)
            on_top = self.controller.always_on_top()
            self.top.setChecked(on_top)
            self.top.blockSignals(False)
            if bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint) != on_top:
                self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on_top)
                self.show()
        self.pause_button.setEnabled(self._snapshot is not None)
        self.top.setEnabled(True)
        self.refresh_button.setEnabled(True)

    def closeEvent(self, event):
        if self._closed:
            event.accept()
            return
        # Capture the actual final coordinates even if the drag debounce has not
        # fired. The independent writer drains this state on normal shutdown.
        self._queue_preferences()
        try:
            self._preferences.close()
        except Exception:
            self._preference_error()
            event.ignore()
            return
        self._closed = True
        self.refresh_timer.stop()
        self.poll_timer.stop()
        self.position_timer.stop()
        self.preferences_timer.stop()
        self.mode_button.setEnabled(False)
        self.quit_button.setEnabled(False)
        self._snapshot = None
        self.pause_button.setEnabled(False)
        self.top.setEnabled(False)
        self.refresh_button.setEnabled(False)
        pending, self._pending = self._pending, None
        if pending is not None:
            # cancel() affects queued work only. Running monitor calls finish
            # naturally; no callback or timer may apply their result to this UI.
            pending.cancel()
        if self._owns_executor:
            # Never join a worker on the GUI thread. Python may still wait for
            # a running call at process exit; this MVP does not promise an
            # immediate process exit or attempt unsafe thread termination.
            self.executor.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(event)


class QtBackend:
    def run(self) -> int:
        if platform.system() == "Linux" and not any(
            os.environ.get(name) for name in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM")
        ):
            raise CompanionUnavailable("Desktop Companion requires a graphical desktop session.")
        app = QApplication.instance() or QApplication(["usagetrim-companion"])
        window = CompanionWindow()
        window.show()
        return app.exec()
