"""System statistics streaming (CPU, GPU, memory, temperature, audio routing)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from reachy_controller.jetson_stats import collect_jetson_stats

router = APIRouter(prefix="/api", tags=["system"])

_SSE_KEEPALIVE_INTERVAL_SECONDS = 15.0
_STATS_UPDATE_INTERVAL_SECONDS = 1.5


async def _system_stats_stream_generator() -> AsyncGenerator[str, None]:
    """Generate Server-Sent Events for system stats updates.

    Emits stats-update events every 1-2 seconds.
    Emits keep-alive comments every ~15s to prevent proxy timeouts.
    """
    last_event_time = asyncio.get_event_loop().time()

    try:
        while True:
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
async def system_stats_stream() -> StreamingResponse:
    """Stream system stats updates as Server-Sent Events (stats-update, keep-alive)."""
    return StreamingResponse(
        _system_stats_stream_generator(),
        media_type="text/event-stream",
    )
