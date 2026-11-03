"""Qt initialization and controls use the offscreen plugin, never a display server."""

from concurrent.futures import Future

import pytest


@pytest.fixture
def qt_app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(["companion-test"])
    yield app
    app.processEvents()


class ImmediateExecutor:
    def submit(self, function, *args):
        future = Future()
        try:
            future.set_result(function(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future


def test_qt_initialization_real_monitor_and_settings(qt_app, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt

    from usagetrim.companion.controller import CompanionController
    from usagetrim.companion.qt import CompanionWindow
    from usagetrim.core.companion_state import settings
    from usagetrim.core.monitor import Monitor

    monkeypatch.setenv("USAGETRIM_STATE_DIR", str(tmp_path / "state"))
    service = Monitor([], tmp_path / "collector")
    service.store.record(100, 30, 100, 30, event_id="qt-real")
    window = CompanionWindow(CompanionController(service), executor=ImmediateExecutor())
    try:
        window.poll()
        assert "Raw: 100" in window.summary.text()
        assert "+70" in window.summary.text()
        assert not window.windowFlags() & Qt.WindowType.FramelessWindowHint
        assert window.refresh_timer.interval() == 5000
        window.pause_button.click()
        window.poll()
        assert settings()["paused"] is True
        assert window.pause_button.text() == "Resume optimization"
        window.top.setChecked(True)
        window.poll()
        assert settings()["companion_qt_always_on_top"] is True
        assert window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    finally:
        window.close()
    assert not window.refresh_timer.isActive()
    assert not window.poll_timer.isActive()


def test_qt_empty_state_and_refresh_failure_are_safe(qt_app, tmp_path):
    from usagetrim.companion.controller import CompanionController
    from usagetrim.companion.qt import CompanionWindow
    from usagetrim.core.monitor import Monitor

    controller = CompanionController(Monitor([], tmp_path))
    window = CompanionWindow(controller, executor=ImmediateExecutor())
    try:
        window.poll()
        assert "No returned UsageTrim measurements" in window.summary.text()

        def unavailable():
            raise OSError("private-path-must-not-appear")

        controller.snapshot = unavailable
        window.refresh()
        window.poll()
        assert "unavailable" in window.status.text()
        assert "private-path" not in window.status.text()
        assert window.refresh_button.isEnabled()
        assert window._snapshot is None
        assert not window.pause_button.isEnabled()
        # Even a direct action invocation must not use the last successful state.
        pause_calls = []
        controller.set_paused = lambda value: pause_calls.append(value)
        window.toggle_pause()
        assert pause_calls == []
        assert window._pending is None

        controller.snapshot = lambda: {"today": None, "paused": True, "issues": []}
        window.refresh()
        window.poll()
        assert window.pause_button.isEnabled()
        assert window.pause_button.text() == "Resume optimization"
        window.toggle_pause()
        assert pause_calls == [False]
    finally:
        window.close()


@pytest.mark.parametrize("running", [True, False])
def test_qt_close_detaches_unfinished_task_without_waiting(qt_app, monkeypatch, running):
    from usagetrim.companion import qt
    from usagetrim.companion.controller import CompanionController

    class ControlledExecutor:
        def __init__(self):
            self.future = Future()
            if running:
                self.future.set_running_or_notify_cancel()
            self.shutdown_calls = []
            self.submissions = 0

        def submit(self, function, *args):
            self.submissions += 1
            return self.future

        def shutdown(self, *, wait, cancel_futures):
            # No actual thread or blocking operation exists in this test.
            assert wait is False
            self.shutdown_calls.append((wait, cancel_futures))

    executor = ControlledExecutor()
    monkeypatch.setattr(qt, "ThreadPoolExecutor", lambda **kwargs: executor)
    window = qt.CompanionWindow(CompanionController())
    assert window._pending is executor.future
    assert not executor.future.done()
    window.close()
    assert window._closed is True
    assert window._pending is None
    assert executor.shutdown_calls == [(False, True)]
    assert not window.refresh_timer.isActive()
    assert not window.poll_timer.isActive()
    assert not window.pause_button.isEnabled()
    assert executor.future.cancelled() is (not running)
    previous = (window.summary.text(), window.status.text())
    if running:
        executor.future.set_result({"today": None, "paused": True, "issues": []})
    # Simulate late timer deliveries and actions after a worker finishes.
    window.poll()
    window.refresh()
    window.toggle_pause()
    qt_app.processEvents()
    assert (window.summary.text(), window.status.text()) == previous
    assert executor.submissions == 1
    window.close()
    assert executor.shutdown_calls == [(False, True)]


def test_qt_backend_headless_failure_is_controlled(qt_app, monkeypatch):
    from usagetrim.companion import CompanionUnavailable
    from usagetrim.companion.qt import QtBackend

    monkeypatch.setattr("usagetrim.companion.qt.platform.system", lambda: "Linux")
    for name in ("QT_QPA_PLATFORM", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(CompanionUnavailable, match="graphical desktop"):
        QtBackend().run()
