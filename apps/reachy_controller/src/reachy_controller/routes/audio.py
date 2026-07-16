"""Real microphone spectrum Server-Sent Events."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from reachy_controller.shared_streams import AudioLevelsBroadcaster

router = APIRouter(prefix="/api", tags=["audio"])

_KEEPALIVE_INTERVAL_SECONDS = 15.0
_AUDIO_PAYLOAD_FIELDS = (
    "rms",
    "rms_db",
    "low",
    "mid",
    "high",
    "spectrum",
    "sample_rate_hz",
    "source",
    "timestamp",
)


async def _audio_level_stream_generator(
    broadcaster: AudioLevelsBroadcaster,
) -> AsyncGenerator[str, None]:
    subscription = await broadcaster.subscribe()
    last_keepalive = asyncio.get_running_loop().time()
    try:
        while True:
            remaining = _KEEPALIVE_INTERVAL_SECONDS - (
                asyncio.get_running_loop().time() - last_keepalive
            )
            try:
                levels: Any = await asyncio.wait_for(
                    subscription.__anext__(), timeout=max(remaining, 0.0)
                )
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                last_keepalive = asyncio.get_running_loop().time()
                continue
            except StopAsyncIteration:
                return

            if not isinstance(levels, dict) or "error" in levels:
                yield "event: daemon-unavailable\ndata: {}\n\n"
                continue
            payload = {field: levels[field] for field in _AUDIO_PAYLOAD_FIELDS}
            yield f"event: audio-level\ndata: {json.dumps(payload)}\n\n"
    except asyncio.CancelledError:
        return
    finally:
        await subscription.aclose()


@router.get("/audio/levels-stream")
async def audio_levels_stream(request: Request) -> StreamingResponse:
    broadcaster: AudioLevelsBroadcaster = request.app.state.audio_levels_broadcaster
    return StreamingResponse(
        _audio_level_stream_generator(broadcaster),
        media_type="text/event-stream",
    )

