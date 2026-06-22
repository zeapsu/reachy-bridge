"""Tests for fail-open addressed-to-me gating."""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from reachy_chat.address_gate import AddressGate, AddressResult


NOW = 1_000.0


def _gate(*, reachy_mini: Any = None, camera_worker: Any = None) -> AddressGate:
    return AddressGate(reachy_mini=reachy_mini, camera_worker=camera_worker)


def test_front_is_pi_over_two() -> None:
    """The microphone's front direction is pi/2, not zero radians."""
    gate = _gate()

    front = gate.fast_check(doa_result=(math.pi / 2, True), face_seen_recently=False)
    side = gate.fast_check(doa_result=(0.0, True), face_seen_recently=False)

    assert front == AddressResult(True, 0.5, "front_facing")
    assert side == AddressResult(False, 0.0, "no_address_cues")


def test_front_and_recent_face_produce_strongest_address_cue() -> None:
    """Two positive physical signals produce the strongest result."""
    result = _gate().fast_check(doa_result=(math.pi / 2, True), face_seen_recently=True)

    assert result == AddressResult(True, 1.0, "front_facing_and_face_detected")


def test_recent_face_keeps_gate_open_for_off_axis_speech() -> None:
    """A recently observed person can address Reachy from outside the front cone."""
    result = _gate().fast_check(doa_result=(0.0, True), face_seen_recently=True)

    assert result == AddressResult(True, 0.5, "face_detected")


def test_front_cone_boundary_is_open() -> None:
    """Speech exactly on the configured front boundary is addressed."""
    gate = _gate()
    angle = math.pi / 2 + gate.front_cone_half_width

    result = gate.fast_check(doa_result=(angle, True), face_seen_recently=False)

    assert result.is_open is True
    assert result.reason == "front_facing"


@pytest.mark.parametrize(
    "doa",
    [None, (0.0, False), (math.nan, True), (math.inf, True), (0.0,), "bad-reading"],
)
def test_missing_or_invalid_doa_degrades_open(doa: Any) -> None:
    """The gate never filters when a speech direction is unavailable."""
    reachy = SimpleNamespace(media=SimpleNamespace(get_DoA=lambda: doa))

    result = _gate(reachy_mini=reachy).fast_check(face_seen_recently=False)

    assert result == AddressResult(True, 0.0, "degrade_open")


def test_missing_face_source_degrades_open() -> None:
    """The gate never filters without a usable face-recency source."""
    result = _gate().fast_check(doa_result=(0.0, True))

    assert result == AddressResult(True, 0.0, "degrade_open")


def test_camera_timestamp_is_converted_to_recent_face_state() -> None:
    """The camera worker's wall-clock timestamp drives face recency."""
    camera = SimpleNamespace(last_face_detected_time=NOW - 1.0)
    gate = _gate(camera_worker=camera)

    result = gate.fast_check(doa_result=(0.0, True), current_time=NOW)

    assert result == AddressResult(True, 0.5, "face_detected")


def test_stale_camera_timestamp_allows_confident_filtering() -> None:
    """Available negative DoA and face signals close the gate."""
    camera = SimpleNamespace(last_face_detected_time=NOW - 10.0)
    gate = _gate(camera_worker=camera)

    result = gate.fast_check(doa_result=(0.0, True), current_time=NOW)

    assert result == AddressResult(False, 0.0, "no_address_cues")


def test_signal_read_exceptions_degrade_open() -> None:
    """Hardware and camera failures cannot escape or close the gate."""
    reachy = MagicMock()
    reachy.media.get_DoA.side_effect = RuntimeError("audio unavailable")
    camera = MagicMock()
    type(camera).last_face_detected_time = property(lambda _self: (_ for _ in ()).throw(RuntimeError("camera")))

    result = _gate(reachy_mini=reachy, camera_worker=camera).fast_check()

    assert result == AddressResult(True, 0.0, "degrade_open")


def test_wake_word_overrides_negative_physical_cues() -> None:
    """A direct name mention addresses Reachy even from outside the front cone."""
    result = _gate().semantic_check(
        "Hey REACHY, look over here",
        doa_result=(0.0, True),
        face_seen_recently=False,
    )

    assert result == AddressResult(True, 1.0, "wake_word_match")


def test_semantic_check_filters_overheard_speech_without_wake_word() -> None:
    """A transcript without a direct cue keeps the negative physical result."""
    result = _gate().semantic_check(
        "Did you finish the report?",
        doa_result=(0.0, True),
        face_seen_recently=False,
    )

    assert result == AddressResult(False, 0.0, "no_address_cues")


class _ExplodingValue:
    def __float__(self) -> float:
        raise RuntimeError("broken signal")

    def lower(self) -> str:
        raise RuntimeError("broken transcript")


def test_public_checks_never_raise_for_broken_values() -> None:
    """Unexpected conversion errors always degrade open."""
    gate = _gate()

    fast = gate.fast_check(doa_result=(_ExplodingValue(), True), face_seen_recently=False)
    semantic = gate.semantic_check(
        _ExplodingValue(),  # type: ignore[arg-type]
        doa_result=(_ExplodingValue(), True),
        face_seen_recently=False,
    )

    assert fast == AddressResult(True, 0.0, "degrade_open")
    assert semantic == AddressResult(True, 0.0, "degrade_open")
