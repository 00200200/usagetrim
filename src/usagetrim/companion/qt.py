"""Opaque native Qt window for Linux and Windows; no tray or transparency tricks."""

from __future__ import annotations

import os
import platform
from concurrent.futures import Executor, ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer
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


class CompanionWindow(QWidget):
    def __init__(
        self, controller: CompanionController | None = None, *, executor: Executor | None = None
    ):
        super().__init__()
        self.controller = controller or CompanionController()
        self.executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="companion"
        )
        self._owns_executor = executor is None
        self._pending = None
        self._closed = False
        self._snapshot = None
        self.setWindowTitle("UsageTrim Desktop Companion")
        self.resize(360, 260)
        layout = QVBoxLayout(self)
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
        if self._closed or self._pending is None or not self._pending.done():
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
        self._closed = True
        self.refresh_timer.stop()
        self.poll_timer.stop()
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
