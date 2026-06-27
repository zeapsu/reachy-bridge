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
    ("state", "expected_speech_detected"),
    [
        ({"doa": {"speech_detected": True}}, True),
        ({"doa": {"speech_detected": False}}, False),
        ({}, False),
    ],
)
async def test_audio_stream_reports_honest_speech_detected_signal(
    state: dict, expected_speech_detected: bool
) -> None:
    from reachy_controller.routes.audio import _audio_level_stream_generator

    stream = _audio_level_stream_generator(AudioProxy([state]))
    event = await anext(stream)
    await stream.aclose()

    assert event.startswith("event: audio-level\n")
    payload = parse_sse_data(event)
    assert payload["speech_detected"] == expected_speech_detected
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
    assert parse_sse_data(recovered)["speech_detected"] is True


@pytest.mark.asyncio
async def test_audio_stream_suppresses_client_cancellation() -> None:
    from reachy_controller.routes.audio import _audio_level_stream_generator

    class CancelledProxy:
        async def get_full_state(self) -> dict | None:
            raise asyncio.CancelledError

    stream = _audio_level_stream_generator(CancelledProxy())

    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_audio_stream_cadence_is_10_hz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assert audio stream poll cadence is 0.10s (10 Hz) via actual sleep calls."""
    from reachy_controller.routes import audio

    sleep_calls = []

    async def tracking_sleep(interval: float) -> None:
        sleep_calls.append(interval)

    monkeypatch.setattr(audio.asyncio, "sleep", tracking_sleep)

    # Drive the generator for 3 iterations to observe 2 sleeps
    # (sleep happens after yield, so 3 yields = 2 sleeps before termination)
    stream = audio._audio_level_stream_generator(
        AudioProxy(
            [
                {"doa": {"speech_detected": True}},
                {"doa": {"speech_detected": False}},
                {"doa": {"speech_detected": True}},
            ]
        )
    )

    # Drive the generator for 3 iterations
    await anext(stream)
    await anext(stream)
    await anext(stream)
    await stream.aclose()

    # Verify sleep was called with the correct interval (0.10s = 10 Hz)
    assert len(sleep_calls) >= 2
    assert all(call == 0.10 for call in sleep_calls)
    assert audio._POLL_INTERVAL_SECONDS == 0.10
