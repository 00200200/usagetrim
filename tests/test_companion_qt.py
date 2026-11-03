"""Qt initialization and controls use the offscreen plugin, never a display server."""

import sys
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


@pytest.mark.parametrize("initial_mode", ["panel", "pet"])
def test_surface_attributes_are_configured_before_initial_native_window(
    qt_app, tmp_path, monkeypatch, initial_mode
):
    from PySide6.QtCore import Qt

    from usagetrim.companion import qt
    from usagetrim.companion.controller import CompanionController
    from usagetrim.core.monitor import Monitor

    monkeypatch.setattr(qt, "supports_transparency", lambda: True)
    controller = CompanionController(Monitor([], tmp_path))
    controller.save_window_preferences(initial_mode, None)
    configurations = []

    class RecordingWindow(qt.CompanionWindow):
        def _configure_surface(self, transparent):
            configurations.append((transparent, self.windowHandle() is None))
            super()._configure_surface(transparent)

    window = RecordingWindow(
        controller, executor=ImmediateExecutor(), preference_executor=ImmediateExecutor()
    )
    try:
        transparent = initial_mode == "pet"
        assert configurations == [(transparent, True)]
        assert window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground) is transparent
        assert window.testAttribute(Qt.WidgetAttribute.WA_NoSystemBackground) is transparent
        assert window.autoFillBackground() is (not transparent)
        window.show()
        window.poll()
        assert window.windowHandle() is not None
        assert window.isVisible() and not window._closed
        assert bool(window.windowFlags() & Qt.WindowType.FramelessWindowHint) is transparent
    finally:
        window.close()


def test_shown_panel_pet_switches_recreate_surface_and_keep_shared_state(
    qt_app, tmp_path, mock_system_tray, monkeypatch
):
    from PySide6.QtCore import Qt

    from usagetrim.companion import qt
    from usagetrim.companion.controller import CompanionController
    from usagetrim.core.monitor import Monitor

    monkeypatch.setattr(qt, "supports_transparency", lambda: True)
    service = Monitor([], tmp_path)
    controller = CompanionController(service)
    controller.set_always_on_top(True)
    controller.save_window_preferences("panel", [60, 70], animations=True)
    original_snapshot = service.snapshot
    reads = []

    def snapshot():
        reads.append(True)
        return original_snapshot()

    monkeypatch.setattr(service, "snapshot", snapshot)
    monitor_executor = ImmediateExecutor()
    window = qt.CompanionWindow(
        controller, executor=monitor_executor, preference_executor=ImmediateExecutor()
    )
    resets = []
    flag_attributes = []
    shutdowns = []
    original_destroy = window.destroy
    original_set_flag = window.setWindowFlag

    def destroy(*args, **kwargs):
        resets.append(
            (
                window.windowHandle() is not None,
                window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground),
            )
        )
        original_destroy(*args, **kwargs)

    def set_flag(flag, enabled=True):
        if flag == Qt.WindowType.FramelessWindowHint:
            flag_attributes.append(
                (
                    enabled,
                    window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground),
                    window.testAttribute(Qt.WidgetAttribute.WA_NoSystemBackground),
                    window.autoFillBackground(),
                )
            )
        original_set_flag(flag, enabled)

    monkeypatch.setattr(window, "destroy", destroy)
    monkeypatch.setattr(window, "setWindowFlag", set_flag)
    window.shutdown_requested.connect(lambda: shutdowns.append(True))
    try:
        window.show()
        window.poll()
        assert window._mode == "panel" and window.windowHandle() is not None
        controls, artwork = window.controls, window.artwork
        tray, writer = window.tray, window._preferences
        icon = tray.icon
        pixmap_key = artwork.pixmap().cacheKey()
        for index, mode in enumerate(("pet", "panel", "pet", "panel", "pet"), 1):
            transparent = mode == "pet"
            # These paths also exercise the public tray/menu mode commands.
            window.tray.actions["show_pet" if transparent else "statistics"].trigger()
            assert window._mode == mode and window.isVisible()
            assert window.windowHandle() is not None
            assert len(resets) == index
            assert resets[-1] == (True, not transparent)
            # Attributes are installed before flags can create another surface.
            assert flag_attributes[-1] == (transparent, transparent, transparent, not transparent)
            assert bool(window.windowFlags() & Qt.WindowType.FramelessWindowHint) is transparent
            assert window.autoFillBackground() is (not transparent)
            assert window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
            assert window.controls is controls and controls.objectName() == "pet-controls"
            assert window.artwork is artwork and not artwork._shutdown
            assert artwork.pixmap().cacheKey() == pixmap_key
            assert artwork.motion_timer.isActive() is transparent
            assert window.tray is tray and tray.icon is icon and icon.visible
            assert window.executor is monitor_executor and window._preferences is writer
            assert window.controller is controller and controller._monitor is service
            assert window.refresh_timer.isActive() and window.poll_timer.isActive()
            assert window.refresh_timer.interval() == 5000
            assert shutdowns == []
            assert len(reads) == 1
        window._flush_preferences()
        window.poll()
        assert len(reads) == 1
        assert controller.window_preferences() == {
            "mode": "pet",
            "position": [window.x(), window.y()],
        }
        assert controller.always_on_top() and controller.animations_enabled()
        window.set_mode("pet")
        assert len(resets) == 5  # No surface reset when the format is unchanged.
    finally:
        window.close()
    assert shutdowns == [True]
    assert not icon.visible


