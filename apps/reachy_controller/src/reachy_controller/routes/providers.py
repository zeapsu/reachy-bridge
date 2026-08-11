"""Local application access to Console-owned provider capabilities."""

from __future__ import annotations

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from reachy_controller.openai_provider import OpenAIProviderError, OpenAIRealtimeProvider

router = APIRouter(prefix="/api/providers", tags=["providers"])


class RealtimeSessionRequest(BaseModel):
    """Requested Realtime model for one short-lived client capability."""

    model: str

    @field_validator("model")
    @classmethod
    def model_is_present(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("model is required")
        return normalized


@router.get("/openai-realtime/status")
async def openai_realtime_status(request: Request) -> dict[str, bool]:
    """Expose setup state only; this response contains no credential material."""
    provider: OpenAIRealtimeProvider = request.app.state.openai_realtime_provider
    return {"configured": provider.is_configured()}


@router.post("/openai-realtime/client-secret", response_model=None)
async def openai_realtime_client_secret(
    payload: RealtimeSessionRequest,
    request: Request,
    x_reachy_console_capability: str | None = Header(default=None),
) -> object:
    """Mint a short-lived Realtime token for a Console-launched local app."""
    accepts_capability = request.app.state.app_supervisor.accepts_provider_capability
    if not accepts_capability(x_reachy_console_capability):
        return JSONResponse({"detail": "provider capability unavailable"}, status_code=403)
    provider: OpenAIRealtimeProvider = request.app.state.openai_realtime_provider
    try:
        client_secret = await provider.issue_client_secret(payload.model)
    except OpenAIProviderError:
        return JSONResponse({"detail": "OpenAI Realtime setup is required"}, status_code=503)
    return {"client_secret": client_secret}
