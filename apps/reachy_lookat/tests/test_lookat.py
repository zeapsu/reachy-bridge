"""Headless unit tests for reachy_lookat (issues #17 and #9).

No robot/daemon required: a fake ReachyMini stands in for the SDK so the control
loop's frame-handling and the new return-to-neutral-on-exit behaviour can be
verified on any platform.
"""

import logging
import threading

import numpy as np

from reachy_lookat import main as lookat
from reachy_lookat.gstreamer_nvidia_patch import (
    _is_jetson,
    apply_nvidia_unixfd_patch,
)
from reachy_lookat.main import (
    ReachyLookat,
    YAW_LIMITS,
    compute_target_from_face,
    smooth_target,
)


class _FakeMedia:
    def __init__(self, frame=None):
        self._frame = frame

    def get_frame(self):
        return self._frame


class _FakeReachy:
    """Records set_target() calls instead of talking to a daemon."""

    def __init__(self, frame=None):
        self.media = _FakeMedia(frame)
        self.calls: list[dict] = []

    def set_target(self, **kwargs):
        self.calls.append(kwargs)


# --- #17: Jetson patch is a safe no-op off-Jetson -------------------------------


def test_patch_is_noop_off_jetson():
    # On the dev Mac / CI this must not raise and must report non-Jetson.
    assert _is_jetson() is False
    apply_nvidia_unixfd_patch()  # idempotent no-op


# --- #17: failures are logged (once), not silently swallowed --------------------


def test_warn_once_logs_a_single_time(caplog):
    lookat._warned_keys.discard("unit-test-key")
    with caplog.at_level(logging.WARNING):
        lookat._warn_once("unit-test-key", "first message")
        lookat._warn_once("unit-test-key", "second message")
    emitted = [r for r in caplog.records if "message" in r.getMessage()]
    assert len(emitted) == 1


# --- #9: head recenters on exit -------------------------------------------------


def test_return_to_neutral_sends_centered_pose():
    reachy = _FakeReachy()
    ReachyLookat._return_to_neutral(reachy)
    assert len(reachy.calls) == 1
    call = reachy.calls[-1]
    assert call["body_yaw"] == 0.0
    head = call["head"]
    assert head.shape == (4, 4)
    # Identity rotation = head looking straight ahead.
    assert np.allclose(head[:3, :3], np.eye(3), atol=1e-6)


def test_run_recenters_when_stopped_immediately():
    # stop_event pre-set -> loop body never runs, but the finally: must still fire.
    reachy = _FakeReachy(frame=None)
    stop = threading.Event()
    stop.set()
    ReachyLookat().run(reachy, stop)
    assert reachy.calls, "run() must issue a neutral set_target on exit (#9)"
    assert reachy.calls[-1]["body_yaw"] == 0.0


# --- pure geometry helpers ------------------------------------------------------


def test_compute_target_centered_face_is_neutral():
    frame_shape = (480, 640, 3)
    # A face whose centre is the frame centre -> no yaw/pitch demand.
    face = (320 - 25, 240 - 25, 50, 50)
    yaw, pitch = compute_target_from_face(face, frame_shape)
    assert abs(yaw) < 1e-6
    assert abs(pitch) < 1e-6


def test_compute_target_face_to_the_right_yields_negative_yaw():
    frame_shape = (480, 640, 3)
    # Face on the right half of the image -> yaw turns toward it (negative sign).
    face = (500, 200, 50, 50)
    yaw, _ = compute_target_from_face(face, frame_shape)
    assert yaw < 0.0


def test_smooth_target_moves_toward_and_clamps():
    yaw, pitch = smooth_target(0.0, 0.0, 100.0, 100.0, alpha=0.4)
    assert 0.0 < yaw <= 100.0
    # pitch is clamped to PITCH_LIMITS even though the eased value is small here
    assert yaw == 40.0  # 0 + 0.4*(100-0)
    # clamping holds at the yaw bound
    big, _ = smooth_target(YAW_LIMITS[1], 0.0, 1e6, 0.0, alpha=1.0)
    assert big == YAW_LIMITS[1]
