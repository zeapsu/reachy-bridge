"""The mic mixer service (#219): waits for the tee, then becomes alsaloop."""

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

    def execv(path: str, args: list[str]) -> None:
        calls.append(("exec", path, args))
        raise SystemExit(0)  # execv doesn't return

    monkeypatch.setattr(mic_mixer.os, "execv", execv)
    return calls


@pytest.mark.parametrize(("flag", "action"), [("--tee", "ensure"), ("--untee", "remove")])
def test_tee_flags_only_change_the_asoundrc(
    monkeypatch: pytest.MonkeyPatch, calls: list[Any], flag: str, action: str
) -> None:
    monkeypatch.setattr(mic_mixer.sys, "argv", ["reachy-bridge-mic-mixer", flag])
    monkeypatch.setattr(audio_tee, "is_installed", lambda: pytest.fail("shouldn't wait for the tee"))
    mic_mixer.main()
    assert calls == [action]


def test_waits_for_the_tee_then_runs_the_pipeline(monkeypatch: pytest.MonkeyPatch, calls: list[Any]) -> None:
    monkeypatch.setattr(mic_mixer.sys, "argv", ["reachy-bridge-mic-mixer"])
    answers = iter([False, False, True])
    monkeypatch.setattr(audio_tee, "is_installed", lambda: next(answers))
    with pytest.raises(SystemExit):
        mic_mixer.main()
    assert calls[:2] == [("sleep", mic_mixer._WAIT_S)] * 2
    _, path, args = calls[2]
    assert args[0] == path and args[1:] == mic_mixer.command()[1:]
    assert len(calls) == 3
