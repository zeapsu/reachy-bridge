"""Reachy Chat client for the local Console provider capability."""

from __future__ import annotations
import os

import httpx


_DEFAULT_CONSOLE_URL = "http://127.0.0.1:7861"


class ConsoleProviderUnavailable(RuntimeError):
    """The Console has no usable OpenAI Realtime capability for this app."""


def _console_url() -> str:
    return (os.getenv("REACHY_CONSOLE_PROVIDER_URL") or _DEFAULT_CONSOLE_URL).rstrip("/")


def openai_realtime_is_configured() -> bool:
    """Return Console setup status without requesting credential material."""
    try:
        response = httpx.get(f"{_console_url()}/api/providers/openai-realtime/status", timeout=0.5)
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return False
    return response.is_success and isinstance(data, dict) and data.get("configured") is True


async def request_openai_realtime_client_secret(model_name: str) -> str:
    """Request a short-lived Realtime token using this launch's capability."""
    capability = (os.getenv("REACHY_CONSOLE_PROVIDER_CAPABILITY") or "").strip()
    if not capability:
        raise ConsoleProviderUnavailable("Console provider capability unavailable")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{_console_url()}/api/providers/openai-realtime/client-secret",
                headers={"X-Reachy-Console-Capability": capability},
                json={"model": model_name},
            )
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ConsoleProviderUnavailable("Console provider unavailable") from exc
    value = data.get("client_secret") if response.is_success and isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise ConsoleProviderUnavailable("Console provider unavailable")
    return value.strip()
