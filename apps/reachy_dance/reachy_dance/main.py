import logging
import threading
import time
from dataclasses import dataclass

import numpy as np
from reachy_mini import ReachyMini, ReachyMiniApp
from reachy_mini.utils import create_head_pose


CONTROL_PERIOD_S = 0.02
ANTENNA_AMPLITUDE_RAD = 0.28
RETURN_HOME_DURATION_S = 2.5
SLEEP_CUE_SETTLE_S = 1.0

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MotionTarget:
    head: np.ndarray
    antennas: np.ndarray


def _clip_degrees(value: float, low: float, high: float) -> float:
    return float(np.clip(value, low, high))


def _smoothstep(x: np.ndarray | float) -> np.ndarray | float:
    """Classic 3x^2-2x^3 ease on [0, 1]."""
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def _eased_sine(phase: float) -> float:
    """A sine whose magnitude is smoothstep-shaped.

    Keeps the exact zero-crossings, sign and extrema timing of ``sin(phase)`` —
    so the beat-sync is preserved precisely — while easing the travel between
    them (the head lingers a touch near centre and eases into the accent),
    which reads as less mechanical than a raw sinusoid.
    """
    s = np.sin(phase)
    return float(np.sign(s) * _smoothstep(np.abs(s)))


@dataclass(frozen=True)
class Variation:
    """Slowly-varying, seeded modulation so beats aren't mechanically identical.

    Each channel is a small sum of low-frequency sines (incommensurate with the
    beat), so ``sample()`` returns a smooth value in ~[-1, 1] that drifts over
    many seconds. Deterministic for a given seed (so it is unit-testable) yet
    different run-to-run when seeded from the clock.
    """

    freqs: np.ndarray  # (channels, components) low frequencies in Hz
    phases: np.ndarray  # (channels, components)
    weights: np.ndarray  # (channels, components), each channel row sums to 1

    @classmethod
    def seeded(cls, seed: int, channels: int = 4, components: int = 3) -> "Variation":
        rng = np.random.default_rng(seed)
        freqs = rng.uniform(0.05, 0.25, size=(channels, components))
        phases = rng.uniform(0.0, 2.0 * np.pi, size=(channels, components))
        raw = rng.uniform(0.5, 1.0, size=(channels, components))
        weights = raw / raw.sum(axis=1, keepdims=True)
        return cls(freqs=freqs, phases=phases, weights=weights)

    def sample(self, t: float, channel: int) -> float:
        """Smooth modulation value in ~[-1, 1] for the given channel at time t."""
        f = self.freqs[channel]
        p = self.phases[channel]
        w = self.weights[channel]
        return float(np.sum(w * np.sin(2.0 * np.pi * f * t + p)))


# Channel indices for Variation.sample().
_V_YAW, _V_PITCH, _V_ROLL, _V_ANTENNA = 0, 1, 2, 3


def breathing_pose(t: float, variation: "Variation | None" = None) -> MotionTarget:
    """Slow subtle idle motion: small vertical breath and gentle head roll."""
    breath_phase = 2.0 * np.pi * 0.18 * t
    v_roll = variation.sample(t, _V_ROLL) if variation is not None else 0.0

    z_mm = 3.0 * _eased_sine(breath_phase)
    # Roll trails the breath slightly (follow-through) instead of moving rigidly
    # in phase, and drifts a hair with the slow variation.
    roll_deg = _clip_degrees(
        2.5 * _eased_sine(breath_phase + np.pi / 5.0 - 0.3 + 0.1 * v_roll),
        -40.0,
        40.0,
    )
    antenna = 0.08 * np.sin(breath_phase + np.pi / 2.0)

    return MotionTarget(
        head=create_head_pose(z=z_mm, roll=roll_deg, mm=True, degrees=True),
        antennas=np.array([antenna, -antenna], dtype=float),
    )


