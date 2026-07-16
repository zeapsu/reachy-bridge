"""Tests for the system stats stream's daemon gating."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from reachy_controller.routes import system_stats


@pytest.mark.asyncio
async def test_stats_stream_skips_host_read_while_daemon_offline(monkeypatch) -> None:
    """Offline shared samples produce keepalives and never read host stats."""
    monkeypatch.setattr(system_stats, "_SSE_KEEPALIVE_INTERVAL_SECONDS", 0.03)
    collect = MagicMock(return_value={"cpu_percent": 5})

    class OfflineProxy:
        probe_count = 0

        async def get_daemon_status(self):
            self.probe_count += 1
            return None

    proxy = OfflineProxy()
    from reachy_controller.shared_streams import StatsBroadcaster

    broadcaster = StatsBroadcaster(proxy, collect_stats=collect, interval=0.01)
    gen = system_stats._system_stats_stream_generator(broadcaster)
    try:
        first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    finally:
        await gen.aclose()

    assert "keepalive" in first
    assert "stats-update" not in first
    collect.assert_not_called()
    assert proxy.probe_count >= 2


@pytest.mark.asyncio
async def test_stats_stream_emits_while_daemon_online() -> None:
    """Online shared samples retain the stats-update event contract."""
    collect = MagicMock(return_value={"cpu_percent": 5})

    class OnlineProxy:
        async def get_daemon_status(self):
            return {"state": "running"}

    from reachy_controller.shared_streams import StatsBroadcaster

    broadcaster = StatsBroadcaster(
        OnlineProxy(), collect_stats=collect, interval=0.01
    )
    gen = system_stats._system_stats_stream_generator(broadcaster)
    try:
        first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    finally:
        await gen.aclose()

    assert "stats-update" in first
    assert '"cpu_percent": 5' in first
    collect.assert_called_once()
