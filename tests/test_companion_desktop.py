"""Desktop routing and controller tests without Qt or a display server."""

import builtins
import sys
from types import ModuleType

import pytest
from typer.testing import CliRunner

from usagetrim.cli import app
from usagetrim.companion import CompanionUnavailable, create_backend, select_backend
from usagetrim.companion.controller import CompanionController
from usagetrim.core.companion_state import settings, update_settings
from usagetrim.core.monitor import Monitor


@pytest.mark.parametrize("system", ["Linux", "Windows"])
def test_desktop_backend_selection(system):
    assert select_backend(system) == "pyside6"


@pytest.mark.parametrize("system", ["Darwin", "FreeBSD"])
def test_desktop_unsupported_platform_is_controlled(system):
    with pytest.raises(CompanionUnavailable):
        select_backend(system)


def test_default_platform_selection_and_lazy_factory(monkeypatch):
    monkeypatch.setattr("usagetrim.companion.platform.system", lambda: "Windows")
    backend = object()
    module = ModuleType("usagetrim.companion.qt")
    module.QtBackend = lambda: backend
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert create_backend() is backend


def test_missing_optional_dependency_has_installation_hint(monkeypatch):
    monkeypatch.setattr("usagetrim.companion.platform.system", lambda: "Linux")
    monkeypatch.delitem(sys.modules, "usagetrim.companion.qt", raising=False)
    original = builtins.__import__

    def without_qt(name, *args, **kwargs):
        if name == "PySide6" or name.startswith("PySide6."):
            raise ModuleNotFoundError("PySide6 unavailable", name="PySide6")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_qt)
    with pytest.raises(CompanionUnavailable, match=r"usagetrim\[companion\]"):
        create_backend()


def test_controller_reads_real_telemetry_and_shared_pause(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGETRIM_STATE_DIR", str(tmp_path / "state"))
    service = Monitor([], tmp_path / "collector")

    def forbidden(*args, **kwargs):
        pytest.fail("desktop snapshots must not request account/provider data")

    monkeypatch.setattr(service.usage, "snapshot", forbidden)
    service.store.record(100, 25, 100, 25, event_id="desktop-real")
    service.store.record(100, 1, 100, 1, delivery="prepared", event_id="desktop-preview")
    controller = CompanionController(service)
    snapshot = controller.snapshot()
    assert snapshot["today"] == {
        "before": 100,
        "after": 25,
        "net": 75,
        "recovery": 0,
        "events": 1,
    }
    assert controller.set_paused(True)["paused"] is True
    assert settings()["paused"] is True
    assert controller.set_paused(False)["paused"] is False


def test_controller_preferences_preserve_existing_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGETRIM_STATE_DIR", str(tmp_path))
    update_settings(custom="preserved", paused=True)
    controller = CompanionController()
    assert controller.always_on_top() is False
    controller.set_always_on_top(True)
    assert CompanionController().always_on_top() is True
    assert settings()["custom"] == "preserved"
    assert settings()["paused"] is True
    with pytest.raises(ValueError):
        controller.set_always_on_top("yes")
    with pytest.raises(ValueError):
        controller.set_paused(1)


def test_controller_empty_history_has_no_invented_counters(tmp_path):
    controller = CompanionController(Monitor([], tmp_path))
    assert controller.snapshot()["today"] is None


def test_pet_preferences_roundtrip_and_validation(tmp_path):
    update_settings(paused=True, custom="preserved")
    controller = CompanionController(Monitor([], tmp_path))
    assert controller.window_preferences() == {"mode": "panel", "position": None}
    controller.save_window_preferences("pet", [100, 200])
    assert CompanionController().window_preferences() == {"mode": "pet", "position": [100, 200]}
    controller.save_window_preferences("panel", None)
    assert settings()["companion_qt_pet_position"] == [100, 200]
    assert settings()["paused"] is True
    assert settings()["custom"] == "preserved"
    with pytest.raises(ValueError):
        controller.save_window_preferences("invalid", None)
    with pytest.raises(ValueError):
        controller.save_window_preferences("pet", [True, 3])
    update_settings(companion_qt_pet_position=["bad", 2])
    assert controller.window_preferences()["position"] is None


def test_window_preference_save_never_reads_monitor(monkeypatch):
    controller = CompanionController()

    def forbidden():
        pytest.fail("saving preferences must not initialize or read the monitor")

    monkeypatch.setattr(controller, "snapshot", forbidden)
    assert controller.save_window_preferences("pet", [42, 73]) == {
        "mode": "pet",
        "position": [42, 73],
    }
    assert controller._monitor is None


def test_preferences_coalesce_and_drain_final_write_without_monitor(monkeypatch):
    from concurrent.futures import Future

    from usagetrim.companion import preferences

    class ControlledExecutor:
        def __init__(self):
            self.jobs = []
            self.shutdown_calls = []

        def submit(self, function, *args):
            future = Future()
            self.jobs.append((future, function, args))
            return future

        def shutdown(self, *, wait, cancel_futures):
            self.shutdown_calls.append((wait, cancel_futures))

    executor = ControlledExecutor()
    monkeypatch.setattr(preferences, "ThreadPoolExecutor", lambda **kwargs: executor)
    controller = CompanionController()

    def forbidden():
        pytest.fail("settings writer must never read telemetry")

    monkeypatch.setattr(controller, "snapshot", forbidden)
    writer = preferences.PreferencesWriter(controller)
    writer.queue("pet", [1, 2])
    writer.flush()
    for value in range(100):
        writer.queue("pet", [value, value + 1])
        writer.flush()
    assert len(executor.jobs) == 1
    writer.close()
    assert len(executor.jobs) == 2
    assert executor.shutdown_calls == [(False, False)]
    writer.close()
    assert len(executor.jobs) == 2
    # Deterministically execute the serialized writes, without threads or waits.
    for future, function, args in executor.jobs:
        future.set_result(function(*args))
    assert CompanionController().window_preferences() == {"mode": "pet", "position": [99, 100]}


def test_companion_cli_launch_and_exit_code(monkeypatch):
    from usagetrim import companion

    class Backend:
        def run(self):
            return 7

    monkeypatch.setattr(companion, "create_backend", Backend)
    result = CliRunner().invoke(app, ["companion"])
    assert result.exit_code == 7


def test_companion_cli_missing_dependency_is_readable(monkeypatch):
    from usagetrim import companion

    def unavailable():
        raise CompanionUnavailable("Install with: pip install 'usagetrim[companion]'")

    monkeypatch.setattr(companion, "create_backend", unavailable)
    result = CliRunner().invoke(app, ["companion"])
    assert result.exit_code == 2
    assert "usagetrim[companion]" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("arguments", [["--help"], ["companion", "--help"], ["monitor", "--help"]])
def test_cli_help_does_not_load_qt(monkeypatch, arguments):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith("PySide6"):
            pytest.fail("ordinary CLI startup must not load Qt")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    assert CliRunner().invoke(app, arguments).exit_code == 0
