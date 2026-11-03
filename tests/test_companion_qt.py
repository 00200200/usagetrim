"""Qt initialization and controls use the offscreen plugin, never a display server."""

from concurrent.futures import Future

import pytest


@pytest.fixture
def qt_app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from usagetrim.companion import tray

    class NoTray:
        @staticmethod
        def isSystemTrayAvailable():
            return False

    monkeypatch.setattr(tray, "QSystemTrayIcon", NoTray)

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


@pytest.fixture
def mock_system_tray(qt_app, monkeypatch):
    from usagetrim.companion import tray

    class FakeTray:
        available = True
        instances = []

        @classmethod
        def isSystemTrayAvailable(cls):
            return cls.available

        def __init__(self, icon, parent):
            self.icon = icon
            self.parent = parent
            self.visible = False
            self.deleted = False
            self.menu = None
            self.tooltip = ""
            self.instances.append(self)

        def setContextMenu(self, menu):
            self.menu = menu

        def setToolTip(self, value):
            self.tooltip = value

        def show(self):
            self.visible = True

        def hide(self):
            self.visible = False

        def deleteLater(self):
            self.deleted = True

    monkeypatch.setattr(tray, "QSystemTrayIcon", FakeTray)
    return FakeTray


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


def test_tray_initialization_show_hide_and_statistics_share_monitor(
    qt_app, tmp_path, mock_system_tray
):
    from usagetrim.companion.controller import CompanionController
    from usagetrim.companion.qt import CompanionWindow
    from usagetrim.core.monitor import Monitor

    class RecordingExecutor(ImmediateExecutor):
        def __init__(self):
            self.calls = []

        def submit(self, function, *args):
            self.calls.append((function, args))
            return super().submit(function, *args)

    service = Monitor([], tmp_path)
    service.store.record(100, 30, 100, 30, event_id="tray-real")
    controller = CompanionController(service)
    executor = RecordingExecutor()
    window = CompanionWindow(controller, executor=executor, preference_executor=ImmediateExecutor())
    try:
        window.show()
        window.poll()
        assert len(mock_system_tray.instances) == 1
        icon = mock_system_tray.instances[0]
        assert icon.visible and icon.menu is window.tray.menu
        assert not icon.icon.isNull()
        assert "+70" in window.summary.text()
        actions = window.tray.actions
        assert actions["pause"].isEnabled()
        actions["show_pet"].trigger()
        assert window.isVisible() and window._mode == "pet"
        assert window.artwork.isVisible()
        assert actions["hide_pet"].isEnabled()
        actions["hide_pet"].trigger()
        assert not window.isVisible()
        assert not window._closed
        assert window.refresh_timer.isActive() and window.poll_timer.isActive()
        assert icon.visible
        assert actions["show_pet"].isEnabled()
        actions["statistics"].trigger()
        assert window.isVisible() and window._mode == "panel"
        assert window.controls.isVisible()
        assert window.artwork.isHidden()
        window.showMinimized()
        window.tray.sync()
        assert actions["statistics"].isEnabled()
        actions["statistics"].trigger()
        assert not window.isMinimized()
        assert window.controller is controller and controller._monitor is service
        assert window.executor is executor
        assert len(executor.calls) == 1  # Show/hide/menu state performs no monitor reads.
        window.tray.refresh_availability()
        assert len(mock_system_tray.instances) == 1
    finally:
        window.close()


def test_robot_only_controls_and_context_menu_restore(qt_app, tmp_path, mock_system_tray):
    from PySide6.QtCore import QPoint

    from usagetrim.companion.controller import CompanionController
    from usagetrim.companion.qt import CompanionWindow
    from usagetrim.core.monitor import Monitor

    window = CompanionWindow(
        CompanionController(Monitor([], tmp_path)),
        executor=ImmediateExecutor(),
        preference_executor=ImmediateExecutor(),
    )
    try:
        window.show_pet()
        window.poll()
        assert window.hide_controls_button.isVisible()
        window.hide_controls_button.click()
        assert window.artwork.isVisible()
        assert window.controls.isHidden()
        assert not window.tray.actions["controls"].isChecked()
        assert window.refresh_timer.isActive()
        # The same QMenu is available from Mint's right-click handler.
        popup_calls = []
        window.tray.popup = lambda position: popup_calls.append(position)
        window.artwork.customContextMenuRequested.emit(QPoint(10, 10))
        assert len(popup_calls) == 1
        window.tray.actions["controls"].trigger()
        assert window.controls.isVisible()
        assert window.tray.actions["controls"].isChecked()
        window.tray.actions["statistics"].trigger()
        assert window._mode == "panel" and window.controls.isVisible()
    finally:
        window.close()


def test_no_tray_preserves_access_to_panel_and_quit(qt_app, tmp_path, mock_system_tray):
    from usagetrim.companion.controller import CompanionController
    from usagetrim.companion.qt import CompanionWindow
    from usagetrim.core.monitor import Monitor

    mock_system_tray.available = False
    window = CompanionWindow(
        CompanionController(Monitor([], tmp_path)),
        executor=ImmediateExecutor(),
        preference_executor=ImmediateExecutor(),
    )
    try:
        window.show_pet()
        window.poll()
        assert window.tray.icon is None
        assert mock_system_tray.instances == []
        window.set_pet_controls_visible(False)
        window.hide_pet()
        assert window.isVisible() and window.controls.isVisible()
        assert window.quit_button.isVisible() and window.mode_button.isVisible()
        assert not window.tray.actions["hide_pet"].isEnabled()
        assert not window.tray.actions["controls"].isEnabled()
        window.mode_button.click()
        assert window._mode == "panel" and window.controls.isVisible()
    finally:
        window.close()


