"""Aggregated Reachy and Jetson telemetry endpoint."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from reachy_controller.daemon_proxy import DaemonProxy
from reachy_controller.jetson_stats import collect_host_metrics

router = APIRouter(prefix="/api", tags=["telemetry"])


async def _safe(coro: Any) -> Any | None:
    try:
        return await coro
    except (httpx.HTTPError, ValueError, TypeError):
        return None


@router.get("/telemetry")
async def telemetry(request: Request) -> dict[str, Any]:
    """Return one resilient snapshot for the dashboard's polling loop."""
    proxy: DaemonProxy = request.app.state.daemon_proxy
    daemon, state, app, motors, speaker, microphone = await asyncio.gather(
        _safe(proxy.get_json("daemon/status")),
        _safe(proxy.get_json("state/full", params={"with_doa": "true"})),
        _safe(proxy.get_json("apps/current-app-status")),
        _safe(proxy.get_json("motors/status")),
        _safe(proxy.get_json("volume/current")),
        _safe(proxy.get_json("volume/microphone/current")),
    )
    host = await asyncio.to_thread(collect_host_metrics)

    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "daemon": {"online": daemon is not None, "status": daemon},
        "robot": {
            "state": state,
            "motor_mode": motors.get("mode") if isinstance(motors, dict) else None,
        },
        "app": app,
        "audio": {
            "speaker_volume": speaker.get("volume") if isinstance(speaker, dict) else None,
            "microphone_volume": microphone.get("volume") if isinstance(microphone, dict) else None,
        },
        "host": host,
    }


@router.get("/telemetry/state", response_model=None)
async def telemetry_state(request: Request) -> dict[str, Any] | JSONResponse:
    """Return one-shot full robot state from daemon."""
    proxy: DaemonProxy = request.app.state.daemon_proxy
    result = await proxy.get_full_state()
    if result is None:
        return JSONResponse({"detail": "Daemon unavailable"}, status_code=503)
    return result


@router.get("/telemetry/daemon-status", response_model=None)
async def telemetry_daemon_status(request: Request) -> dict[str, Any] | JSONResponse:
    """Return one-shot daemon status from daemon."""
    proxy: DaemonProxy = request.app.state.daemon_proxy
    result = await proxy.get_daemon_status()
    if result is None:
        return JSONResponse({"detail": "Daemon unavailable"}, status_code=503)
    return result


_SSE_KEEPALIVE_INTERVAL_SECONDS = 15.0


async def _daemon_state_stream_generator(proxy: DaemonProxy) -> AsyncGenerator[str, None]:
    """Generate Server-Sent Events for robot state updates.

    Streams state from daemon via WebSocket.
    Emits daemon-offline when daemon is unavailable.
    Emits keep-alive comments every ~15s.
    """
    last_event_time = asyncio.get_event_loop().time()

    try:
        async for state_dict in proxy.ws_stream_full_state():
            if "error" in state_dict:
                # Daemon unavailable
                yield "event: daemon-offline\ndata: {}\n\n"
            else:
                # Valid state update
                yield f"event: state-update\ndata: {json.dumps(state_dict)}\n\n"

            # Emit keepalive periodically
            now = asyncio.get_event_loop().time()
            if now - last_event_time >= _SSE_KEEPALIVE_INTERVAL_SECONDS:
                yield ": keepalive\n\n"
                last_event_time = now
    except asyncio.CancelledError:
        # Client disconnected
        pass


# Root endpoint without /api prefix
root_router = APIRouter(tags=["telemetry"])


@root_router.get("/telemetry/state-stream")
async def telemetry_state_stream(request: Request) -> StreamingResponse:
    """Stream robot state updates as Server-Sent Events.

    Emits state-update events with robot state from daemon.
    Emits daemon-offline events when daemon is unreachable.
    Emits keep-alive comments every ~15s to prevent proxy timeouts.

    Returns:
        StreamingResponse with media_type="text/event-stream"
    """
    proxy: DaemonProxy = request.app.state.daemon_proxy
    return StreamingResponse(
        _daemon_state_stream_generator(proxy),
        media_type="text/event-stream",
    )
