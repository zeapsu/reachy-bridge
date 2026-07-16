"""System statistics streaming (CPU, GPU, memory, temperature, audio routing)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from reachy_controller.jetson_stats import collect_jetson_stats
from reachy_controller.shared_streams import StatsBroadcaster

router = APIRouter(prefix="/api", tags=["system"])

_SSE_KEEPALIVE_INTERVAL_SECONDS = 15.0
_STATS_UPDATE_INTERVAL_SECONDS = 1.5


async def _system_stats_stream_generator(
    broadcaster: StatsBroadcaster,
) -> AsyncGenerator[str, None]:
    """Adapt shared stats samples to the existing SSE contract."""
    subscription = await broadcaster.subscribe()
    last_event_time = asyncio.get_running_loop().time()
    try:
        while True:
            remaining = _SSE_KEEPALIVE_INTERVAL_SECONDS - (
                asyncio.get_running_loop().time() - last_event_time
            )
            try:
                stats = await asyncio.wait_for(
                    subscription.__anext__(), timeout=max(remaining, 0.0)
                )
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                last_event_time = asyncio.get_running_loop().time()
                continue
            except StopAsyncIteration:
                return

            if stats is not None:
                yield f"event: stats-update\ndata: {json.dumps(stats)}\n\n"
    except asyncio.CancelledError:
        return
    finally:
        await subscription.aclose()


@router.get("/system/stats")
async def system_stats() -> dict[str, Any]:
    """Return one-shot system stats snapshot."""
    return await asyncio.to_thread(collect_jetson_stats)

@router.get("/system/stats-stream")
async def system_stats_stream(request: Request) -> StreamingResponse:
    """Stream system stats updates as Server-Sent Events."""
    broadcaster: StatsBroadcaster = request.app.state.stats_broadcaster
    return StreamingResponse(
        _system_stats_stream_generator(broadcaster),
        media_type="text/event-stream",
    )
