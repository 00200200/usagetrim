"""Timestamp formatting tolerates missing and corrupt SQLite metadata."""

import time

import pytest

from usagetrim.tui import formatting


@pytest.mark.parametrize("value", [None, "invalid", 1e300, float("nan"), float("inf"), True])
def test_format_timestamp_invalid_values(value):
    assert formatting.format_timestamp(value) == "—"


@pytest.mark.parametrize("value", [0, 1_700_000_000, 1_700_000_000.5])
def test_format_timestamp_preserves_local_display(value):
    assert formatting.format_timestamp(value) == time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(value)
    )


@pytest.mark.parametrize("error", [TypeError, ValueError, OverflowError, OSError])
def test_format_timestamp_handles_platform_errors(monkeypatch, error):
    def unavailable(value):
        raise error("invalid platform timestamp")

    monkeypatch.setattr(formatting.time, "localtime", unavailable)
    assert formatting.format_timestamp(0) == "—"