def dance_move(
    t: float, bpm: float = 120, variation: "Variation | None" = None
) -> MotionTarget:
    """Beat-synced symbolic choreography for head yaw/pitch/roll and antennas.

    Three techniques layer organic feel onto the beat without breaking sync:
    eased accents (``_eased_sine``), overlapping secondary motion (roll leans
    *into* the yaw turn a beat-fraction late; antennas overshoot and settle each
    beat), and slow seeded ``variation`` so amplitudes/phase drift instead of
    repeating identically. ``variation=None`` reproduces a clean deterministic
    baseline.
    """
    beats = t * bpm / 60.0
    beat_phase = 2.0 * np.pi * beats
    half_phase = np.pi * beats

    if variation is not None:
        v_yaw = variation.sample(t, _V_YAW)
        v_pitch = variation.sample(t, _V_PITCH)
        v_roll = variation.sample(t, _V_ROLL)
        v_ant = variation.sample(t, _V_ANTENNA)
    else:
        v_yaw = v_pitch = v_roll = v_ant = 0.0

    # Primary head-shake, eased and gently amplitude-modulated.
    yaw_deg = _clip_degrees(
        28.0 * (1.0 + 0.08 * v_yaw) * _eased_sine(half_phase),
        -180.0,
        180.0,
    )
    # Pitch keeps its syncopated second harmonic; the primary accent is eased.
    pitch_deg = _clip_degrees(
        9.0 * (1.0 + 0.08 * v_pitch) * _eased_sine(beat_phase + np.pi / 3.0)
        + 4.0 * np.sin(0.5 * beat_phase),
        -40.0,
        40.0,
    )
    # Follow-through: roll trails the yaw turn by ~0.16 beat (a small phase lag),
    # so the head leans into the motion instead of rolling rigidly in phase.
    roll_lag = 0.5 + 0.1 * v_roll
    roll_deg = _clip_degrees(
        12.0 * _eased_sine(half_phase + np.pi / 2.0 - roll_lag),
        -40.0,
        40.0,
    )

    z_mm = 3.0 * _eased_sine(2.0 * np.pi * 0.25 * t)
    head = create_head_pose(
        z=z_mm,
        yaw=yaw_deg,
        pitch=pitch_deg,
        roll=roll_deg,
        mm=True,
        degrees=True,
    )

    # Antennas: eased base sway plus a per-beat overshoot that decays across the
    # beat (a "pluck and settle"). The carrier is zero at each beat boundary, so
    # the decaying envelope adds no discontinuity.
    beat_frac = beats - np.floor(beats)
    overshoot = 0.05 * np.sin(2.0 * np.pi * 2.0 * beats) * np.exp(-3.0 * beat_frac)
    ant_amp = ANTENNA_AMPLITUDE_RAD * (1.0 + 0.08 * v_ant)
    antenna_right = ant_amp * _eased_sine(beat_phase) + overshoot
    antenna_left = ant_amp * _eased_sine(beat_phase + np.pi / 2.0) - overshoot

    return MotionTarget(
        head=head,
        antennas=np.array([antenna_right, antenna_left], dtype=float),
    )


class ReachyDance(ReachyMiniApp):
    custom_app_url: str | None = None
    request_media_backend: str | None = None

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        try:
            start = time.monotonic()
            next_tick = start
            # Clock-seeded so the subtle variation differs run-to-run.
            variation = Variation.seeded(time.time_ns() % (2**32))

            while not stop_event.is_set():
                now = time.monotonic()
                t = now - start

                target = dance_move(t, bpm=120, variation=variation)
                reachy_mini.set_target(head=target.head, antennas=target.antennas)

                next_tick += CONTROL_PERIOD_S
                stop_event.wait(max(0.0, next_tick - time.monotonic()))
        finally:
            self._return_home(reachy_mini)

    @staticmethod
    def _return_home(reachy_mini: ReachyMini) -> None:
        try:
            reachy_mini.goto_target(
                head=create_head_pose(degrees=True),
                antennas=np.array([0.0, 0.0], dtype=float),
                body_yaw=0.0,
                duration=RETURN_HOME_DURATION_S,
            )
        except (AttributeError, TypeError) as exc:
            logger.warning(
                "dance: graceful return-home unavailable; snapping home: %s", exc
            )
            try:
                reachy_mini.set_target(
                    head=create_head_pose(degrees=True),
                    antennas=np.array([0.0, 0.0], dtype=float),
                    body_yaw=0.0,
                )
            except (AttributeError, TypeError) as set_exc:
                logger.warning("dance: failed to snap home on exit: %s", set_exc)
            except Exception as set_exc:
                logger.debug("dance: failed to snap home on exit: %s", set_exc)
        except Exception as exc:
            logger.debug("dance: failed to return home on exit: %s", exc)

        try:
            reachy_mini.media.play_sound("go_sleep.wav")
            time.sleep(SLEEP_CUE_SETTLE_S)
        except AttributeError as exc:
            logger.warning("dance: sleep cue unavailable on exit: %s", exc)
        except Exception as exc:
            logger.debug("dance: sleep cue failed on exit: %s", exc)


if __name__ == "__main__":
    app = ReachyDance()
    try:
        app.wrapped_run()
    except KeyboardInterrupt:
        app.stop()
