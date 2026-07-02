"""System statistics streaming (CPU, GPU, memory, temperature, audio routing)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from reachy_controller.daemon_proxy import DaemonProxy
from reachy_controller.jetson_stats import collect_jetson_stats

router = APIRouter(prefix="/api", tags=["system"])

_SSE_KEEPALIVE_INTERVAL_SECONDS = 15.0
_STATS_UPDATE_INTERVAL_SECONDS = 1.5


async def _system_stats_stream_generator(
    proxy: DaemonProxy,
) -> AsyncGenerator[str, None]:
    """Generate Server-Sent Events for system stats updates.

    Emits stats-update events every 1-2 seconds — but only while the daemon is
    reachable. The stats element reports host telemetry during robot operation;
    with Reachy off it must not turn into a general-purpose system monitor, so
    the host read is skipped entirely (not just unsent).
    Emits keep-alive comments every ~15s to prevent proxy timeouts.
    """
    last_event_time = asyncio.get_event_loop().time()

    try:
        while True:
            if await proxy.get_daemon_status() is not None:
                stats = await asyncio.to_thread(collect_jetson_stats)
                yield f"event: stats-update\ndata: {json.dumps(stats)}\n\n"

            now = asyncio.get_event_loop().time()
            if now - last_event_time >= _SSE_KEEPALIVE_INTERVAL_SECONDS:
                yield ": keepalive\n\n"
                last_event_time = now

            await asyncio.sleep(_STATS_UPDATE_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        pass


@router.get("/system/stats")
async def system_stats() -> dict[str, Any]:
    """Return one-shot system stats snapshot."""
    return await asyncio.to_thread(collect_jetson_stats)


@router.get("/system/stats-stream")
async def system_stats_stream(request: Request) -> StreamingResponse:
    """Stream system stats updates as Server-Sent Events (stats-update, keep-alive)."""
    proxy: DaemonProxy = request.app.state.daemon_proxy
    return StreamingResponse(
        _system_stats_stream_generator(proxy),
        media_type="text/event-stream",
    )
