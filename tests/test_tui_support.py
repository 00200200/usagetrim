"""Synchronization failures must report state without waiting for real timeouts."""

import asyncio
from types import SimpleNamespace

import pytest

from tui_support import press, settle


def make_app(wait_idle):
    lane = SimpleNamespace(
        name="telemetry-refresh",
        busy=True,
        closed=False,
        revision=2,
        _running_revision=1,
        _pending=(2,),
        wait_idle=wait_idle,
    )
    return SimpleNamespace(is_running=True, _refresh_lane=lane, screen=SimpleNamespace(id="test"))


def test_settle_timeout_reports_lane_and_waiting_phase():
    async def unavailable():
        raise TimeoutError("simulated deadline")

    async def scenario():
        pilot = SimpleNamespace(app=make_app(unavailable))
        with pytest.raises(AssertionError, match="waiting for telemetry-refresh") as error:
            await settle(pilot)
        message = str(error.value)
        assert "busy=True" in message
        assert "revision=2, running=1, pending=True" in message

    asyncio.run(scenario())


def test_press_timeout_reports_key_and_lane_state():
    async def unavailable(*keys):
        raise TimeoutError("simulated deadline")

    async def scenario():
        pilot = SimpleNamespace(app=make_app(unavailable), press=unavailable)
        with pytest.raises(AssertionError, match="key handling timed out") as error:
            await press(pilot, "escape")
        assert "escape" in str(error.value)
        assert "telemetry-refresh" in str(error.value)

    asyncio.run(scenario())
