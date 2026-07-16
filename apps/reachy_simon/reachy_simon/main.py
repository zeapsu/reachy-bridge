import logging
import os
import random
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from importlib import resources
from typing import Any

import numpy as np
from reachy_mini import ReachyMini, ReachyMiniApp
from reachy_mini.utils import create_head_pose
from reachy_mini.utils.interpolation import InterpolationTechnique


ANTENNA_THRESHOLD = 0.3
HEAD_TURN_THRESHOLD = 0.35
HEAD_PITCH_THRESHOLD = 0.25
INPUT_TIMEOUT_SECONDS = 5.0
MAX_SEQUENCE_LENGTH = 8

logger = logging.getLogger(__name__)

CUE_OPTIONS = (
    "right_antenna",
    "left_antenna",
    "head_right",
    "head_left",
    "head_up",
    "head_down",
)


class ReachySimon(ReachyMiniApp):
    custom_app_url: str | None = None
    request_media_backend: str | None = None

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event):
        try:
            self.ready_animation(reachy_mini, stop_event)

            # Signal readiness after ready_animation completes
            _ready_file = os.getenv("REACHY_READY_FILE")
            if _ready_file:
                try:
                    Path(_ready_file).touch()
                except Exception as exc:
                    logger.debug("could not write readiness file: %s", exc)

            sequence: list[str] = []

            while not stop_event.is_set():
                if not sequence or len(sequence) >= MAX_SEQUENCE_LENGTH:
                    sequence = []

                sequence.append(random.choice(CUE_OPTIONS))
                self.play_sequence(reachy_mini, sequence, stop_event)

                if stop_event.is_set():
                    break

                player_sequence, auto_played = self.collect_player_sequence(
                    reachy_mini=reachy_mini,
                    expected_sequence=sequence,
                    stop_event=stop_event,
                )

                if stop_event.is_set():
                    break

                if player_sequence == sequence:
                    self.win_animation(reachy_mini, stop_event)
                    if auto_played:
                        print(f"Simon auto-mode repeated round {len(sequence)}.")
                else:
                    print(f"Simon reset: expected {sequence}, got {player_sequence}.")
                    self.lose_animation(reachy_mini, stop_event)
                    sequence = []

                self.wait_or_stop(stop_event, 0.8)
        finally:
            self._return_home(reachy_mini)

    @staticmethod
    def _return_home(reachy_mini: ReachyMini) -> None:
        home_target = {
            "head": create_head_pose(degrees=True),
            "antennas": np.array([-0.1745, 0.1745], dtype=float),
            "body_yaw": 0.0,
        }
        try:
            reachy_mini.goto_target(
                **home_target,
                duration=2.4,
                method=InterpolationTechnique.MIN_JERK,
            )
        except Exception as exc:
            logger.warning("simon: graceful return-home unavailable; snapping home: %s", exc)
            try:
                reachy_mini.set_target(**home_target)
            except Exception as set_exc:
                logger.warning("simon: failed to snap home on exit: %s", set_exc)
            return

        try:
            asset = resources.files("reachy_simon").joinpath("assets", "return_home.wav")
            with resources.as_file(asset) as sound_path:
                reachy_mini.media.play_sound(str(sound_path))
                time.sleep(0.5)
        except Exception as exc:
            logger.warning("simon: return-home arrival cue unavailable: %s", exc)

    def play_sequence(
        self,
        reachy_mini: ReachyMini,
        sequence: Sequence[str],
        stop_event: threading.Event,
    ):
        self.wait_or_stop(stop_event, 0.4)
        for cue in sequence:
            if stop_event.is_set():
                break
            self.play_cue(reachy_mini, cue, stop_event)
            self.neutral(reachy_mini)
            self.wait_or_stop(stop_event, 0.5)

    def play_cue(
        self,
        reachy_mini: ReachyMini,
        cue: str,
        stop_event: threading.Event,
    ):
        if cue == "right_antenna":
            self.set_antennas(reachy_mini, [0.5, 0.0])
            self.wait_or_stop(stop_event, 0.35)
        elif cue == "left_antenna":
            self.set_antennas(reachy_mini, [0.0, -0.5])
            self.wait_or_stop(stop_event, 0.35)
        elif cue == "head_right":
            self.goto_head(reachy_mini, create_head_pose(yaw=-30.0, degrees=True))
            self.wait_or_stop(stop_event, 0.55)
        elif cue == "head_left":
            self.goto_head(reachy_mini, create_head_pose(yaw=30.0, degrees=True))
            self.wait_or_stop(stop_event, 0.55)
        elif cue == "head_up":
            self.goto_head(reachy_mini, create_head_pose(pitch=20.0, degrees=True))
            self.wait_or_stop(stop_event, 0.55)
        elif cue == "head_down":
            self.goto_head(reachy_mini, create_head_pose(pitch=-20.0, degrees=True))
            self.wait_or_stop(stop_event, 0.55)
        else:
            print(f"Unknown Simon cue ignored: {cue}")

    def collect_player_sequence(
        self,
        reachy_mini: ReachyMini,
        expected_sequence: Sequence[str],
        stop_event: threading.Event,
    ) -> tuple[list[str], bool]:
        inputs: list[str] = []
        deadline = time.monotonic() + INPUT_TIMEOUT_SECONDS
        last_input: str | None = None

        while len(inputs) < len(expected_sequence) and not stop_event.is_set():
            cue = self.detect_player_input(reachy_mini, stop_event)

            if cue is None:
                if time.monotonic() >= deadline:
                    return list(expected_sequence), True
                self.wait_or_stop(stop_event, 0.02)
                last_input = None
                continue

            if cue != last_input:
                inputs.append(cue)
                self.play_input_feedback(reachy_mini, cue, stop_event)
                deadline = time.monotonic() + INPUT_TIMEOUT_SECONDS
                if inputs != list(expected_sequence[: len(inputs)]):
                    return inputs, False

            last_input = cue
            self.wait_or_stop(stop_event, 0.08)

        return inputs, False

    def detect_player_input(
        self,
        reachy_mini: ReachyMini,
        stop_event: threading.Event | None = None,
    ) -> str | None:
        if stop_event is not None and stop_event.is_set():
            return None

        antenna_positions = self.read_antennas(reachy_mini)
        if antenna_positions is not None:
            right, left = antenna_positions
            if abs(right) > ANTENNA_THRESHOLD:
                return "right_antenna"
            if abs(left) > ANTENNA_THRESHOLD:
                return "left_antenna"

        head_position = self.read_head(reachy_mini)
        if head_position is None:
            return None

        yaw, pitch = head_position
        if yaw > HEAD_TURN_THRESHOLD:
            return "head_left"
        if yaw < -HEAD_TURN_THRESHOLD:
            return "head_right"
        if pitch > HEAD_PITCH_THRESHOLD:
            return "head_up"
        if pitch < -HEAD_PITCH_THRESHOLD:
            return "head_down"
        return None

    def read_antennas(self, reachy_mini: ReachyMini) -> tuple[float, float] | None:
        positions = self.read_attr(
            reachy_mini,
            ("get_present_antenna_joint_positions",),
        )
        if positions is None:
            joint_positions = self.read_attr(reachy_mini, ("get_current_joint_positions",))
            if isinstance(joint_positions, tuple) and len(joint_positions) >= 2:
                positions = joint_positions[1]

        if positions is None:
            positions = self.read_attr(reachy_mini, ("antennas", "antenna_positions"))

        if positions is None:
            return None

        values = self.to_float_pair(positions)
        if values is None:
            return None
        return values

    def read_head(self, reachy_mini: ReachyMini) -> tuple[float, float] | None:
        position = self.read_attr(reachy_mini, ("head", "head_position", "head_pose"))
        if position is None:
            return None

        yaw = self.value_from(position, ("yaw", "roll_yaw"))
        pitch = self.value_from(position, ("pitch", "neck_pitch"))
        if yaw is None or pitch is None:
            values = self.to_float_pair(position)
            if values is None:
                return None
            yaw, pitch = values

        return float(yaw), float(pitch)

    def ready_animation(self, reachy_mini: ReachyMini, stop_event: threading.Event):
        for value in (0.18, 0.0, -0.18, 0.0):
            if stop_event.is_set():
                break
            self.set_antennas(reachy_mini, [value, -value])
            self.wait_or_stop(stop_event, 0.12)
        self.neutral(reachy_mini)

    def win_animation(self, reachy_mini: ReachyMini, stop_event: threading.Event):
        for _ in range(2):
            if stop_event.is_set():
                break
            self.set_antennas(reachy_mini, [0.45, 0.45])
            self.wait_or_stop(stop_event, 0.16)
            self.set_antennas(reachy_mini, [0.0, 0.0])
            self.wait_or_stop(stop_event, 0.12)
        self.neutral(reachy_mini)

    def lose_animation(self, reachy_mini: ReachyMini, stop_event: threading.Event):
        for _ in range(4):
            if stop_event.is_set():
                break
            self.set_antennas(reachy_mini, [0.45, -0.45])
            self.wait_or_stop(stop_event, 0.12)
            self.set_antennas(reachy_mini, [-0.45, 0.45])
            self.wait_or_stop(stop_event, 0.12)
        self.neutral(reachy_mini)

    def play_input_feedback(
        self,
        reachy_mini: ReachyMini,
        cue: str,
        stop_event: threading.Event,
    ):
        self.play_cue(reachy_mini, cue, stop_event)
        self.neutral(reachy_mini)

    def neutral(self, reachy_mini: ReachyMini):
        self.safe_set_target(
            reachy_mini,
            head=create_head_pose(),
            antennas=np.array([0.0, 0.0]),
        )

    def set_antennas(self, reachy_mini: ReachyMini, antennas: Sequence[float]):
        self.safe_set_target(
            reachy_mini,
            head=create_head_pose(),
            antennas=np.array(antennas, dtype=float),
        )

    def goto_head(self, reachy_mini: ReachyMini, head_pose: Any):
        try:
            reachy_mini.goto_target(head=head_pose, antennas=np.array([0.0, 0.0]))
        except (AttributeError, TypeError):
            self.safe_set_target(
                reachy_mini,
                head=head_pose,
                antennas=np.array([0.0, 0.0]),
            )

    def safe_set_target(self, reachy_mini: ReachyMini, **target: Any):
        try:
            reachy_mini.set_target(**target)
        except Exception as exc:
            print(f"Reachy Simon motion command skipped: {exc}")

    def wait_or_stop(self, stop_event: threading.Event, duration: float) -> bool:
        return stop_event.wait(duration)

    @staticmethod
    def read_attr(source: Any, names: Sequence[str]) -> Any | None:
        for name in names:
            try:
                value = getattr(source, name)
            except Exception:
                continue

            if callable(value):
                try:
                    value = value()
                except TypeError:
                    pass
                except Exception:
                    continue

            return value
        return None

    @staticmethod
    def value_from(source: Any, names: Sequence[str]) -> float | None:
        for name in names:
            try:
                value = getattr(source, name)
            except Exception:
                continue

            if callable(value):
                try:
                    value = value()
                except TypeError:
                    pass
                except Exception:
                    continue

            try:
                return float(value)
            except (TypeError, ValueError):
                continue

        if isinstance(source, dict):
            for name in names:
                try:
                    return float(source[name])
                except (KeyError, TypeError, ValueError):
                    continue

        return None

    @staticmethod
    def to_float_pair(values: Any) -> tuple[float, float] | None:
        if isinstance(values, dict):
            for first_name, second_name in (
                ("right", "left"),
                ("right_antenna", "left_antenna"),
                ("yaw", "pitch"),
            ):
                if first_name in values and second_name in values:
                    try:
                        return float(values[first_name]), float(values[second_name])
                    except (TypeError, ValueError):
                        return None

        try:
            first, second = values[:2]
        except (TypeError, ValueError, IndexError):
            return None

        try:
            return float(first), float(second)
        except (TypeError, ValueError):
            return None


if __name__ == "__main__":
    app = ReachySimon()
    try:
        app.wrapped_run()
    except KeyboardInterrupt:
        app.stop()