def test_tray_disappearance_restores_hidden_window_and_controls(qt_app, tmp_path, mock_system_tray):
    from usagetrim.companion.controller import CompanionController
    from usagetrim.companion.qt import CompanionWindow
    from usagetrim.core.monitor import Monitor

    window = CompanionWindow(
        CompanionController(Monitor([], tmp_path)),
        executor=ImmediateExecutor(),
        preference_executor=ImmediateExecutor(),
    )
    try:
        window.show_pet()
        window.poll()
        window.set_pet_controls_visible(False)
        window.hide_pet()
        icon = mock_system_tray.instances[0]
        mock_system_tray.available = False
        window.refresh()
        window.poll()
        assert window.isVisible() and window.controls.isVisible()
        assert not icon.visible and icon.deleted
        assert window.tray.icon is None
        mock_system_tray.available = True
        window.tray.refresh_availability()
        assert window.tray.icon is not None
        assert len(mock_system_tray.instances) == 2
    finally:
        window.close()


def test_tray_pause_and_top_track_real_settings_without_unhiding(
    qt_app, tmp_path, mock_system_tray, monkeypatch
):
    from PySide6.QtCore import Qt

    from usagetrim.companion.controller import CompanionController
    from usagetrim.companion.qt import CompanionWindow
    from usagetrim.core.companion_state import settings
    from usagetrim.core.monitor import Monitor

    service = Monitor([], tmp_path)
    window = CompanionWindow(
        CompanionController(service),
        executor=ImmediateExecutor(),
        preference_executor=ImmediateExecutor(),
    )
    top_writes = []
    original_set_top = window.controller.set_always_on_top

    def record_top(value):
        top_writes.append(value)
        original_set_top(value)

    monkeypatch.setattr(window.controller, "set_always_on_top", record_top)
    try:
        window.show_pet()
        window.poll()
        actions = window.tray.actions
        actions["pause"].trigger()
        assert not actions["pause"].isEnabled()
        window.poll()
        assert settings()["paused"] is True
        assert actions["pause"].text() == "Resume Optimization"
        actions["pause"].trigger()
        window.poll()
        assert settings()["paused"] is False
        assert actions["pause"].text() == "Pause Optimization"
        window.hide_pet()
        actions["top"].trigger()
        assert window.top.isChecked()
        window.poll()
        assert not window.isVisible()
        assert actions["top"].isChecked() and window.top.isChecked()
        assert settings()["companion_qt_always_on_top"] is True
        assert window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
        assert top_writes == [True]
        # Checkbox -> QAction synchronization must not trigger another save.
        window.top.setChecked(False)
        assert not actions["top"].isChecked()
        window.poll()
        assert not settings()["companion_qt_always_on_top"]
        assert not window.isVisible()
        assert not window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
        window.tray.sync()
        window.tray.sync()
        assert top_writes == [True, False]

        def unavailable():
            raise OSError("monitor unavailable")

        window.controller.snapshot = unavailable
        window.refresh()
        window.poll()
        assert not actions["pause"].isEnabled()
        assert window._snapshot is None
    finally:
        window.close()


def test_tray_quit_closes_hidden_window_and_detaches_active_work(qt_app, mock_system_tray):
    from usagetrim.companion.controller import CompanionController
    from usagetrim.companion.qt import CompanionWindow

    class PendingExecutor:
        def __init__(self):
            self.future = Future()
            self.future.set_running_or_notify_cancel()

        def submit(self, *args):
            return self.future

    executor = PendingExecutor()
    window = CompanionWindow(
        CompanionController(), executor=executor, preference_executor=ImmediateExecutor()
    )
    window.show_pet()
    window.hide_pet()
    shutdowns = []
    window.shutdown_requested.connect(lambda: shutdowns.append(True))
    icon = mock_system_tray.instances[0]
    window.tray.actions["quit"].trigger()
    assert shutdowns == [True]
    assert window._closed and not window.isVisible()
    assert not icon.visible and icon.deleted
    assert window.tray._closed and window._preferences._closed
    assert all(
        not timer.isActive()
        for timer in (
            window.refresh_timer,
            window.poll_timer,
            window.position_timer,
            window.preferences_timer,
        )
    )
    assert not executor.future.done()
    before = window.summary.text()
    executor.future.set_result({"today": None, "paused": True, "issues": []})
    window.poll()
    assert window.summary.text() == before
    window.close()
    assert shutdowns == [True]


def test_backend_quit_exits_event_loop_even_when_window_hidden(
    qt_app, tmp_path, monkeypatch, mock_system_tray
):
    from types import SimpleNamespace

    from usagetrim.companion import qt
    from usagetrim.companion.controller import CompanionController
    from usagetrim.core.monitor import Monitor

    window_type = qt.CompanionWindow
    windows = []

    def create_window():
        window = window_type(
            CompanionController(Monitor([], tmp_path)),
            executor=ImmediateExecutor(),
            preference_executor=ImmediateExecutor(),
        )
        windows.append(window)
        return window

    class Application:
        quit_policy = True
        quit_calls = 0

        def quitOnLastWindowClosed(self):
            return self.quit_policy

        def setQuitOnLastWindowClosed(self, value):
            self.quit_policy = value

        def quit(self):
            self.quit_calls += 1

        def exec(self):
            assert not self.quit_policy
            window = windows[0]
            window.show_pet()
            window.hide_pet()
            window.tray.actions["quit"].trigger()
            assert self.quit_calls == 1
            return 0

    application = Application()
    monkeypatch.setattr(qt, "QApplication", SimpleNamespace(instance=lambda: application))
    monkeypatch.setattr(qt, "CompanionWindow", create_window)
    assert qt.QtBackend().run() == 0
    assert application.quit_policy is True
    assert application.quit_calls == 1
    assert len(windows) == 1 and windows[0]._closed
