"""Optional desktop companion; importing this package does not load Qt."""

from __future__ import annotations

import platform
from typing import Protocol


class CompanionBackend(Protocol):
    """A desktop frontend owns its event loop and returns a process exit status."""

    def run(self) -> int: ...


class CompanionUnavailable(RuntimeError):
    """A platform or optional GUI dependency is unavailable."""


def select_backend(system: str | None = None) -> str:
    system = platform.system() if system is None else system
    if system in {"Linux", "Windows"}:
        return "pyside6"
    if system == "Darwin":
        raise CompanionUnavailable("Use the existing macOS UsageTrim companion application.")
    raise CompanionUnavailable(f"Desktop Companion is not supported on {system}.")


def create_backend() -> CompanionBackend:
    select_backend()
    try:
        from usagetrim.companion.qt import QtBackend
    except ModuleNotFoundError as exc:
        if exc.name == "PySide6" or (exc.name or "").startswith("PySide6."):
            raise CompanionUnavailable(
                "Install Desktop Companion with: pip install 'usagetrim[companion]'"
            ) from None
        raise CompanionUnavailable(
            "The PySide6 runtime is incomplete. Reinstall 'usagetrim[companion]' "
            "and check the native Qt libraries."
        ) from None
    except ImportError:
        raise CompanionUnavailable(
            "Cannot load the PySide6 runtime. Check the native Qt libraries."
        ) from None
    return QtBackend()
