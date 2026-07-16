from __future__ import annotations

import json
from datetime import datetime

import pytest


class AudioProxy:
    def __init__(self, states: list[dict | None]) -> None:
        self.states = iter(states)

    async def ws_stream_full_state(self):
        for state in self.states:
            yield state if state is not None else {"error": "daemon_unavailable"}

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
    from reachy_controller.shared_streams import StateBroadcaster

    stream = _audio_level_stream_generator(
        StateBroadcaster(AudioProxy([state]), reconnect_delay=0.01)
    )
    event = await anext(stream)
    await stream.aclose()

    assert event.startswith("event: audio-level\n")
    payload = parse_sse_data(event)
    assert payload["speech_detected"] == expected_speech_detected
    timestamp = datetime.fromisoformat(payload["timestamp"])
    assert timestamp.utcoffset() is not None
    assert timestamp.utcoffset().total_seconds() == 0


@pytest.mark.asyncio
async def test_audio_stream_reports_offline_then_recovers_from_shared_state() -> None:
    from reachy_controller.routes import audio
    from reachy_controller.shared_streams import StateBroadcaster

    stream = audio._audio_level_stream_generator(
        StateBroadcaster(
            AudioProxy([None, {"doa": {"speech_detected": True}}]),
            reconnect_delay=0.01,
        )
    )

    offline = await anext(stream)
    recovered = await anext(stream)
    await stream.aclose()

    assert offline == "event: daemon-unavailable\ndata: {}\n\n"
    assert parse_sse_data(recovered)["speech_detected"] is True


@pytest.mark.asyncio
async def test_audio_stream_cleanup_unsubscribes_from_shared_state() -> None:
    from reachy_controller.routes.audio import _audio_level_stream_generator
    from reachy_controller.shared_streams import StateBroadcaster

    proxy = AudioProxy([{"doa": {"speech_detected": True}}])
    broadcaster = StateBroadcaster(proxy)
    stream = _audio_level_stream_generator(broadcaster)

    await anext(stream)
    await stream.aclose()

    assert broadcaster.subscriber_count == 0
    assert broadcaster.upstream_task is None


@pytest.mark.asyncio
async def test_audio_stream_uses_each_shared_state_update() -> None:
    from reachy_controller.routes.audio import _audio_level_stream_generator
    from reachy_controller.shared_streams import StateBroadcaster

    stream = _audio_level_stream_generator(
        StateBroadcaster(
            AudioProxy(
                [
                    {"doa": {"speech_detected": True}},
                    {"doa": {"speech_detected": False}},
                    {"doa": {"speech_detected": True}},
                ]
            )
        )
    )

    events = [await anext(stream) for _ in range(3)]
    await stream.aclose()

    assert [parse_sse_data(event)["speech_detected"] for event in events] == [
        True,
        False,
        True,
    ]
