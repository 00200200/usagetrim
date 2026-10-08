"""REST API and curl HAR response compactor."""

from __future__ import annotations

from usagetrim.core.json_slimmer import filter_har_log, filter_rest_response

__all__ = [
    "filter_rest_response",
    "filter_har_log",
]
