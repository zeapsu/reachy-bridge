"""Headless unit tests for reachy_dance choreography (issue #19).

These assert the naturalism changes without a robot: the eased motion stays
beat-synced (zero-crossings preserved), the seeded variation is deterministic
and actually changes the motion, and the trajectory stays continuous (the
per-beat antenna overshoot must not introduce a jump) and within bounds.
"""

import numpy as np

from reachy_dance.main import (
    ANTENNA_AMPLITUDE_RAD,
    Variation,
    _eased_sine,
    _smoothstep,
    breathing_pose,
    dance_move,
)

BPM = 120


# --- easing helpers -------------------------------------------------------------


def test_smoothstep_endpoints_and_monotonic():
    assert _smoothstep(0.0) == 0.0
    assert _smoothstep(1.0) == 1.0
    xs = np.linspace(0.0, 1.0, 50)
    ys = np.array([_smoothstep(x) for x in xs])
    assert np.all(np.diff(ys) >= -1e-9)  # non-decreasing


def test_eased_sine_preserves_sign_and_zero_crossings():
    # Same sign as sin everywhere, and exact zeros where sin is zero -> the beat
    # timing is identical to the original sinusoid.
    for phase in np.linspace(-4 * np.pi, 4 * np.pi, 400):
        eased = _eased_sine(phase)
        s = np.sin(phase)
        assert np.sign(round(eased, 12)) == np.sign(round(float(s), 12))
        assert abs(eased) <= 1.0 + 1e-9
    for k in range(-3, 4):
        assert abs(_eased_sine(k * np.pi)) < 1e-9  # zeros at every half-beat


# --- seeded variation -----------------------------------------------------------


def test_variation_is_deterministic_per_seed():
    a = Variation.seeded(42)
    b = Variation.seeded(42)
    assert np.array_equal(a.freqs, b.freqs)
    samples_a = [a.sample(t, 0) for t in np.linspace(0, 10, 25)]
    samples_b = [b.sample(t, 0) for t in np.linspace(0, 10, 25)]
    assert samples_a == samples_b


def test_variation_differs_between_seeds_and_is_bounded():
    a = Variation.seeded(1)
    b = Variation.seeded(2)
    assert not np.array_equal(a.freqs, b.freqs)
    for t in np.linspace(0, 30, 100):
        for ch in range(4):
            assert abs(a.sample(t, ch)) <= 1.0 + 1e-9  # weights sum to 1


# --- dance_move -----------------------------------------------------------------


def test_dance_move_deterministic_with_fixed_variation():
    v = Variation.seeded(7)
    t = 3.14
    m1 = dance_move(t, bpm=BPM, variation=v)
    m2 = dance_move(t, bpm=BPM, variation=v)
    assert np.array_equal(m1.head, m2.head)
    assert np.array_equal(m1.antennas, m2.antennas)


def test_variation_changes_the_motion_vs_baseline():
    v = Variation.seeded(3)
    ts = np.linspace(0.05, 8.0, 200)
    differ = any(
        not np.allclose(
            dance_move(t, bpm=BPM, variation=v).antennas,
            dance_move(t, bpm=BPM).antennas,
        )
        for t in ts
    )
    assert differ, "seeded variation should perturb the choreography"


def test_dance_move_returns_valid_pose_and_bounded_antennas():
    v = Variation.seeded(11)
    for t in np.linspace(0, 12, 400):
        m = dance_move(t, bpm=BPM, variation=v)
        head = m.head
        assert head.shape == (4, 4)
        rot = head[:3, :3]
        assert np.allclose(rot @ rot.T, np.eye(3), atol=1e-6)  # valid rotation
        assert np.allclose(head[3, :], [0.0, 0.0, 0.0, 1.0])
        assert np.all(np.isfinite(m.antennas))
        # eased base (|.|<=1) * amp(<=1.08) + overshoot(<=0.05)
        assert np.all(np.abs(m.antennas) <= ANTENNA_AMPLITUDE_RAD * 1.08 + 0.05 + 1e-9)


def test_trajectory_is_continuous_across_beats():
    # A discontinuity (e.g. a bad overshoot envelope at beat boundaries) would
    # show up as a large step between adjacent fine-grained samples.
    v = Variation.seeded(5)
    dt = 0.004
    ts = np.arange(0.0, 10.0, dt)
    prev = dance_move(ts[0], bpm=BPM, variation=v)
    for t in ts[1:]:
        cur = dance_move(t, bpm=BPM, variation=v)
        assert np.max(np.abs(cur.head - prev.head)) < 0.05
        assert np.max(np.abs(cur.antennas - prev.antennas)) < 0.05
        prev = cur


def test_baseline_dance_move_still_callable_without_variation():
    m = dance_move(1.23, bpm=BPM)
    assert m.head.shape == (4, 4)
    assert m.antennas.shape == (2,)


def test_breathing_pose_variation_optional():
    a = breathing_pose(2.0)
    b = breathing_pose(2.0, variation=Variation.seeded(9))
    assert a.head.shape == (4, 4)
    assert b.head.shape == (4, 4)
