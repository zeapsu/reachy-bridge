"""Validated controller-owned routes for Reachy controls."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from reachy_controller.daemon_proxy import DaemonProxy

router = APIRouter(prefix="/api", tags=["controls"])

VALID_MOTOR_MODES = {"enabled", "disabled", "gravity_compensation"}


class VolumeSetRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    volume: int = Field(ge=0, le=100, description="Volume level from 0 through 100")


class PlaySoundRequest(BaseModel):
    file: str = Field(description="Sound filename or daemon-local path")


def _result_or_unavailable(result: dict[str, Any] | None) -> dict[str, Any]:
    if result is None:
        raise HTTPException(status_code=503, detail="Daemon unavailable")
    return result


def _validate_volume(volume: int) -> None:
    if not 0 <= volume <= 100:
        raise HTTPException(status_code=400, detail="Volume must be between 0 and 100")


@router.get("/volume/current")
async def get_speaker_volume(request: Request) -> dict[str, Any]:
    proxy: DaemonProxy = request.app.state.daemon_proxy
    return _result_or_unavailable(await proxy.get_speaker_volume())


@router.post("/volume/set")
async def set_speaker_volume(body: VolumeSetRequest, request: Request) -> dict[str, Any]:
    _validate_volume(body.volume)
    proxy: DaemonProxy = request.app.state.daemon_proxy
    return _result_or_unavailable(await proxy.set_speaker_volume(body.volume))


@router.get("/volume/microphone/current")
async def get_microphone_volume(request: Request) -> dict[str, Any]:
    proxy: DaemonProxy = request.app.state.daemon_proxy
    return _result_or_unavailable(await proxy.get_microphone_volume())


@router.post("/volume/microphone/set")
async def set_microphone_volume(body: VolumeSetRequest, request: Request) -> dict[str, Any]:
    _validate_volume(body.volume)
    proxy: DaemonProxy = request.app.state.daemon_proxy
    return _result_or_unavailable(await proxy.set_microphone_volume(body.volume))


@router.get("/motors/status")
async def get_motor_status(request: Request) -> dict[str, Any]:
    proxy: DaemonProxy = request.app.state.daemon_proxy
    return _result_or_unavailable(await proxy.get_motor_status())


@router.post("/motors/set_mode/{mode}")
async def set_motor_mode(mode: str, request: Request) -> dict[str, Any]:
    if mode not in VALID_MOTOR_MODES:
        raise HTTPException(status_code=400, detail="Invalid motor mode")
    proxy: DaemonProxy = request.app.state.daemon_proxy
    return _result_or_unavailable(await proxy.set_motor_mode(mode))


@router.post("/media/wobbling/enable")
async def enable_wobbling(request: Request) -> dict[str, Any]:
    proxy: DaemonProxy = request.app.state.daemon_proxy
    return _result_or_unavailable(await proxy.enable_wobbling())


@router.post("/media/wobbling/disable")
async def disable_wobbling(request: Request) -> dict[str, Any]:
    proxy: DaemonProxy = request.app.state.daemon_proxy
    return _result_or_unavailable(await proxy.disable_wobbling())


@router.post("/media/play_sound")
async def play_sound(body: PlaySoundRequest, request: Request) -> dict[str, Any]:
    proxy: DaemonProxy = request.app.state.daemon_proxy
    return _result_or_unavailable(await proxy.play_sound(body.file))


@router.post("/volume/test-sound")
async def play_test_sound(request: Request) -> dict[str, Any]:
    proxy: DaemonProxy = request.app.state.daemon_proxy
    return _result_or_unavailable(await proxy.play_test_sound())


@router.post("/media/stop-sound")
async def stop_sound(request: Request) -> dict[str, Any]:
    proxy: DaemonProxy = request.app.state.daemon_proxy
    return _result_or_unavailable(await proxy.stop_sound())