def test_shown_pet_fallback_never_enables_translucency_or_resets_surface(
    qt_app, tmp_path, monkeypatch
):
    from PySide6.QtCore import Qt

    from usagetrim.companion import qt
    from usagetrim.companion.controller import CompanionController
    from usagetrim.core.monitor import Monitor

    monkeypatch.setattr(qt, "supports_transparency", lambda: False)
    window = qt.CompanionWindow(
        CompanionController(Monitor([], tmp_path)),
        executor=ImmediateExecutor(),
        preference_executor=ImmediateExecutor(),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("an unchanged opaque format must not need a native surface reset")

    try:
        window.show()
        window.poll()
        monkeypatch.setattr(window, "destroy", forbidden)
        for mode in ("pet", "panel", "pet"):
            window.set_mode(mode)
            assert not window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            assert not window.testAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
            assert not window.windowFlags() & Qt.WindowType.FramelessWindowHint
            assert window.autoFillBackground() and window.isVisible()
            assert window.refresh_timer.isActive()
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


def test_mint_states_follow_real_measurements_without_extra_reads(qt_app, tmp_path, monkeypatch):
    from PySide6.QtCore import QAbstractAnimation

    from usagetrim.companion.controller import CompanionController
    from usagetrim.companion.pet import PetState
    from usagetrim.companion.qt import CompanionWindow
    from usagetrim.core.monitor import Monitor

    service = Monitor([], tmp_path)
    original_snapshot = service.snapshot
    reads = []

    def snapshot():
        reads.append(True)
        return original_snapshot()

    monkeypatch.setattr(service, "snapshot", snapshot)
    window = CompanionWindow(
        CompanionController(service),
        executor=ImmediateExecutor(),
        preference_executor=ImmediateExecutor(),
    )
    try:
        window.show_pet()
        window.poll()
        mint = window.artwork
        assert mint.state is PetState.IDLE  # An empty history is not an error.
        assert mint.motion_timer.isActive()
        service.store.record(100, 20, 100, 20, event_id="mint-activity")
        window.refresh()
        window.poll()
        assert mint.state is PetState.ACTIVE
        assert mint.activity_timer.isActive()
        assert "New local measurements" in mint.toolTip()
        position = window.pos()
        pixmap_key = mint.pixmap().cacheKey()
        read_count = len(reads)
        for _ in range(20):
            mint.motion_timer.timeout.emit()
        assert -3 <= mint.vertical_offset < 0
        assert window.pos() == position
        assert mint.pixmap().cacheKey() == pixmap_key
        assert len(reads) == read_count
        mint.activity_timer.timeout.emit()
        assert mint.state is PetState.IDLE
        window.pause_button.click()
        window.poll()
        assert mint.state is PetState.PAUSED
        assert not mint.motion_timer.isActive()
        assert mint.return_animation.state() == QAbstractAnimation.State.Running
        mint.return_animation.setCurrentTime(mint.return_animation.duration())
        assert mint.vertical_offset == 0
        window.pause_button.click()
        window.poll()
        assert mint.state is PetState.IDLE
        assert mint.motion_timer.isActive()
        # Prepared previews are not new returned-operation measurements.
        service.store.record(10, 2, 10, 2, delivery="prepared", event_id="mint-preview")
        window.refresh()
        window.poll()
        assert mint.state is PetState.IDLE

        def unavailable():
            raise OSError("monitor unavailable")

        monkeypatch.setattr(service, "snapshot", unavailable)
        window.refresh()
        window.poll()
        assert mint.state is PetState.UNAVAILABLE
        assert not mint.motion_timer.isActive()
        assert not window.pause_button.isEnabled()
        monkeypatch.setattr(service, "snapshot", snapshot)
        window.refresh()
        window.poll()
        assert mint.state is PetState.IDLE
        assert mint.motion_timer.isActive()
        assert window.refresh_timer.interval() == 5000
    finally:
        window.close()


def test_mint_activity_baseline_handles_history_reset_and_day_change(qt_app):
    from usagetrim.companion.pet import MintArtwork, PetState

    mint = MintArtwork()

    def snapshot(count, date="2026-11-03", paused=False):
        return {
            "paused": paused,
            "today": {"events": count} if count else None,
            "days": [{"date": date}],
        }

    try:
        mint.update_snapshot(snapshot(50))
        assert mint.state is PetState.IDLE  # Existing history at startup is not activity.
        mint.update_snapshot(snapshot(51))
        assert mint.state is PetState.ACTIVE
        mint.update_snapshot(snapshot(52, date="2026-11-04"))
        assert mint.state is PetState.IDLE
        mint.update_snapshot(snapshot(0, date="2026-11-04"))
        assert mint.state is PetState.IDLE
        mint.update_snapshot(snapshot(1, date="2026-11-04", paused=True))
        assert mint.state is PetState.PAUSED
        mint.update_snapshot(snapshot(1, date="2026-11-04"))
        assert mint.state is PetState.IDLE
    finally:
        mint.close()


def test_mint_motion_visibility_and_quit(qt_app, tmp_path, mock_system_tray):
    from PySide6.QtCore import QAbstractAnimation

    from usagetrim.companion.controller import CompanionController
    from usagetrim.companion.pet import PetState
    from usagetrim.companion.qt import CompanionWindow
    from usagetrim.core.monitor import Monitor

    service = Monitor([], tmp_path)
    window = CompanionWindow(
        CompanionController(service),
        executor=ImmediateExecutor(),
        preference_executor=ImmediateExecutor(),
    )
    try:
        window.show_pet()
        window.poll()
        mint = window.artwork
        assert mint.motion_timer.isActive()
        window.hide_pet()
        assert not mint.motion_timer.isActive()
        assert not mint.activity_timer.isActive()
        assert mint.vertical_offset == 0
        mint.motion_timer.timeout.emit()  # Late delivery must not move hidden content.
        assert mint.vertical_offset == 0
        window.show_pet()
        assert mint.motion_timer.isActive()
        window.show_statistics()
        assert not mint.motion_timer.isActive()
        window.show_pet()
        assert mint.motion_timer.isActive()
        window.showMinimized()
        assert not mint.motion_timer.isActive()
        window.showNormal()
        assert mint.motion_timer.isActive()
        service.store.record(100, 10, 100, 10, event_id="mint-before-quit")
        window.refresh()
        window.poll()
        assert mint.state is PetState.ACTIVE
        window.tray.actions["quit"].trigger()
        assert window._closed
        assert not mint.motion_timer.isActive()
        assert not mint.activity_timer.isActive()
        assert mint.return_animation.state() == QAbstractAnimation.State.Stopped
        mint.resume_motion()
        mint.motion_timer.timeout.emit()
        mint.activity_timer.timeout.emit()
        assert not mint.motion_timer.isActive()
        assert mint.vertical_offset == 0
    finally:
        window.close()


def test_animation_preference_persists_and_tray_stays_in_sync(qt_app, tmp_path, mock_system_tray):
    from PySide6.QtCore import QAbstractAnimation

    from usagetrim.companion.controller import CompanionController
    from usagetrim.companion.qt import CompanionWindow
    from usagetrim.core.monitor import Monitor

    controller = CompanionController(Monitor([], tmp_path / "first"))
    window = CompanionWindow(
        controller, executor=ImmediateExecutor(), preference_executor=ImmediateExecutor()
    )
    try:
        window.show_pet()
        window.poll()
        assert window.animations.text() == "Mint animations: On"
        assert window.artwork.motion_timer.isActive()
        window.animations.setChecked(False)
        assert window.animations.text() == "Mint animations: Off"
        assert not window.artwork.motion_timer.isActive()
        assert window.artwork.return_animation.state() == QAbstractAnimation.State.Stopped
        assert window.artwork.vertical_offset == 0
        assert not window.tray.actions["animations"].isChecked()
    finally:
        window.close()  # Flush the preference without delivering a debounce timer.
    assert CompanionController().animations_enabled() is False
    restored = CompanionWindow(
        CompanionController(Monitor([], tmp_path / "second")),
        executor=ImmediateExecutor(),
        preference_executor=ImmediateExecutor(),
    )
    try:
        restored.show_pet()
        restored.poll()
        assert not restored.animations.isChecked()
        assert restored.animations.text() == "Mint animations: Off"
        assert not restored.artwork.motion_timer.isActive()
        restored.tray.actions["animations"].trigger()
        assert restored.animations.isChecked()
        assert restored.animations.text() == "Mint animations: On"
        assert restored.artwork.motion_timer.isActive()
        restored._flush_preferences()
        restored.poll()
        assert CompanionController().animations_enabled() is True
    finally:
        restored.close()


def test_animation_label_describes_preference_while_optimization_paused(
    qt_app, tmp_path, mock_system_tray
):
    from usagetrim.companion.controller import CompanionController
    from usagetrim.companion.pet import PetState
    from usagetrim.companion.qt import CompanionWindow
    from usagetrim.core.monitor import Monitor

    controller = CompanionController(Monitor([], tmp_path))
    controller.set_paused(True)
    window = CompanionWindow(
        controller, executor=ImmediateExecutor(), preference_executor=ImmediateExecutor()
    )
    try:
        window.show_pet()
        window.poll()
        assert window.artwork.state is PetState.PAUSED
        assert not window.artwork.motion_timer.isActive()
        assert window.animations.isChecked()
        assert window.animations.text() == "Mint animations: On"
        assert window.tray.actions["animations"].isChecked()
        window.animations.setChecked(False)
        assert window.animations.text() == "Mint animations: Off"
        window.tray.actions["animations"].trigger()
        assert window.animations.text() == "Mint animations: On"
        assert window.artwork.state is PetState.PAUSED
        assert not window.artwork.motion_timer.isActive()
        window._flush_preferences()
        window.poll()
        assert controller.animations_enabled() is True
    finally:
        window.close()


def test_tray_information_uses_real_snapshots_and_updates_while_hidden(
    qt_app, tmp_path, mock_system_tray, monkeypatch
):
    from PySide6.QtCore import QTimer

    from usagetrim.companion.controller import CompanionController
    from usagetrim.companion.qt import CompanionWindow
    from usagetrim.core.monitor import Monitor

    service = Monitor([], tmp_path)
    service.store.record(2000, 460, 2000, 460, event_id="tray-summary-first")
    original_snapshot = service.snapshot
    reads = []

    def snapshot():
        reads.append(True)
        return original_snapshot()

    monkeypatch.setattr(service, "snapshot", snapshot)
    window = CompanionWindow(
        CompanionController(service),
        executor=ImmediateExecutor(),
        preference_executor=ImmediateExecutor(),
    )
    try:
        info = window.tray.information
        assert info["status"].text() == "Status: Unavailable"
        assert info["operations"].text() == "Today: Unavailable"
        assert info["net"].text() == "Net reduction: Unavailable"
        assert window.tray.menu.actions()[:4] == list(info.values())
        assert window.tray.menu.actions()[4].isSeparator()
        assert all(not item.isEnabled() and not item.isCheckable() for item in info.values())
        assert set(window.tray.actions) == {
            "show_pet",
            "hide_pet",
            "statistics",
            "controls",
            "pause",
            "top",
            "animations",
            "quit",
        }
        timer_count = len(window.findChildren(QTimer))
        window.poll()
        assert info["title"].text() == "UsageTrim Mint"
        assert info["status"].text() == "Status: Active"
        assert info["operations"].text() == "Today: 1 operation"
        assert info["net"].text() == "Net reduction: +1,540 tokens (estimate)"
        assert len(reads) == 1
        window.show_pet()
        window.hide_pet()
        window.tray.menu.aboutToShow.emit()
        window.tray.sync()
        assert len(reads) == 1
        assert not window.isVisible()
        service.store.record(100, 50, 100, 50, event_id="tray-summary-second")
        window.refresh_timer.timeout.emit()
        window.poll()
        assert not window.isVisible()
        assert info["operations"].text() == "Today: 2 operations"
        assert info["net"].text() == "Net reduction: +1,590 tokens (estimate)"
        assert len(reads) == 2
        window.tray.menu.aboutToShow.emit()
        window.tray.sync()
        assert len(reads) == 2
        assert window.refresh_timer.interval() == 5000
        assert len(window.findChildren(QTimer)) == timer_count
        assert window.tray.actions["show_pet"].isEnabled()
        assert window.tray.actions["pause"].isEnabled()
    finally:
        window.close()


def test_tray_information_no_measurements_pause_error_and_recovery(
    qt_app, tmp_path, mock_system_tray, monkeypatch
):
    from usagetrim.companion.controller import CompanionController
    from usagetrim.companion.qt import CompanionWindow
    from usagetrim.core.monitor import Monitor

    service = Monitor([], tmp_path)
    original_snapshot = service.snapshot
    window = CompanionWindow(
        CompanionController(service),
        executor=ImmediateExecutor(),
        preference_executor=ImmediateExecutor(),
    )
    try:
        window.poll()
        info = window.tray.information
        assert info["status"].text() == "Status: Active"
        assert info["operations"].text() == "Today: No measurements"
        assert info["net"].text() == "Net reduction: No measurements"
        window.tray.actions["pause"].trigger()
        window.poll()
        assert info["status"].text() == "Status: Paused"
        assert window.tray.actions["pause"].text() == "Resume Optimization"
        assert info["operations"].text() == "Today: No measurements"

        def unavailable():
            raise OSError("monitor unavailable")

        monkeypatch.setattr(service, "snapshot", unavailable)
        window.refresh()
        window.poll()
        assert info["status"].text() == "Status: Unavailable"
        assert info["operations"].text() == "Today: Unavailable"
        assert info["net"].text() == "Net reduction: Unavailable"
        assert not window.tray.actions["pause"].isEnabled()
        monkeypatch.setattr(service, "snapshot", original_snapshot)
        window.refresh()
        window.poll()
        assert info["status"].text() == "Status: Paused"
        window.tray.actions["pause"].trigger()
        window.poll()
        assert info["status"].text() == "Status: Active"
        assert window.tray.actions["pause"].text() == "Pause Optimization"
    finally:
        window.close()


@pytest.mark.parametrize("compact,net", [(150, "-150"), (0, "+0")])
def test_tray_information_preserves_net_expansion_and_actual_zero(
    qt_app, tmp_path, mock_system_tray, monkeypatch, compact, net
):
    from usagetrim.companion.controller import CompanionController
    from usagetrim.companion.qt import CompanionWindow
    from usagetrim.core.monitor import Monitor

    service = Monitor([], tmp_path)
    service.store.record(0, compact, 0, compact, operation="retrieve", event_id="tray-net")
    window = CompanionWindow(
        CompanionController(service),
        executor=ImmediateExecutor(),
        preference_executor=ImmediateExecutor(),
    )
    try:
        window.poll()
        info = window.tray.information
        assert info["operations"].text() == "Today: 1 operation"
        assert info["net"].text() == f"Net reduction: {net} tokens (estimate)"
        assert "saving" not in info["net"].text().lower()

        # An error must clear previous figures, not retain stale signed numbers.
        def unavailable():
            raise OSError("monitor unavailable")

        monkeypatch.setattr(service, "snapshot", unavailable)
        window.refresh()
        window.poll()
        assert info["net"].text() == "Net reduction: Unavailable"
        assert info["operations"].text() == "Today: Unavailable"
    finally:
        window.close()


@pytest.mark.parametrize(
    "plugin,expected",
    [
        ("windows", True),
        ("xcb", True),
        ("wayland", False),
        ("wayland-egl", False),
        ("minimal", False),
        ("offscreen", True),
        ("unknown", False),
    ],
)
def test_positioning_capabilities_follow_qt_plugin(qt_app, monkeypatch, plugin, expected):
    from types import SimpleNamespace

    from usagetrim.companion import pet

    monkeypatch.setattr(pet, "QGuiApplication", SimpleNamespace(platformName=lambda: plugin))
    assert pet.supports_positioning() is expected


@pytest.mark.parametrize("status,enabled,expected", [(0, 1, True), (0, 0, False), (-1, 1, False)])
def test_windows_dwm_results_are_checked_without_changing_host_platform(
    qt_app, monkeypatch, status, enabled, expected
):
    import ctypes
    from types import SimpleNamespace

    from PySide6.QtGui import QGuiApplication

    from usagetrim.companion import pet

    host_platform = sys.platform
    calls = []

    def query(pointer):
        calls.append(True)
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int))[0] = enabled
        return status

    # Replace module-local adapters only; never mutate sys.platform or Qt's QPA plugin.
    monkeypatch.setattr(pet, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(pet, "QGuiApplication", SimpleNamespace(platformName=lambda: "windows"))
    monkeypatch.setattr(
        pet,
        "ctypes",
        SimpleNamespace(
            c_int=ctypes.c_int,
            byref=ctypes.byref,
            windll=SimpleNamespace(dwmapi=SimpleNamespace(DwmIsCompositionEnabled=query)),
        ),
    )
    assert pet.supports_transparency() is expected
    assert calls == [True]
    assert sys.platform == host_platform
    assert QGuiApplication.platformName() == "offscreen"


@pytest.mark.parametrize("error", [OSError, AttributeError])
def test_windows_dwm_unavailable_falls_back_safely(qt_app, monkeypatch, error):
    import ctypes
    from types import SimpleNamespace

    from usagetrim.companion import pet

    def query(pointer):
        raise error("DWM unavailable")

    monkeypatch.setattr(pet, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(pet, "QGuiApplication", SimpleNamespace(platformName=lambda: "windows"))
    monkeypatch.setattr(
        pet,
        "ctypes",
        SimpleNamespace(
            c_int=ctypes.c_int,
            byref=ctypes.byref,
            windll=SimpleNamespace(dwmapi=SimpleNamespace(DwmIsCompositionEnabled=query)),
        ),
    )
    assert pet.supports_transparency() is False


@pytest.mark.parametrize(
    "library,display,atom,owner,expected",
    [
        (None, 123, 7, 42, False),
        ("X11", None, 7, 42, False),
        ("X11", 123, 0, 42, False),
        ("X11", 123, 7, 0, False),
        ("X11", 123, 7, 42, True),
    ],
)
def test_x11_transparency_requires_compositor_ownership(
    qt_app, monkeypatch, library, display, atom, owner, expected
):
    import ctypes
    from types import SimpleNamespace

    from usagetrim.companion import pet

    class NativeCall:
        def __init__(self, result):
            self.result = result
            self.calls = []

        def __call__(self, *args):
            self.calls.append(args)
            return self.result

    x11 = SimpleNamespace(
        XOpenDisplay=NativeCall(display),
        XDefaultScreen=NativeCall(0),
        XInternAtom=NativeCall(atom),
        XGetSelectionOwner=NativeCall(owner),
        XCloseDisplay=NativeCall(0),
    )
    monkeypatch.setattr(pet, "QGuiApplication", SimpleNamespace(platformName=lambda: "xcb"))
    monkeypatch.setattr(
        pet,
        "ctypes",
        SimpleNamespace(
            util=SimpleNamespace(find_library=lambda name: library),
            CDLL=lambda name: x11,
            c_char_p=ctypes.c_char_p,
            c_void_p=ctypes.c_void_p,
            c_int=ctypes.c_int,
            c_ulong=ctypes.c_ulong,
        ),
    )
    assert pet.supports_transparency() is expected
    if library and display:
        assert x11.XInternAtom.calls == [(display, b"_NET_WM_CM_S0", True)]
        assert x11.XCloseDisplay.calls == [(display,)]
    else:
        assert x11.XGetSelectionOwner.calls == []
        assert x11.XCloseDisplay.calls == []


@pytest.mark.parametrize("position", [[99999, 99999], [-99999, -99999], [-400, -200]])
def test_restored_pet_position_is_clamped_without_more_monitor_reads(
    qt_app, tmp_path, monkeypatch, position
):
    from types import SimpleNamespace

    from PySide6.QtCore import QPoint, QRect

    from usagetrim.companion import qt
    from usagetrim.companion.controller import CompanionController
    from usagetrim.core.monitor import Monitor

    area = QRect(-800, -300, 1600, 1200)
    screen = SimpleNamespace(availableGeometry=lambda: area)
    monkeypatch.setattr(
        qt,
        "QGuiApplication",
        SimpleNamespace(screenAt=lambda point: None, primaryScreen=lambda: screen),
    )
    monkeypatch.setattr(qt, "supports_positioning", lambda: True)
    monkeypatch.setattr(qt, "supports_transparency", lambda: False)
    service = Monitor([], tmp_path)
    original_snapshot = service.snapshot
    reads = []

    def snapshot():
        reads.append(True)
        return original_snapshot()

    monkeypatch.setattr(service, "snapshot", snapshot)
    controller = CompanionController(service)
    controller.save_window_preferences("pet", position)
    window = qt.CompanionWindow(
        controller, executor=ImmediateExecutor(), preference_executor=ImmediateExecutor()
    )
    try:
        expected = QPoint(
            max(area.left(), min(position[0], area.right() - window.width() + 1)),
            max(area.top(), min(position[1], area.bottom() - window.height() + 1)),
        )
        assert window.pos() == expected
        window.poll()
        assert len(reads) == 1
        window.restore_pet_position(position)
        assert window.pos() == expected
        assert len(reads) == 1
    finally:
        window.close()


def test_wayland_rejects_manual_drag_without_probing_global_window_apis(
    qt_app, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    from usagetrim.companion import pet, qt
    from usagetrim.companion.controller import CompanionController
    from usagetrim.core.companion_state import settings, update_settings
    from usagetrim.core.monitor import Monitor

    def forbidden(*args, **kwargs):
        pytest.fail("Wayland must not use global positioning or native compositor probes")

    update_settings(companion_qt_mode="pet", companion_qt_pet_position=[123, 234])
    monkeypatch.setattr(pet, "QGuiApplication", SimpleNamespace(platformName=lambda: "wayland"))
    monkeypatch.setattr(
        qt, "QGuiApplication", SimpleNamespace(screenAt=forbidden, primaryScreen=forbidden)
    )
    monkeypatch.setattr(pet.ctypes, "CDLL", forbidden)
    window = qt.CompanionWindow(
        CompanionController(Monitor([], tmp_path)),
        executor=ImmediateExecutor(),
        preference_executor=ImmediateExecutor(),
    )
    try:
        monkeypatch.setattr(window, "move", forbidden)
        monkeypatch.setattr(window.artwork, "_start_system_move", lambda: False)
        window.restore_pet_position([456, 789])
        press = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(10, 10),
            QPointF(30, 30),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        move = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(20, 20),
            QPointF(40, 40),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        window.artwork.mousePressEvent(press)
        window.artwork.mouseMoveEvent(move)
        window.save_pet_position()
        window._flush_preferences()
        assert settings()["companion_qt_pet_position"] == [123, 234]
        assert not window.windowFlags() & Qt.WindowType.FramelessWindowHint
        assert not window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert window.autoFillBackground()
    finally:
        window.close()


# This test is collected only on Windows. All common offscreen tests run on both OSes.
if sys.platform == "win32":

    def test_native_windows_dwm_probe_matches_system_result(qt_app, monkeypatch):
        import ctypes
        from types import SimpleNamespace

        from PySide6.QtGui import QGuiApplication

        from usagetrim.companion import pet

        enabled = ctypes.c_int()
        status = ctypes.windll.dwmapi.DwmIsCompositionEnabled(ctypes.byref(enabled))
        monkeypatch.setattr(pet, "QGuiApplication", SimpleNamespace(platformName=lambda: "windows"))
        assert pet.supports_transparency() is (status == 0 and bool(enabled.value))
        assert QGuiApplication.platformName() == "offscreen"
