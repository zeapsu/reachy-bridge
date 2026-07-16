"""Focused tests for dance's app-owned return-home choreography (#34)."""

import logging
import wave
from importlib import resources
from pathlib import Path

import numpy as np
from reachy_mini.utils.interpolation import InterpolationTechnique

import reachy_dance.main as dance_main
from reachy_dance.main import ReachyDance


class _FakeMedia:
    def __init__(self, events: list[tuple[str, object]], *, error: Exception | None = None) -> None:
        self._events = events
        self._error = error

    def play_sound(self, sound_file: str) -> None:
        self._events.append(("audio", sound_file))
        if self._error:
            raise self._error


class _FakeReachy:
    def __init__(self, *, goto_error: Exception | None = None, audio_error: Exception | None = None) -> None:
        self.events: list[tuple[str, object]] = []
        self._goto_error = goto_error
        self.media = _FakeMedia(self.events, error=audio_error)

    def goto_target(self, **kwargs: object) -> None:
        self.events.append(("goto", kwargs))
        if self._goto_error:
            raise self._goto_error

    def set_target(self, **kwargs: object) -> None:
        self.events.append(("set", kwargs))


def _assert_target(target: dict[str, object]) -> None:
    assert np.allclose(target["head"], np.eye(4))
    assert np.allclose(target["antennas"], [-0.1745, 0.1745])
    assert target["body_yaw"] == 0.0


def test_return_home_moves_before_playing_packaged_sound(monkeypatch) -> None:
    reachy = _FakeReachy()
    sleep_calls: list[float] = []
    monkeypatch.setattr(dance_main.time, "sleep", sleep_calls.append)

    ReachyDance._return_home(reachy)

    assert [name for name, _ in reachy.events] == ["goto", "audio"]
    target = reachy.events[0][1]
    assert isinstance(target, dict)
    _assert_target(target)
    assert target["duration"] == 2.4
    assert target["method"] is InterpolationTechnique.MIN_JERK
    assert Path(reachy.events[1][1]).name == "return_home.wav"
    assert sleep_calls == [0.5]


def test_return_home_falls_back_when_motion_fails(caplog) -> None:
    reachy = _FakeReachy(goto_error=RuntimeError("daemon unavailable"))

    with caplog.at_level(logging.WARNING):
        ReachyDance._return_home(reachy)

    assert [name for name, _ in reachy.events] == ["goto", "set"]
    fallback = reachy.events[1][1]
    assert isinstance(fallback, dict)
    _assert_target(fallback)
    assert "return-home" in caplog.text


def test_return_home_audio_failure_does_not_escape(monkeypatch) -> None:
    reachy = _FakeReachy(audio_error=RuntimeError("audio unavailable"))
    sleep_calls: list[float] = []
    monkeypatch.setattr(dance_main.time, "sleep", sleep_calls.append)

    ReachyDance._return_home(reachy)

    assert [name for name, _ in reachy.events] == ["goto", "audio"]
    assert sleep_calls == []


def test_return_home_resource_is_a_decodable_pcm_wav() -> None:
    asset = resources.files("reachy_dance").joinpath("assets", "return_home.wav")

    with resources.as_file(asset) as path:
        with wave.open(str(path), "rb") as wav:
            assert wav.getcomptype() == "NONE"
            assert wav.getnchannels() == 1
            assert wav.getsampwidth() == 2
            assert wav.getframerate() >= 8000
            assert wav.getnframes() > 0
