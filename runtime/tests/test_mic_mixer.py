"""The mic mixer service (#219): waits for the tee, then runs alsaloop."""

from __future__ import annotations

from typing import Any

import pytest

from reachy_bridge_runtime import audio_tee, mic_mixer


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []
    monkeypatch.setattr(audio_tee, "ensure", lambda: calls.append("ensure"))
    monkeypatch.setattr(audio_tee, "remove", lambda: calls.append("remove"))
    monkeypatch.setattr(mic_mixer.time, "sleep", lambda s: calls.append(("sleep", s)))

    return calls


@pytest.mark.parametrize(("flag", "action"), [("--tee", "ensure"), ("--untee", "remove")])
def test_tee_flags_only_change_the_asoundrc(
    monkeypatch: pytest.MonkeyPatch, calls: list[Any], flag: str, action: str
) -> None:
    monkeypatch.setattr(mic_mixer.sys, "argv", ["reachy-bridge-mic-mixer", flag])
    monkeypatch.setattr(audio_tee, "is_installed", lambda: pytest.fail("shouldn't wait for the tee"))
    mic_mixer.main()
    assert calls == [action]


class FakeProcess:
    def __init__(self, exits_after: int | None = None) -> None:
        self.polls = 0
        self.exits_after = exits_after
        self.returncode: int | None = None

    def poll(self) -> int | None:
        self.polls += 1
        if self.exits_after is not None and self.polls > self.exits_after:
            self.returncode = 3
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def wait(self) -> int | None:
        return self.returncode


def test_waits_for_the_tee_then_runs_the_bridge(monkeypatch: pytest.MonkeyPatch, calls: list[Any]) -> None:
    monkeypatch.setattr(mic_mixer.sys, "argv", ["reachy-bridge-mic-mixer"])
    answers = iter([False, False, True])
    monkeypatch.setattr(audio_tee, "is_installed", lambda: next(answers))
    started: list[list[str]] = []

    def popen(args: list[str]) -> FakeProcess:
        started.append(args)
        return FakeProcess(exits_after=0)

    monkeypatch.setattr(mic_mixer.subprocess, "Popen", popen)
    with pytest.raises(SystemExit) as stopped:
        mic_mixer.main()
    assert stopped.value.code == 3
    assert calls == [("sleep", mic_mixer._WAIT_S)] * 2
    assert started == [mic_mixer.command()]


def test_stops_the_bridge_when_the_sound_cards_change(monkeypatch: pytest.MonkeyPatch, calls: list[Any]) -> None:
    process = FakeProcess()
    monkeypatch.setattr(mic_mixer.subprocess, "Popen", lambda _args: process)
    cards = iter(["with mic", "with mic", "without"])
    monkeypatch.setattr(mic_mixer, "_cards", lambda: next(cards))
    assert mic_mixer._bridge() == 1
    assert process.returncode == -15
    assert calls == [("sleep", mic_mixer._WATCH_S)] * 2
