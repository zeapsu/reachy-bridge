"""Real-signal audio level Server-Sent Events."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from reachy_controller.shared_streams import StateBroadcaster

router = APIRouter(prefix="/api", tags=["audio"])

_POLL_INTERVAL_SECONDS = 0.10
_KEEPALIVE_INTERVAL_SECONDS = 15.0


async def _audio_level_stream_generator(
    broadcaster: StateBroadcaster,
) -> AsyncGenerator[str, None]:
    subscription = await broadcaster.subscribe()
    last_keepalive = asyncio.get_running_loop().time()
    try:
        while True:
            remaining = _KEEPALIVE_INTERVAL_SECONDS - (
                asyncio.get_running_loop().time() - last_keepalive
            )
            try:
                state = await asyncio.wait_for(
                    subscription.__anext__(), timeout=max(remaining, 0.0)
                )
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                last_keepalive = asyncio.get_running_loop().time()
                continue
            except StopAsyncIteration:
                return

            if isinstance(state, dict) and "error" in state:
                yield "event: daemon-unavailable\ndata: {}\n\n"
            else:
                doa = state.get("doa") if isinstance(state, dict) else None
                speech_detected = isinstance(doa, dict) and doa.get("speech_detected") is True
                payload = {
                    "speech_detected": speech_detected,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                yield f"event: audio-level\ndata: {json.dumps(payload)}\n\n"
    except asyncio.CancelledError:
        return
    finally:
        await subscription.aclose()


@router.get("/audio/levels-stream")
async def audio_levels_stream(request: Request) -> StreamingResponse:
    broadcaster: StateBroadcaster = request.app.state.state_broadcaster
    return StreamingResponse(
        _audio_level_stream_generator(broadcaster),
        media_type="text/event-stream",
    )

