from __future__ import annotations

import asyncio
import json
import math

import pytest

from reachy_controller.shared_streams import AudioLevelsBroadcaster


LEVELS = {
    "rms": 0.62,
    "rms_db": -14.5,
    "low": 0.8,
    "mid": 0.2,
    "high": 0.05,
    "sample_rate_hz": 16_000,
    "source": "daemon mic tap",
    "timestamp": "2026-07-16T08:00:00+00:00",
}


def parse_sse_data(event: str) -> dict:
    data_line = next(line for line in event.splitlines() if line.startswith("data: "))
    return json.loads(data_line.removeprefix("data: "))



@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rms", True),
        ("rms", math.nan),
        ("low", "0.2"),
        ("mid", None),
        ("high", math.inf),
        ("rms_db", "-14.5"),
        ("rms_db", 1.0),
        ("sample_rate_hz", False),
        ("sample_rate_hz", 0),
        ("source", " "),
        ("timestamp", None),
    ],
)
def test_audio_levels_validator_rejects_incomplete_or_nonfinite_contract(
    field: str, value: object
) -> None:
    payload = dict(LEVELS, **{field: value})
    assert AudioLevelsBroadcaster._valid_levels(payload) is False


def test_audio_levels_validator_accepts_complete_daemon_contract() -> None:
    assert AudioLevelsBroadcaster._valid_levels(LEVELS) is True

class AudioProxy:
    def __init__(self, levels: list[dict | None]) -> None:
        self.levels = iter(levels)
        self.calls = 0
        self.full_state_calls = 0

    async def get_audio_levels(self):
        self.calls += 1
        try:
            return next(self.levels)
        except StopIteration:
            await asyncio.Event().wait()
            return None

    async def get_full_state(self):
        self.full_state_calls += 1
        raise AssertionError("audio must not poll full state")


@pytest.mark.asyncio
async def test_audio_stream_reports_real_levels_payload() -> None:
    from reachy_controller.routes.audio import _audio_level_stream_generator

    proxy = AudioProxy([LEVELS])
    broadcaster = AudioLevelsBroadcaster(proxy, interval=0.01, reconnect_delay=0.01)
    stream = _audio_level_stream_generator(broadcaster)

    event = await anext(stream)
    await stream.aclose()

    assert event.startswith("event: audio-level\n")
    assert parse_sse_data(event) == LEVELS
    assert proxy.full_state_calls == 0


@pytest.mark.asyncio
async def test_invalid_audio_response_emits_unavailable_before_recovery() -> None:
    from reachy_controller.routes.audio import _audio_level_stream_generator

    proxy = AudioProxy([dict(LEVELS, low=True), LEVELS])
    broadcaster = AudioLevelsBroadcaster(proxy, interval=0.01, reconnect_delay=0.01)
    stream = _audio_level_stream_generator(broadcaster)

    unavailable = await anext(stream)
    recovered = await anext(stream)
    await stream.aclose()

    assert unavailable == "event: daemon-unavailable\ndata: {}\n\n"
    assert parse_sse_data(recovered) == LEVELS


@pytest.mark.asyncio
async def test_audio_stream_reports_offline_then_recovers() -> None:
    from reachy_controller.routes import audio

    recovered = dict(LEVELS, rms=0.9)
    proxy = AudioProxy([None, recovered])
    broadcaster = AudioLevelsBroadcaster(proxy, interval=0.01, reconnect_delay=0.01)
    stream = audio._audio_level_stream_generator(broadcaster)

    offline = await anext(stream)
    online = await anext(stream)
    await stream.aclose()

    assert offline == "event: daemon-unavailable\ndata: {}\n\n"
    assert parse_sse_data(online) == recovered
    assert proxy.calls == 2


@pytest.mark.asyncio
async def test_audio_stream_cleanup_unsubscribes_and_cancels_poll() -> None:
    from reachy_controller.routes.audio import _audio_level_stream_generator

    proxy = AudioProxy([LEVELS])
    broadcaster = AudioLevelsBroadcaster(proxy)
    stream = _audio_level_stream_generator(broadcaster)

    await anext(stream)
    await stream.aclose()

    assert broadcaster.subscriber_count == 0
    assert broadcaster.upstream_task is None


@pytest.mark.asyncio
async def test_audio_stream_shares_one_daemon_get_for_multiple_clients() -> None:

    proxy = AudioProxy([LEVELS])
    broadcaster = AudioLevelsBroadcaster(proxy, interval=1.0)
    first = await broadcaster.subscribe()
    second = await broadcaster.subscribe()

    assert await first.__anext__() == LEVELS
    assert await second.__anext__() == LEVELS
    assert proxy.calls == 1
    await first.aclose()
    await second.aclose()


@pytest.mark.asyncio
async def test_audio_broadcaster_does_no_idle_work_and_replays_latest() -> None:
    proxy = AudioProxy([LEVELS])
    broadcaster = AudioLevelsBroadcaster(proxy, interval=0.01)

    await asyncio.sleep(0.02)
    assert proxy.calls == 0

    subscriber = await broadcaster.subscribe()
    assert await subscriber.__anext__() == LEVELS
    replay = await broadcaster.subscribe()
    assert await replay.__anext__() == LEVELS
    await subscriber.aclose()
    await replay.aclose()



@pytest.mark.asyncio
async def test_audio_broadcaster_drops_idle_snapshot_before_resubscribe() -> None:
    proxy = AudioProxy([LEVELS, None])
    broadcaster = AudioLevelsBroadcaster(proxy, interval=0.01, reconnect_delay=0.01)

    first = await broadcaster.subscribe()
    assert await first.__anext__() == LEVELS
    await first.aclose()

    second = await broadcaster.subscribe()
    assert await second.__anext__() == {"error": "daemon_unavailable"}
    assert proxy.calls == 2
    await second.aclose()

@pytest.mark.asyncio
async def test_audio_stream_keepalive_and_close() -> None:
    from reachy_controller.routes import audio
    from reachy_controller.routes.audio import _audio_level_stream_generator

    original_interval = audio._KEEPALIVE_INTERVAL_SECONDS
    audio._KEEPALIVE_INTERVAL_SECONDS = 0.01
    try:
        proxy = AudioProxy([])
        broadcaster = AudioLevelsBroadcaster(proxy, reconnect_delay=1.0)
        stream = _audio_level_stream_generator(broadcaster)
        event = await anext(stream)
        assert event == ": keepalive\n\n"
        await stream.aclose()
    finally:
        audio._KEEPALIVE_INTERVAL_SECONDS = original_interval
