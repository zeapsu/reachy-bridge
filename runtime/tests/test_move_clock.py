"""The move clock clamp (#318): a move whose second clock read lands past its
end finishes at its target instead of raising."""

from __future__ import annotations

import asyncio
from itertools import chain, repeat
from types import SimpleNamespace

import numpy as np
import pytest
from reachy_mini.daemon.backend import abstract
from reachy_mini.motion import goto
from reachy_mini.motion.goto import GotoMove
from reachy_mini.utils.interpolation import InterpolationTechnique

from reachy_bridge_runtime import upstream
from reachy_bridge_runtime.features import move_clock

DURATION = 0.2
START = np.eye(4)
TARGET = np.eye(4)
TARGET[2, 3] = 0.01


@pytest.fixture
def restore(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the unwrapped functions back after each test."""
    monkeypatch.setattr(goto, "time_trajectory", goto.time_trajectory)
    monkeypatch.setattr(abstract, "time_trajectory", abstract.time_trajectory)


def _move() -> GotoMove:
    return GotoMove(
        start_head_pose=START,
        target_head_pose=TARGET,
        start_antennas=np.zeros(2),
        target_antennas=np.ones(2),
        start_body_yaw=0.0,
        target_body_yaw=0.0,
        duration=DURATION,
        method=InterpolationTechnique.MIN_JERK,
    )


class _Backend(SimpleNamespace):
    """What ``Backend.play_move`` touches, recording the targets it sets."""

    def __init__(self) -> None:
        super().__init__(_stop_move_requested=False, heads=[], antennas=[])

    def _enter_move(self) -> None:
        return None

    def _exit_move(self, _token: object) -> None:
        return None

    def set_target_head_pose(self, pose: np.ndarray) -> None:
        self.heads.append(pose)

    def set_target_body_yaw(self, _yaw: float) -> None:
        return None

    def set_target_antenna_joint_positions(self, antennas: np.ndarray) -> None:
        self.antennas.append(antennas)


def _play_preempted(monkeypatch: pytest.MonkeyPatch) -> _Backend:
    """Run upstream's play_move with the thread switched out between the
    loop's clock check (still inside the move) and its read of ``t`` (past
    the end), as in the failed CI power-on."""
    clock = chain([0.0, DURATION * 0.99, DURATION * 1.01], repeat(DURATION * 2))
    monkeypatch.setattr(abstract, "time", SimpleNamespace(time=lambda: next(clock)))
    backend = _Backend()
    asyncio.run(abstract.Backend.play_move(backend, _move()))  # type: ignore[arg-type]
    return backend


def test_upstream_fails_a_move_whose_clock_read_lands_past_its_end(
    restore: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="out of range"):
        _play_preempted(monkeypatch)


def test_the_clamped_move_ends_at_its_target(restore: None, monkeypatch: pytest.MonkeyPatch) -> None:
    move_clock.apply()
    backend = _play_preempted(monkeypatch)
    np.testing.assert_allclose(backend.heads[-1], TARGET)
    np.testing.assert_allclose(backend.antennas[-1], np.ones(2))


def test_times_outside_the_move_clamp_to_its_ends(restore: None) -> None:
    move_clock.apply()
    head, antennas, _ = _move().evaluate(DURATION * 1.5)
    np.testing.assert_allclose(head, TARGET)
    np.testing.assert_allclose(antennas, np.ones(2))
    head, antennas, _ = _move().evaluate(-0.01)
    np.testing.assert_allclose(head, START)
    np.testing.assert_allclose(antennas, np.zeros(2))
    # goto_joint_positions calls it from the backend module.
    assert abstract.time_trajectory(-1e-9) == 0.0
    assert abstract.time_trajectory(1.0 + 1e-9) == pytest.approx(1.0)


def test_times_inside_the_move_are_unchanged(restore: None) -> None:
    original = goto.time_trajectory
    move_clock.apply()
    for t in (0.0, 0.25, 0.5, 0.9, 1.0):
        assert goto.time_trajectory(t) == original(t)
        assert abstract.time_trajectory(t, method=InterpolationTechnique.LINEAR) == t


def test_applying_twice_wraps_once(restore: None) -> None:
    move_clock.apply()
    wrapped = goto.time_trajectory
    move_clock.apply()
    assert goto.time_trajectory is wrapped


def test_the_seam_matches_before_and_after_the_patch(restore: None) -> None:
    assert upstream.check({"move_clock"}) == {}
    move_clock.apply()
    assert upstream.check({"move_clock"}) == {}


def test_a_module_with_its_own_time_trajectory_is_a_mismatch(restore: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(goto, "time_trajectory", lambda t, method=None: t)
    problems = upstream.check({"move_clock"})
    assert any("reachy_mini.motion.goto" in p for p in problems["move_clock"])
