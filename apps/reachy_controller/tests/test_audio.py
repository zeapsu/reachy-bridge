from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest


class AudioProxy:
    def __init__(self, states: list[dict | None]) -> None:
        self.states = iter(states)

    async def get_full_state(self) -> dict | None:
        return next(self.states)


def parse_sse_data(event: str) -> dict:
    data_line = next(line for line in event.splitlines() if line.startswith("data: "))
    return json.loads(data_line.removeprefix("data: "))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected_level"),
    [
        ({"doa": {"speech_detected": True}}, 0.7),
        ({"doa": {"speech_detected": False}}, 0.0),
        ({}, 0.0),
    ],
)
async def test_audio_stream_maps_real_speech_signal_to_level(
    state: dict, expected_level: float
) -> None:
    from reachy_controller.routes.audio import _audio_level_stream_generator

    stream = _audio_level_stream_generator(AudioProxy([state]))
    event = await anext(stream)
    await stream.aclose()

    assert event.startswith("event: audio-level\n")
    payload = parse_sse_data(event)
    assert payload["level"] == expected_level
    timestamp = datetime.fromisoformat(payload["timestamp"])
    assert timestamp.utcoffset() is not None
    assert timestamp.utcoffset().total_seconds() == 0


@pytest.mark.asyncio
async def test_audio_stream_reports_offline_then_keeps_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reachy_controller.routes import audio

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(audio.asyncio, "sleep", no_sleep)
    stream = audio._audio_level_stream_generator(
        AudioProxy([None, {"doa": {"speech_detected": True}}])
    )

    offline = await anext(stream)
    recovered = await anext(stream)
    await stream.aclose()

    assert offline == "event: daemon-unavailable\ndata: {}\n\n"
    assert parse_sse_data(recovered)["level"] == 0.7


@pytest.mark.asyncio
async def test_audio_stream_suppresses_client_cancellation() -> None:
    from reachy_controller.routes.audio import _audio_level_stream_generator

    class CancelledProxy:
        async def get_full_state(self) -> dict | None:
            raise asyncio.CancelledError

    stream = _audio_level_stream_generator(CancelledProxy())

    with pytest.raises(StopAsyncIteration):
        await anext(stream)
