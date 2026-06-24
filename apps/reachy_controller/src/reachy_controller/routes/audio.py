"""Real-signal audio level Server-Sent Events."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from reachy_controller.daemon_proxy import DaemonProxy

router = APIRouter(prefix="/api", tags=["audio"])

_POLL_INTERVAL_SECONDS = 0.05
_KEEPALIVE_INTERVAL_SECONDS = 15.0


async def _audio_level_stream_generator(proxy: DaemonProxy) -> AsyncGenerator[str, None]:
    last_keepalive = asyncio.get_running_loop().time()
    try:
        while True:
            state = await proxy.get_full_state()
            if state is None:
                yield "event: daemon-unavailable\ndata: {}\n\n"
            else:
                doa = state.get("doa")
                speech_detected = isinstance(doa, dict) and doa.get("speech_detected") is True
                payload = {
                    "level": 0.7 if speech_detected else 0.0,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                yield f"event: audio-level\ndata: {json.dumps(payload)}\n\n"

            now = asyncio.get_running_loop().time()
            if now - last_keepalive >= _KEEPALIVE_INTERVAL_SECONDS:
                yield ": keepalive\n\n"
                last_keepalive = now

            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        pass


@router.get("/audio/levels-stream")
async def audio_levels_stream(request: Request) -> StreamingResponse:
    proxy: DaemonProxy = request.app.state.daemon_proxy
    return StreamingResponse(
        _audio_level_stream_generator(proxy),
        media_type="text/event-stream",
    )
