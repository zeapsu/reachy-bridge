from __future__ import annotations

import asyncio

import pytest

from reachy_controller.shared_streams import StateBroadcaster, StatsBroadcaster


class StateProxy:
    def __init__(self) -> None:
        self.stream_calls = 0
        self.stream_closed = asyncio.Event()
        self.next_state = asyncio.Event()

    async def ws_stream_full_state(self):
        self.stream_calls += 1
        try:
            yield {"seq": 1, "doa": {"speech_detected": True}}
            await self.next_state.wait()
            yield {"seq": 2, "doa": {"speech_detected": False}}
            await asyncio.Event().wait()
        finally:
            self.stream_closed.set()


@pytest.mark.asyncio
async def test_state_broadcaster_shares_upstream_and_replays_latest() -> None:
    proxy = StateProxy()
    broadcaster = StateBroadcaster(proxy, queue_size=2, reconnect_delay=0.01)
    first = await broadcaster.subscribe()
    second = await broadcaster.subscribe()

    assert await first.__anext__() == {"seq": 1, "doa": {"speech_detected": True}}
    assert await second.__anext__() == {"seq": 1, "doa": {"speech_detected": True}}
    assert proxy.stream_calls == 1

    replay = await broadcaster.subscribe()
    assert await replay.__anext__() == {"seq": 1, "doa": {"speech_detected": True}}

    proxy.next_state.set()
    assert await first.__anext__() == {"seq": 2, "doa": {"speech_detected": False}}
    assert await second.__anext__() == {"seq": 2, "doa": {"speech_detected": False}}

    await first.aclose()
    await second.aclose()
    await replay.aclose()
    await asyncio.wait_for(proxy.stream_closed.wait(), timeout=1)
    assert broadcaster.subscriber_count == 0
    assert broadcaster.upstream_task is None


@pytest.mark.asyncio
async def test_state_broadcaster_drops_oldest_when_queue_is_full() -> None:
    proxy = StateProxy()
    broadcaster = StateBroadcaster(proxy, queue_size=2, reconnect_delay=0.01)
    subscriber = await broadcaster.subscribe()
    assert await subscriber.__anext__() == {"seq": 1, "doa": {"speech_detected": True}}

    for seq in range(2, 6):
        broadcaster._publish({"seq": seq})

    assert await subscriber.__anext__() == {"seq": 4}
    assert await subscriber.__anext__() == {"seq": 5}
    await subscriber.aclose()


@pytest.mark.asyncio
async def test_state_broadcaster_recovers_after_offline_without_busy_loop() -> None:
    calls = 0
    online = asyncio.Event()

    class RecoveringProxy:
        async def ws_stream_full_state(self):
            nonlocal calls
            calls += 1
            if calls == 1:
                yield {"error": "daemon_unavailable"}
                return
            await online.wait()
            yield {"seq": 3}
            await asyncio.Event().wait()

    broadcaster = StateBroadcaster(RecoveringProxy(), reconnect_delay=0.02)
    subscriber = await broadcaster.subscribe()
    assert await subscriber.__anext__() == {"error": "daemon_unavailable"}
    await asyncio.sleep(0.005)
    assert calls == 1
    online.set()
    assert await asyncio.wait_for(subscriber.__anext__(), timeout=1) == {"seq": 3}
    assert calls == 2
    await subscriber.aclose()


class StatsProxy:
    def __init__(self, online: bool = True) -> None:
        self.online = online
        self.probes = 0

    async def get_daemon_status(self):
        self.probes += 1
        return {"ready": True} if self.online else None


@pytest.mark.asyncio
async def test_stats_broadcaster_samples_once_for_multiple_clients(monkeypatch) -> None:
    proxy = StatsProxy()
    samples = 0
    sample_ready = asyncio.Event()

    def collect():
        nonlocal samples
        samples += 1
        sample_ready.set()
        return {"cpu_percent": samples}

    broadcaster = StatsBroadcaster(
        proxy, collect_stats=collect, interval=0.02, queue_size=2
    )
    first = await broadcaster.subscribe()
    second = await broadcaster.subscribe()
    await asyncio.wait_for(sample_ready.wait(), timeout=1)
    assert await first.__anext__() == {"cpu_percent": 1}
    assert await second.__anext__() == {"cpu_percent": 1}
    await asyncio.sleep(0.005)
    assert samples == 1
    assert proxy.probes == 1

    replay = await broadcaster.subscribe()
    assert await replay.__anext__() == {"cpu_percent": 1}
    await first.aclose()
    await second.aclose()
    await replay.aclose()


@pytest.mark.asyncio
async def test_stats_broadcaster_skips_collection_offline_and_does_no_idle_work() -> None:
    proxy = StatsProxy(online=False)
    collect_calls = 0

    def collect():
        nonlocal collect_calls
        collect_calls += 1
        return {"cpu_percent": 5}

    broadcaster = StatsBroadcaster(proxy, collect_stats=collect, interval=0.01)
    await asyncio.sleep(0.03)
    assert proxy.probes == 0
    assert collect_calls == 0

    subscriber = await broadcaster.subscribe()
    await asyncio.sleep(0.035)
    await subscriber.aclose()
    assert proxy.probes >= 2
    assert collect_calls == 0
    assert broadcaster.subscriber_count == 0
    assert broadcaster.upstream_task is None
