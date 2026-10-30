"""Make the head wobble follow everything sent to the speaker.

Upstream's wobble only sees audio that passes through the daemon's media
server. Apps open the speaker device themselves and Talk goes through the
audio gateway, so neither moved the head. With the speaker tee in place, this
reads ``reachy_bridge_wobble`` (see ``audio_tee``) into the daemon's wobbler
while wobbling is on. Upstream's own feed is dropped then, because that audio
reaches the tee too and would count twice. Without the tee, upstream's
behaviour stays.

An app using the SDK's wobble (the Conversation app does) sends its own
offsets to the daemon. The last writer wins, so two sources would jitter: the
tap holds off while an app's offsets are arriving.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from reachy_bridge_runtime import audio_tee, upstream

logger = logging.getLogger(__name__)

FEATURE = "speaker_wobble"

_ZERO = (0.0,) * 6


class Gate:
    """Decides which of the tap's offsets reach the backend."""

    APP_HOLD_S = 1.0

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._app_at: float | None = None
        self._at_zero = False
        self._ours = threading.local()

    def wrap(self, send: Callable[[Any], None]) -> Callable[[Any], None]:
        def gated(offsets: Any) -> None:
            if not self._allow(tuple(offsets)):
                return
            self._ours.active = True
            try:
                send(offsets)
            finally:
                self._ours.active = False

        return gated

    def _allow(self, offsets: tuple[float, ...]) -> bool:
        if self._app_at is not None and self._clock() - self._app_at < self.APP_HOLD_S:
            self._at_zero = False
            return False
        # Silence would otherwise rewrite the same zero pose 20 times a second.
        if offsets == _ZERO and self._at_zero:
            return False
        self._at_zero = offsets == _ZERO
        return True

    def saw_offsets(self) -> None:
        """Offsets reached the backend; anything but our own came from an app."""
        if not getattr(self._ours, "active", False):
            self._app_at = self._clock()
            self._at_zero = False


class Tap:
    """Feeds the speaker's audio to the media server's wobbler."""

    def __init__(self) -> None:
        self._pipeline: Any = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._pipeline is not None

    def start(self, server: Any) -> None:
        from gi.repository import Gst

        with self._lock:
            if self._pipeline is not None:
                return
            pipeline = Gst.parse_launch(
                f"alsasrc device={audio_tee.WOBBLE_SOURCE} ! audioconvert ! audioresample ! "
                f"audio/x-raw,format=F32LE,channels=1,rate={upstream.wobbler_sample_rate(server)},layout=interleaved ! "
                "appsink name=sink drop=true max-buffers=5 sync=false emit-signals=true"
            )
            pipeline.get_by_name("sink").connect("new-sample", self._on_sample, server)
            bus = pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message::error", self._on_error, server)
            if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                self._close(pipeline)
                logger.warning("Couldn't read %s; the wobble only follows the daemon's own sounds.", audio_tee.WOBBLE_SOURCE)
                return
            self._pipeline = pipeline
        logger.info("Head wobble follows the speaker.")

    def stop(self) -> None:
        with self._lock:
            pipeline, self._pipeline = self._pipeline, None
            if pipeline is not None:
                self._close(pipeline)

    @staticmethod
    def _close(pipeline: Any) -> None:
        from gi.repository import Gst

        pipeline.get_bus().remove_signal_watch()
        pipeline.set_state(Gst.State.NULL)

    def _on_error(self, _bus: Any, message: Any, server: Any) -> None:
        error, _ = message.parse_error()
        logger.warning("Head wobble stopped following the speaker: %s", error)
        self.stop()
        # No more audio is coming to bring the head back.
        wobbler = upstream.head_wobbler(server)
        if wobbler is not None:
            wobbler.reset()

    @staticmethod
    def _on_sample(appsink: Any, server: Any) -> Any:
        from gi.repository import Gst

        sample = appsink.emit("pull-sample")
        wobbler = upstream.head_wobbler(server)
        if sample is not None and wobbler is not None:
            buffer = sample.get_buffer()
            wobbler.feed(np.frombuffer(buffer.extract_dup(0, buffer.get_size()), dtype=np.float32), time.monotonic_ns())
        return Gst.FlowReturn.OK


def apply() -> None:
    gate = Gate()
    tap = Tap()

    def enable(original: Any, server: Any, callback: Any) -> None:
        original(server, gate.wrap(callback))
        if audio_tee.is_installed():
            try:
                tap.start(server)
            except Exception as error:  # noqa: BLE001 - upstream's wobble still works
                logger.warning("Couldn't start following the speaker: %s", error)

    def disable(original: Any, server: Any) -> None:
        tap.stop()
        original(server)

    def on_sample(original: Any, server: Any, appsink: Any) -> Any:
        if not tap.running:
            return original(server, appsink)
        from gi.repository import Gst

        appsink.emit("pull-sample")
        return Gst.FlowReturn.OK

    def set_offsets(original: Any, backend: Any, offsets: Any) -> None:
        gate.saw_offsets()
        original(backend, offsets)

    upstream.wrap_wobbling(enable, disable, on_sample, set_offsets)
    logger.info("Speaker wobble: on")
