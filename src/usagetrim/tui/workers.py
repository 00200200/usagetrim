"""Serialized Textual thread workers with coalesced, generation-checked results."""

import asyncio

from textual.message import Message


class IoResult(Message):
    """Deliver a completed synchronous operation to its owner's UI thread."""

    def __init__(self, lane, revision, value, error):
        super().__init__()
        self.lane = lane
        self.revision = revision
        self.value = value
        self.error = error


class IOLane:
    """Run one physical operation at a time, retaining only the latest request.

    Worker cancellation does not mean a synchronous call has stopped. Only its
    completion message releases this lane; cancellation never starts a successor.
    All methods except the worker closure run on the UI thread.
    """

    def __init__(self, owner, name):
        self.owner = owner
        self.name = name
        self.busy = False
        self.closed = False
        self.revision = 0
        self._running_revision = 0
        self._key = None
        self._pending = None
        self._apply = None
        self._idle = asyncio.Event()
        self._idle.set()

    def submit(self, key, operation, apply, *, force=False):
        if self.closed:
            return
        if not force:
            if self._pending is not None and self._pending[1] == key:
                return
            if (
                self.busy
                and self._pending is None
                and self._key == key
                and self._running_revision == self.revision
            ):
                return
        self.revision += 1
        self._pending = (self.revision, key, operation, apply)
        self._idle.clear()
        if not self.busy:
            self._start()

    def _start(self):
        revision, key, operation, self._apply = self._pending
        self._pending = None
        self._key = key
        self._running_revision = revision
        self.busy = True

        def run():
            try:
                value, error = operation(), None
            except Exception as exc:
                # Report errors as data, including after Worker.cancel(). No
                # widget access or call_from_thread wait is needed here.
                value, error = None, exc
            if not self.closed:
                try:
                    self.owner.post_message(IoResult(self, revision, value, error))
                except RuntimeError:
                    # The loop can close between the liveness check and posting.
                    if not self.closed:
                        raise

        self.owner.run_worker(
            run, name=self.name, group=self.name, thread=True, exit_on_error=False
        )

    def finish(self, message):
        if message.revision != self._running_revision:
            return
        self.busy = False
        apply, self._apply = self._apply, None
        try:
            if not self.closed and message.revision == self.revision:
                apply(message.value, message.error)
        finally:
            if not self.closed and self._pending is not None and not self.busy:
                self._start()
            if not self.busy and self._pending is None:
                self._idle.set()

    def invalidate(self):
        self.revision += 1
        self._pending = None
        if not self.busy:
            self._idle.set()

    def close(self):
        self.closed = True
        self.invalidate()

    async def wait_idle(self):
        """Wait for operation completion and UI delivery, useful in headless tests."""
        await self._idle.wait()
