"""Wait for threaded I/O and its UI delivery without timing assumptions."""

import asyncio


def _io_state(app):
    """Describe pending UI delivery without waiting for any message pump."""
    lanes = [app._refresh_lane]
    try:
        screen = app.screen
    except Exception:
        # Diagnostics must not hide the timeout if teardown removed the screen.
        screen = None
    if hasattr(screen, "_page_lane"):
        lanes.extend(
            [screen._page_lane, screen._detail_lane, screen._export_lane, screen._clipboard_lane]
        )
    return f"screen={getattr(screen, 'id', None)!r}; " + "; ".join(
        f"{lane.name}: busy={lane.busy}, closed={lane.closed}, "
        f"revision={lane.revision}, running={lane._running_revision}, "
        f"pending={lane._pending is not None}"
        for lane in lanes
    )


async def press(pilot, *keys):
    """Bound key handling too, before a test reaches settle()."""
    try:
        async with asyncio.timeout(5):
            await pilot.press(*keys)
    except TimeoutError as exc:
        raise AssertionError(
            f"TUI key handling timed out for {keys!r}: {_io_state(pilot.app)}"
        ) from exc


async def settle(pilot):
    app = pilot.app
    phase = "collecting lanes"
    try:
        async with asyncio.timeout(5):
            while app.is_running:
                lanes = [app._refresh_lane]
                screen = app.screen
                if hasattr(screen, "_page_lane"):
                    lanes.extend(
                        [
                            screen._page_lane,
                            screen._detail_lane,
                            screen._export_lane,
                            screen._clipboard_lane,
                        ]
                    )
                lanes = [lane for lane in lanes if not lane.closed]
                for lane in lanes:
                    phase = f"waiting for {lane.name}"
                    await lane.wait_idle()
                phase = "draining UI messages"
                await pilot.pause()
                if not any(lane.busy for lane in lanes) and app.screen is screen:
                    return
    except TimeoutError as exc:
        raise AssertionError(f"TUI did not settle ({phase}): {_io_state(app)}") from exc
