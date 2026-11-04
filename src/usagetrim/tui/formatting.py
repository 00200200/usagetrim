"""Safe display formatting for metadata from local SQLite databases."""

import math
import time


def format_timestamp(value: object) -> str:
    """Format a local timestamp, or use a placeholder for invalid metadata."""
    try:
        if isinstance(value, bool) or not math.isfinite(value):
            return "—"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))
    except (TypeError, ValueError, OverflowError, OSError):
        return "—"
