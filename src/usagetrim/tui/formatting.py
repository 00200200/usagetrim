"""Safe display formatting for metadata from local SQLite databases."""

import math
import time

from usagetrim.core.cleaner import strip_ansi
from usagetrim.core.redactor import redact_secrets

_CONTROL_ESCAPES = {
    value: f"\\x{value:02x}" for value in (*range(32), *range(127, 160)) if value not in (9, 10)
}


def redact_terminal_text(text: str) -> str:
    """Remove ANSI before redaction so control sequences cannot split credentials.

    Keep ordinary line endings intact for complete cache exports.
    """
    return redact_secrets(strip_ansi(text))


def display_text(text: str) -> str:
    """Make untrusted text safe for plain Rich/Textual rendering.

    Disabling markup does not disable terminal escapes. Any remaining C0/C1
    controls must be visible text; only tabs and newlines retain their layout.
    """
    return redact_terminal_text(text).translate(_CONTROL_ESCAPES)


def format_timestamp(value: object) -> str:
    """Format a local timestamp, or use a placeholder for invalid metadata."""
    try:
        if isinstance(value, bool) or not math.isfinite(value):
            return "—"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))
    except (TypeError, ValueError, OverflowError, OSError):
        return "—"
