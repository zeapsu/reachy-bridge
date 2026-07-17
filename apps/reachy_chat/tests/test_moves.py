"""Movement composition contracts."""

from __future__ import annotations
from unittest.mock import MagicMock

import numpy as np
import pytest

from reachy_chat.moves import MovementManager


def test_antenna_wiggle_is_additive_and_never_moves_head_or_body() -> None:
    """A processing wiggle changes only additive antenna offsets."""
    manager = MovementManager(MagicMock())
    baseline = manager._compose_full_body_pose(10.0)
    amplitude = float(np.deg2rad(4.0))

    manager.wiggle_antennas(duration=1.4, amplitude=amplitude)
    manager._poll_signals(10.0)
    midpoint = manager._compose_full_body_pose(10.7)

    assert np.allclose(midpoint[0], baseline[0])
    assert midpoint[2] == baseline[2]
    assert midpoint[1] == pytest.approx((amplitude, -amplitude))

    finished = manager._compose_full_body_pose(11.4)
    assert np.allclose(finished[0], baseline[0])
    assert finished[1] == pytest.approx(baseline[1])
    assert finished[2] == baseline[2]


def test_antenna_wiggle_rejects_invalid_parameters() -> None:
    """Invalid wiggle durations and amplitudes fail before queuing."""
    manager = MovementManager(MagicMock())

    with pytest.raises(ValueError, match="duration"):
        manager.wiggle_antennas(duration=0.0)
    with pytest.raises(ValueError, match="amplitude"):
        manager.wiggle_antennas(amplitude=-0.1)
