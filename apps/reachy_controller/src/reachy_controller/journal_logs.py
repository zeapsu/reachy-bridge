"""Follow the daemon service's journal into the log broker.

systemd owns the daemon, so its output lives in the user journal rather than
in a pipe the controller holds. One ``journalctl --follow`` child feeds the
``daemon`` log source for as long as the controller runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil

from reachy_controller.log_stream import LogBroker

logger = logging.getLogger(__name__)

SOURCE = "daemon"
MAX_LINE_BYTES = 64 * 1024


class JournalFollower:
    """Publish one systemd user unit's journal lines to a broker source."""

    def __init__(self, broker: LogBroker, unit: str | None = None) -> None:
        self.broker = broker
        self.unit = unit or os.getenv("REACHY_DAEMON_UNIT", "reachy-bridge.service")
        self._task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._closing = False
        broker.bind_source(SOURCE, status="empty")

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="journal-follower")

    async def _run(self) -> None:
        journalctl = shutil.which("journalctl")
        if journalctl is None:
            self.broker.set_status(SOURCE, "unavailable")
            return
        try:
            self._process = await asyncio.create_subprocess_exec(
                # --user-unit reads the system journal, where user services log
                # when the user journal isn't persistent (the Jetson default).
                journalctl, f"--user-unit={self.unit}", "-f", "-n", "200", "-o", "cat",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_LINE_BYTES,
            )
        except OSError as exc:
            logger.warning("Could not follow %s journal: %s", self.unit, exc)
            self.broker.set_status(SOURCE, "unavailable")
            return
        self.broker.set_status(SOURCE, "live")
        assert self._process.stdout is not None and self._process.stderr is not None
        status = "stopped"
        try:
            # journalctl reports problems such as missing journal permissions
            # on stderr while it keeps following, so show those lines too.
            await asyncio.gather(
                self._pump(self._process.stdout, ""),
                self._pump(self._process.stderr, "journalctl: "),
            )
            # A non-zero exit is a failure unless aclose() terminated it.
            if await self._process.wait() != 0 and not self._closing:
                status = "unavailable"
        finally:
            self.broker.set_status(SOURCE, status)

    async def _pump(self, stream: asyncio.StreamReader, prefix: str) -> None:
        skipping = False
        while True:
            try:
                raw = await stream.readuntil(b"\n")
            except asyncio.LimitOverrunError as exc:
                # Line longer than the stream limit: drop all of it, including
                # the part still to arrive, and keep following.
                await stream.readexactly(exc.consumed)
                if not skipping:
                    notice = f"[line over {MAX_LINE_BYTES} bytes dropped]"
                    self.broker.publish(SOURCE, prefix + notice)
                skipping = True
                continue
            except asyncio.IncompleteReadError as exc:
                if exc.partial and not skipping:
                    self.broker.publish(SOURCE, prefix + exc.partial.decode("utf-8", "replace"))
                return
            if skipping:
                skipping = False
                continue
            self.broker.publish(SOURCE, prefix + raw.decode("utf-8", "replace").rstrip("\r\n"))

    async def aclose(self) -> None:
        self._closing = True
        if self._process is not None and self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._process.wait(), timeout=2.0)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
