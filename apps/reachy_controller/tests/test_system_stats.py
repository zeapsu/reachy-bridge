"""Tests for the system stats stream's daemon gating."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from reachy_controller.routes import system_stats


@pytest.mark.asyncio
async def test_stats_stream_skips_host_read_while_daemon_offline(monkeypatch) -> None:
    """With the daemon unreachable, the stream emits keepalives only and never samples the host."""
    monkeypatch.setattr(system_stats, "_STATS_UPDATE_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(system_stats, "_SSE_KEEPALIVE_INTERVAL_SECONDS", 0.03)
    collect = MagicMock(return_value={"cpu_percent": 5})
    monkeypatch.setattr(system_stats, "collect_jetson_stats", collect)

    proxy = AsyncMock()
    proxy.get_daemon_status = AsyncMock(return_value=None)

    gen = system_stats._system_stats_stream_generator(proxy)
    try:
        first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    finally:
        await gen.aclose()

    assert "keepalive" in first
    assert "stats-update" not in first
    collect.assert_not_called()
    assert proxy.get_daemon_status.await_count >= 2  # kept polling, kept skipping


@pytest.mark.asyncio
async def test_stats_stream_emits_while_daemon_online(monkeypatch) -> None:
    """With the daemon reachable, the stream samples the host and emits stats-update."""
    monkeypatch.setattr(system_stats, "_STATS_UPDATE_INTERVAL_SECONDS", 0.01)
    collect = MagicMock(return_value={"cpu_percent": 5})
    monkeypatch.setattr(system_stats, "collect_jetson_stats", collect)

    proxy = AsyncMock()
    proxy.get_daemon_status = AsyncMock(return_value={"state": "running"})

    gen = system_stats._system_stats_stream_generator(proxy)
    try:
        first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    finally:
        await gen.aclose()

    assert "stats-update" in first
    assert '"cpu_percent": 5' in first
    collect.assert_called_once()
