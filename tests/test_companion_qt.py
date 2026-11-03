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
    window = CompanionWindow(
        CompanionController(service),
        executor=ImmediateExecutor(),
        preference_executor=ImmediateExecutor(),
    )
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
    window = CompanionWindow(
        controller, executor=ImmediateExecutor(), preference_executor=ImmediateExecutor()
    )
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
    window = qt.CompanionWindow(CompanionController(), preference_executor=ImmediateExecutor())
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


def test_mint_resource_renders_with_alpha(qt_app):
    from PySide6.QtGui import QImage

    from usagetrim.companion.pet import MintArtwork

    artwork = MintArtwork()
    try:
        image = artwork.pixmap().toImage()
        assert not image.isNull()
        assert image.hasAlphaChannel()
        assert image.width() <= 176 and image.height() <= 176
        target = QImage(artwork.size(), QImage.Format.Format_ARGB32)
        target.fill(0)
        artwork.render(target)
        assert any(
            target.pixelColor(x, y).alpha() > 0
            for x in range(target.width())
            for y in range(target.height())
        )
    finally:
        artwork.close()


def test_pet_panel_switching_and_transparency_fallback(qt_app, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt

    from usagetrim.companion import qt
    from usagetrim.companion.controller import CompanionController
    from usagetrim.core.monitor import Monitor

    monkeypatch.setattr(qt, "supports_transparency", lambda: True)
    window = qt.CompanionWindow(
        CompanionController(Monitor([], tmp_path)),
        executor=ImmediateExecutor(),
        preference_executor=ImmediateExecutor(),
    )
    try:
        window.poll()
        window.mode_button.click()
        window.poll()
        assert window._mode == "pet"
        assert not window.artwork.isHidden()
        assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
        assert window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert window.mode_button.text() == "Statistics panel"
        assert window.pause_button.isEnabled()
        window.top.setChecked(True)
        window.poll()
        assert window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
        assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
        window.mode_button.click()
        window.poll()
        assert window._mode == "panel"
        assert window.artwork.isHidden()
        assert not window.windowFlags() & Qt.WindowType.FramelessWindowHint
        monkeypatch.setattr(qt, "supports_transparency", lambda: False)
        window.set_mode("pet")
        window.poll()
        assert not window.windowFlags() & Qt.WindowType.FramelessWindowHint
        assert not window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert window.autoFillBackground()
        assert not window.pet_hint.isHidden()
    finally:
        window.close()


def test_pet_manual_drag_saves_and_restores_position(qt_app, tmp_path, monkeypatch):
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    from usagetrim.companion import pet, qt
    from usagetrim.companion.controller import CompanionController
    from usagetrim.core.companion_state import settings
    from usagetrim.core.monitor import Monitor

    monkeypatch.setattr(pet, "supports_positioning", lambda: True)
    monkeypatch.setattr(qt, "supports_positioning", lambda: True)
    controller = CompanionController(Monitor([], tmp_path))
    window = qt.CompanionWindow(
        controller, executor=ImmediateExecutor(), preference_executor=ImmediateExecutor()
    )

    def event(kind, point, button, buttons):
        return QMouseEvent(
            kind, QPointF(20, 20), QPointF(point), button, buttons, Qt.KeyboardModifier.NoModifier
        )

    try:
        window.poll()
        window.set_mode("pet")
        window.poll()
        # Force the manual path independently of native plugin drag support.
        monkeypatch.setattr(window.artwork, "_start_system_move", lambda: False)
        window.move(20, 30)
        window.artwork.mousePressEvent(
            event(
                QEvent.Type.MouseButtonPress,
                QPoint(40, 50),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
            )
        )
        window.artwork.mouseMoveEvent(
            event(
                QEvent.Type.MouseMove,
                QPoint(80, 90),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton,
            )
        )
        assert window.pos() == QPoint(60, 70)
        window.artwork.mouseReleaseEvent(
            event(
                QEvent.Type.MouseButtonRelease,
                QPoint(80, 90),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
            )
        )
        window._flush_preferences()
        window.poll()
        assert settings()["companion_qt_pet_position"] == [60, 70]
        assert settings()["companion_qt_mode"] == "pet"
    finally:
        window.close()
    restored = qt.CompanionWindow(
        controller, executor=ImmediateExecutor(), preference_executor=ImmediateExecutor()
    )
    try:
        assert restored._mode == "pet"
        assert restored.pos() == QPoint(60, 70)
    finally:
        restored.close()


def test_wayland_pet_does_not_restore_or_save_global_position(qt_app, tmp_path, monkeypatch):
    from usagetrim.companion import qt
    from usagetrim.companion.controller import CompanionController
    from usagetrim.core.companion_state import settings, update_settings
    from usagetrim.core.monitor import Monitor

    update_settings(companion_qt_mode="pet", companion_qt_pet_position=[123, 234])
    monkeypatch.setattr(qt, "supports_positioning", lambda: False)
    monkeypatch.setattr(qt, "supports_transparency", lambda: False)
    window = qt.CompanionWindow(
        CompanionController(Monitor([], tmp_path)),
        executor=ImmediateExecutor(),
        preference_executor=ImmediateExecutor(),
    )
    try:
        window.poll()
        window.move(50, 60)
        window.save_pet_position()
        window.poll()
        assert settings()["companion_qt_pet_position"] == [123, 234]
        assert not window.pet_hint.isHidden()
    finally:
        window.close()


def test_wayland_capabilities_fall_back_without_native_probes(qt_app, monkeypatch):
    from types import SimpleNamespace

    from usagetrim.companion import pet

    def forbidden(*args, **kwargs):
        pytest.fail("Wayland must not assume X11 compositor or global positioning")

    monkeypatch.setattr(pet, "QGuiApplication", SimpleNamespace(platformName=lambda: "wayland"))
    monkeypatch.setattr(pet.ctypes, "CDLL", forbidden)
    assert pet.supports_transparency() is False
    assert pet.supports_positioning() is False


def test_pet_closes_with_running_monitor_without_late_updates(qt_app, tmp_path, monkeypatch):
    from usagetrim.companion import qt
    from usagetrim.companion.controller import CompanionController
    from usagetrim.core.companion_state import update_settings

    update_settings(companion_qt_mode="pet")

    class PendingExecutor:
        def __init__(self):
            self.future = Future()
            self.future.set_running_or_notify_cancel()

        def submit(self, *args):
            return self.future

    executor = PendingExecutor()
    window = qt.CompanionWindow(
        CompanionController(), executor=executor, preference_executor=ImmediateExecutor()
    )
    before = window.summary.text()
    window.close()
    executor.future.set_result({"today": None, "paused": True, "issues": []})
    window.poll()
    window.save_pet_position()
    assert window.summary.text() == before
    assert window._pending is None
    assert not window.position_timer.isActive()


def test_final_preferences_survive_close_while_monitor_never_finishes(qt_app, monkeypatch):
    from PySide6.QtCore import QPoint

    from usagetrim.companion import qt
    from usagetrim.companion.controller import CompanionController

    class PendingMonitorExecutor:
        def __init__(self):
            self.future = Future()
            self.future.set_running_or_notify_cancel()
            self.submissions = 0

        def submit(self, *args):
            self.submissions += 1
            return self.future

    controller = CompanionController()

    def forbidden():
        pytest.fail("preference persistence must not call snapshot")

    monkeypatch.setattr(controller, "snapshot", forbidden)
    monkeypatch.setattr(qt, "supports_positioning", lambda: True)
    monkeypatch.setattr(qt, "supports_transparency", lambda: False)
    executor = PendingMonitorExecutor()
    window = qt.CompanionWindow(
        controller, executor=executor, preference_executor=ImmediateExecutor()
    )
    assert window._mode == "panel"
    assert not executor.future.done()
    window.set_mode("pet")
    window.move(QPoint(90, 110))
    # Do not deliver either debounce timer: closing must capture these coordinates.
    window.close()
    assert window._closed
    assert window._pending is None
    assert not executor.future.done()
    assert executor.submissions == 1
    assert CompanionController().window_preferences() == {
        "mode": "pet",
        "position": [90, 110],
    }
    assert not window.preferences_timer.isActive()
    executor.future.set_result({"today": None, "paused": False, "issues": []})
    before = window.summary.text()
    window.poll()
    assert window.summary.text() == before


def test_preference_result_is_not_a_telemetry_snapshot(qt_app, tmp_path, monkeypatch):
    from usagetrim.companion import qt
    from usagetrim.companion.controller import CompanionController
    from usagetrim.core.monitor import Monitor

    controller = CompanionController(Monitor([], tmp_path))
    window = qt.CompanionWindow(
        controller, executor=ImmediateExecutor(), preference_executor=ImmediateExecutor()
    )
    try:
        window.poll()
        snapshot = window._snapshot

        def forbidden():
            pytest.fail("preference save must not refresh telemetry")

        monkeypatch.setattr(controller, "snapshot", forbidden)
        window.set_mode("pet")
        window._flush_preferences()
        window.poll()
        assert window._snapshot is snapshot
        assert window._pending is None
        assert window.pause_button.isEnabled()
    finally:
        window.close()
