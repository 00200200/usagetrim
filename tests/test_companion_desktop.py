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
