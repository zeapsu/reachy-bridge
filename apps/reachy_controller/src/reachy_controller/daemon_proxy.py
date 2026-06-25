"""HTTP client and transparent API proxy for the local Reachy daemon."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, Mapping
from typing import Any

import httpx
import websockets
from fastapi import APIRouter, Request
from fastapi.responses import Response
from websockets.exceptions import WebSocketException


class DaemonProxy:
    """Own an async client whose requests are constrained to the daemon API."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 2.0,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self._transport,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        await self.start()
        assert self._client is not None
        daemon_path = "/api/" + path.removeprefix("/api/").lstrip("/")
        return await self._client.request(
            method,
            daemon_path,
            params=params,
            content=content,
            headers=headers,
        )

    async def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        response = await self.request("GET", path, params=params)
        response.raise_for_status()
        return response.json()

    async def get_full_state(self) -> dict[str, Any] | None:
        """Get full robot state from daemon via GET /api/state/full."""
        try:
            return await self.get_json("state/full", params={"with_doa": "true"})
        except (httpx.HTTPError, ValueError):
            return None

    async def get_daemon_status(self) -> dict[str, Any] | None:
        """Get daemon status from daemon via GET /api/daemon/status."""
        try:
            return await self.get_json("daemon/status")
        except (httpx.HTTPError, ValueError):
            return None

    async def get_robot_app_lock_status(self) -> dict[str, Any] | None:
        """Get robot app lock status from daemon via GET /api/daemon/robot-app-lock-status."""
        try:
            return await self.get_json("daemon/robot-app-lock-status")
        except (httpx.HTTPError, ValueError):
            return None

    async def _post_json(
        self,
        path: str,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        content = json.dumps(body).encode() if body is not None else None
        headers = {"content-type": "application/json"} if body is not None else None
        try:
            response = await self.request("POST", path, content=content, headers=headers)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            return None

    async def get_speaker_volume(self) -> dict[str, Any] | None:
        try:
            return await self.get_json("volume/current")
        except (httpx.HTTPError, ValueError):
            return None

    async def set_speaker_volume(self, volume: int) -> dict[str, Any] | None:
        return await self._post_json("volume/set", {"volume": volume})

    async def get_microphone_volume(self) -> dict[str, Any] | None:
        try:
            return await self.get_json("volume/microphone/current")
        except (httpx.HTTPError, ValueError):
            return None

    async def set_microphone_volume(self, volume: int) -> dict[str, Any] | None:
        return await self._post_json("volume/microphone/set", {"volume": volume})

    async def get_motor_status(self) -> dict[str, Any] | None:
        try:
            return await self.get_json("motors/status")
        except (httpx.HTTPError, ValueError):
            return None

    async def set_motor_mode(self, mode: str) -> dict[str, Any] | None:
        return await self._post_json(f"motors/set_mode/{mode}")

    async def play_move(self, name: str) -> dict[str, Any] | None:
        return await self._post_json(f"move/play/{name}")

    async def get_move_running(self) -> dict[str, Any] | None:
        try:
            return await self.get_json("move/running")
        except (httpx.HTTPError, ValueError):
            return None

    async def enable_wobbling(self) -> dict[str, Any] | None:
        return await self._post_json("media/wobbling/enable")

    async def disable_wobbling(self) -> dict[str, Any] | None:
        return await self._post_json("media/wobbling/disable")

    async def play_sound(self, file: str) -> dict[str, Any] | None:
        return await self._post_json("media/play_sound", {"file": file})

    async def play_test_sound(self) -> dict[str, Any] | None:
        return await self._post_json("volume/test-sound")

    async def stop_sound(self) -> dict[str, Any] | None:
        return await self._post_json("media/stop_sound")

    async def release_media(self) -> dict[str, Any] | None:
        return await self._post_json("media/release")

    async def acquire_media(self) -> dict[str, Any] | None:
        return await self._post_json("media/acquire")

    async def get_media_status(self) -> dict[str, Any] | None:
        try:
            return await self.get_json("media/status")
        except (httpx.HTTPError, ValueError):
            return None

    async def ws_stream_full_state(self) -> AsyncGenerator[dict[str, Any], None]:
        """Stream full robot state from daemon via WebSocket.

        Yields parsed JSON dicts from ws://daemon/api/state/ws/full.
        On connection failure, yields {"error": "daemon_unavailable"} once.
        Yields each subsequent message from the daemon as a parsed dict.
        """
        # Convert http:// to ws://
        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/api/state/ws/full?with_doa=true"

        try:
            async with websockets.connect(ws_url) as websocket:
                async for message in websocket:
                    with contextlib.suppress(json.JSONDecodeError):
                        yield json.loads(message)
        except (ConnectionRefusedError, WebSocketException, OSError, TimeoutError):
            yield {"error": "daemon_unavailable"}
        except asyncio.CancelledError:
            # Client disconnected gracefully
            pass


router = APIRouter(prefix="/api")


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def proxy_daemon_request(path: str, request: Request) -> Response:
    """Forward controller API calls to the same path on the local daemon."""
    proxy: DaemonProxy = request.app.state.daemon_proxy
    body = await request.body()
    forwarded_headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() in {"accept", "content-type"}
    }
    try:
        upstream = await proxy.request(
            request.method,
            path,
            params=dict(request.query_params),
            content=body,
            headers=forwarded_headers,
        )
    except httpx.RequestError as exc:
        return Response(
            content=f'{{"detail":"Reachy daemon unavailable: {type(exc).__name__}"}}',
            status_code=503,
            media_type="application/json",
        )

    response_headers: dict[str, str] = {}
    if content_type := upstream.headers.get("content-type"):
        response_headers["content-type"] = content_type
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
    )
