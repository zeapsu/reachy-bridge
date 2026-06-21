import threading
import time
from dataclasses import dataclass

import numpy as np
from reachy_mini import ReachyMini, ReachyMiniApp
from reachy_mini.utils import create_head_pose


CONTROL_PERIOD_S = 0.02
ANTENNA_AMPLITUDE_RAD = 0.28


@dataclass(frozen=True)
class MotionTarget:
    head: np.ndarray
    antennas: np.ndarray


def _clip_degrees(value: float, low: float, high: float) -> float:
    return float(np.clip(value, low, high))


def breathing_pose(t: float) -> MotionTarget:
    """Slow subtle idle motion: small vertical breath and gentle head roll."""
    breath_phase = 2.0 * np.pi * 0.18 * t
    roll_phase = breath_phase + np.pi / 5.0

    z_mm = 3.0 * np.sin(breath_phase)
    roll_deg = _clip_degrees(2.5 * np.sin(roll_phase), -40.0, 40.0)
    antenna = 0.08 * np.sin(breath_phase + np.pi / 2.0)

    return MotionTarget(
        head=create_head_pose(z=z_mm, roll=roll_deg, mm=True, degrees=True),
        antennas=np.array([antenna, -antenna], dtype=float),
    )


def dance_move(t: float, bpm: float = 120) -> MotionTarget:
    """Beat-synced symbolic choreography for head yaw/pitch/roll and antennas."""
    beats = t * bpm / 60.0
    beat_phase = 2.0 * np.pi * beats
    half_phase = np.pi * beats

    yaw_deg = _clip_degrees(28.0 * np.sin(half_phase), -180.0, 180.0)
    pitch_deg = _clip_degrees(
        9.0 * np.sin(beat_phase + np.pi / 3.0)
        + 4.0 * np.sin(0.5 * beat_phase),
        -40.0,
        40.0,
    )
    roll_deg = _clip_degrees(12.0 * np.sin(half_phase + np.pi / 2.0), -40.0, 40.0)

    z_mm = 3.0 * np.sin(2.0 * np.pi * 0.25 * t)
    head = create_head_pose(
        z=z_mm,
        yaw=yaw_deg,
        pitch=pitch_deg,
        roll=roll_deg,
        mm=True,
        degrees=True,
    )

    antenna_right = ANTENNA_AMPLITUDE_RAD * np.sin(beat_phase)
    antenna_left = ANTENNA_AMPLITUDE_RAD * np.sin(beat_phase + np.pi / 2.0)

    return MotionTarget(
        head=head,
        antennas=np.array([antenna_right, antenna_left], dtype=float),
    )


class ReachyDance(ReachyMiniApp):
    custom_app_url: str | None = None
    request_media_backend: str | None = None

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        start = time.monotonic()
        next_tick = start

        while not stop_event.is_set():
            now = time.monotonic()
            t = now - start

            target = dance_move(t, bpm=120)
            reachy_mini.set_target(head=target.head, antennas=target.antennas)

            next_tick += CONTROL_PERIOD_S
            stop_event.wait(max(0.0, next_tick - time.monotonic()))

        target = breathing_pose(time.monotonic() - start)
        reachy_mini.set_target(head=target.head, antennas=target.antennas)


if __name__ == "__main__":
    app = ReachyDance()
    try:
        app.wrapped_run()
    except KeyboardInterrupt:
        app.stop()
