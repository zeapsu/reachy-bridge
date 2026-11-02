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

While the speaker is muted for hearing, only Talk still comes out of it, so
only Talk moves the head, and an app's own offsets are ignored.

An app using the SDK's wobble also turns the daemon's off when it stops, even
if the dashboard turned it on. ``Choice`` makes the dashboard win: while it wants
the wobble on, an app can't turn it off, at any point. A sleep still stops it, and
the dashboard has to turn it back on.

The daemon doesn't check offsets against the robot's limits, and a driven head
can already be near them. ``Limits`` scales down any offsets, from any source,
that would make the head unreachable, pass a motor's limit or jump its joints.
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
_SPEAKER_CHECK_S = 0.25


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

    def ours(self) -> bool:
        return getattr(self._ours, "active", False)

    def saw_offsets(self) -> None:
        """Offsets reached the backend; anything but our own came from an app."""
        if not self.ours():
            self._app_at = self._clock()
            self._at_zero = False


class Choice:
    """Whether the dashboard's choice keeps the wobble on when an app turns it off."""

    def __init__(self) -> None:
        self._wanted = False
        self._app = threading.local()

    def from_app(self, run: Callable[[], Any]) -> Any:
        """Run ``run``; wobble changes during it are the app's."""
        self._app.active = True
        try:
            return run()
        finally:
            self._app.active = False

    def _is_app(self) -> bool:
        return getattr(self._app, "active", False)

    def enabled(self) -> None:
        if not self._is_app():
            self._wanted = True

    def keep_on(self) -> bool:
        """A wobble-off request: True if it should be ignored."""
        if self._is_app():
            return self._wanted
        self._wanted = False
        return False


class SpeakerSwitch:
    """Whether the speaker is on, read at most a few times a second."""

    def __init__(
        self, read: Callable[[], bool] = audio_tee.speaker_on, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._read = read
        self._clock = clock
        self._at: float | None = None
        self._on = True

    def on(self) -> bool:
        now = self._clock()
        if self._at is None or now - self._at >= _SPEAKER_CHECK_S:
            self._at = now
            self._on = self._read()
        return self._on


def heard_from_speaker(stereo: Any, speaker_on: Callable[[], bool]) -> Any:
    """Mono audio that comes out of the speaker, from interleaved robot audio
    (left) and Talk (right)."""
    robot, talk = stereo[0::2], stereo[1::2]
    # The switch only matters when the robot is making a sound.
    if np.any(robot) and speaker_on():
        return robot + talk
    return talk


class Limits:
    """Scales offsets so the head stays where the motors can safely go."""

    SCALES = (1.0, 0.5, 0.25)
    MARGIN_TICKS = 40.0
    MAX_JOINT_CHANGE_RAD = 0.35

    def __init__(self, solve: Callable[[Any, Any], Any], motor_limits: list[tuple[float, float]]) -> None:
        # solve(backend, offsets or None) -> the seven joints; motor_limits has
        # (lowest, highest) for each of the six head motors.
        self._solve = solve
        self._motors = motor_limits

    def _ok(self, joints: Any, base: Any) -> bool:
        if np.any(np.isnan(joints)):
            return False
        if np.max(np.abs(joints[1:] - base[1:])) > self.MAX_JOINT_CHANGE_RAD:
            return False
        for angle, (low, high) in zip(joints[1:], self._motors, strict=True):
            ticks = upstream.motor_ticks(angle)
            if not low + self.MARGIN_TICKS <= ticks <= high - self.MARGIN_TICKS:
                return False
        return True

    def scale(self, backend: Any, offsets: Any) -> tuple[float, ...]:
        """``offsets``, reduced as far as needed; zero if nothing is safe."""
        offsets = tuple(offsets)
        if offsets == _ZERO:
            return offsets
        try:
            base = np.asarray(self._solve(backend, None))
            if np.any(np.isnan(base)):
                return _ZERO
            for scale in self.SCALES:
                scaled = tuple(scale * value for value in offsets)
                if self._ok(np.asarray(self._solve(backend, scaled)), base):
                    return scaled
        except Exception:  # noqa: BLE001 - never move the head on a failed check
            logger.exception("Couldn't check the head wobble against the robot's limits")
        return _ZERO


class Tap:
    """Feeds the speaker's audio to the media server's wobbler."""

    def __init__(self) -> None:
        self._pipeline: Any = None
        self._lock = threading.Lock()
        self._speaker = SpeakerSwitch()

    @property
    def running(self) -> bool:
        return self._pipeline is not None

    def start(self, server: Any) -> None:
        from gi.repository import Gst

        with self._lock:
            if self._pipeline is not None:
                return
            pipeline = Gst.parse_launch(
                f"alsasrc device={audio_tee.WOBBLE_SOURCE} ! audio/x-raw,channels=2 ! audioconvert ! audioresample ! "
                f"audio/x-raw,format=F32LE,channels=2,rate={upstream.wobbler_sample_rate(server)},layout=interleaved ! "
                "appsink name=sink drop=true max-buffers=5 sync=false emit-signals=true"
            )
            pipeline.get_by_name("sink").connect("new-sample", self._on_sample, server)
            bus = pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message::error", self._on_error, server)
            if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                self._close(pipeline)
                logger.warning(
                    "Couldn't read %s; the wobble only follows the daemon's own sounds.", audio_tee.WOBBLE_SOURCE
                )
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

    def _on_sample(self, appsink: Any, server: Any) -> Any:
        from gi.repository import Gst

        sample = appsink.emit("pull-sample")
        wobbler = upstream.head_wobbler(server)
        if sample is not None and wobbler is not None:
            buffer = sample.get_buffer()
            stereo = np.frombuffer(buffer.extract_dup(0, buffer.get_size()), dtype=np.float32)
            wobbler.feed(heard_from_speaker(stereo, self._speaker.on), time.monotonic_ns())
        return Gst.FlowReturn.OK


def apply() -> None:
    gate = Gate()
    tap = Tap()
    limits = Limits(*upstream.wobble_limits())
    speaker = SpeakerSwitch()
    choice = Choice()

    def process_command(original: Any, backend: Any, cmd: Any, *args: Any, **kwargs: Any) -> Any:
        # Only this command: a sleep must still stop the wobble before it moves.
        if type(cmd).__name__ != "SetWobblingCmd":
            return original(backend, cmd, *args, **kwargs)
        return choice.from_app(lambda: original(backend, cmd, *args, **kwargs))

    def enable(original: Any, server: Any, callback: Any) -> None:
        choice.enabled()
        original(server, gate.wrap(callback))
        if audio_tee.is_installed():
            try:
                tap.start(server)
            except Exception as error:  # noqa: BLE001 - upstream's wobble still works
                logger.warning("Couldn't start following the speaker: %s", error)

    def disable(original: Any, server: Any) -> None:
        if choice.keep_on():
            return
        tap.stop()
        original(server)

    def on_sample(original: Any, server: Any, appsink: Any) -> Any:
        if not tap.running:
            return original(server, appsink)
        from gi.repository import Gst

        appsink.emit("pull-sample")
        return Gst.FlowReturn.OK

    def set_offsets(original: Any, backend: Any, offsets: Any) -> None:
        if not gate.ours() and any(offsets) and not speaker.on():
            return
        gate.saw_offsets()
        original(backend, limits.scale(backend, offsets))

    upstream.wrap_wobbling(enable, disable, on_sample, set_offsets, process_command)
    logger.info("Speaker wobble: on")
