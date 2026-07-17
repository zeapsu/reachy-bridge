"""Native controller settings providers backed by loopback app APIs."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, field_validator

DEFAULT_REACHY_CHAT_SETTINGS_URL = "http://127.0.0.1:7860"
SETTINGS_UNAVAILABLE_DETAIL = "Reachy Chat settings service unavailable"
INVALID_UPSTREAM_DETAIL = "Reachy Chat settings service returned an invalid response"


class SettingValuePayload(BaseModel):
    """Payload accepted by every generic settings mutation endpoint."""

    value: str

    @field_validator("value")
    @classmethod
    def require_non_empty_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be empty")
        return normalized


class SettingsUnavailableError(Exception):
    """Raised when a loopback settings service cannot be reached."""


class UpstreamHTTPError(Exception):
    """Raised when a loopback settings service returns a non-success status."""

    def __init__(self, response: httpx.Response, *, contains_secret: bool = False) -> None:
        super().__init__(f"settings service returned HTTP {response.status_code}")
        self.response = response
        self.contains_secret = contains_secret


class InvalidUpstreamResponseError(Exception):
    """Raised when a successful upstream response is not valid JSON."""


class AppSettingsProxy:
    """Own one async client constrained to the loopback settings service."""

    def __init__(
        self,
        base_url: str = DEFAULT_REACHY_CHAT_SETTINGS_URL,
        *,
        timeout: float = 12.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        """Create the shared client once, normally from the controller lifespan."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self._transport,
            )

    async def close(self) -> None:
        """Close the shared client during controller shutdown."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        """Make one request without allowing callers to escape the configured base URL."""
        await self.start()
        assert self._client is not None
        try:
            return await self._client.request(method, path, json=json)
        except httpx.TransportError as exc:
            raise SettingsUnavailableError from exc


class SettingsProvider(Protocol):
    """Provider contract used by the generic settings router."""

    async def get_schema(self) -> dict[str, Any]:
        """Return the native settings field schema."""
        ...

    async def set_value(self, key: str, value: str) -> Any:
        """Apply one native settings field."""
        ...


class AppSettingsRegistry:
    """Registry mapping managed app names to optional settings providers."""

    def __init__(self, providers: Mapping[str, SettingsProvider] | None = None) -> None:
        self._providers = dict(providers or {})

    def provider_for(self, app_name: str) -> SettingsProvider | None:
        return self._providers.get(app_name)

    def supports(self, app_name: str) -> bool:
        """Return whether an app has a native settings provider."""
        return app_name in self._providers


class ReachyChatSettingsProvider:
    """Expose Reachy Chat's settings API as a controller-native schema."""

    app_name = "reachy_chat"
    title = "Reachy Chat"

    def __init__(self, proxy: AppSettingsProxy) -> None:
        self.proxy = proxy

    async def _json_request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        contains_secret: bool = False,
    ) -> Any:
        response = await self.proxy.request(method, path, json=json)
        if not 200 <= response.status_code < 300:
            raise UpstreamHTTPError(response, contains_secret=contains_secret)
        try:
            return response.json()
        except ValueError as exc:
            raise InvalidUpstreamResponseError from exc

    @staticmethod
    def _credential_definition(status: Mapping[str, Any]) -> tuple[str, str] | None:
        provider = str(status.get("backend_provider") or "").strip().lower()
        if provider == "openai_realtime":
            return "OpenAI API key", "has_openai_realtime_key"
        if provider == "openai":
            return "OpenRouter API key", "has_openrouter_key"
        if provider == "gemini":
            return "Gemini API key", "has_gemini_key"
        return None

    @staticmethod
    def _backend_provider(status: Mapping[str, Any]) -> str:
        provider = str(status.get("backend_provider") or "").strip().lower()
        if not provider:
            raise InvalidUpstreamResponseError
        return provider

    async def get_schema(self) -> dict[str, Any]:
        status, voices, current = await asyncio.gather(
            self._json_request("GET", "/status"),
            self._json_request("GET", "/voices"),
            self._json_request("GET", "/voices/current"),
        )

        if (
            not isinstance(status, dict)
            or not isinstance(voices, list)
            or not isinstance(current, dict)
        ):
            raise InvalidUpstreamResponseError
        if not all(isinstance(voice, str) and voice.strip() for voice in voices):
            raise InvalidUpstreamResponseError
        current_voice = current.get("voice")
        if not isinstance(current_voice, str) or not current_voice.strip():
            raise InvalidUpstreamResponseError

        fields: list[dict[str, Any]] = []
        credential = self._credential_definition(status)
        if credential is not None:
            label, configured_key = credential
            fields.append(
                {
                    "key": "api_key",
                    "label": label,
                    "type": "password",
                    "configured": bool(status.get(configured_key, False)),
                }
            )
        fields.append(
            {
                "key": "voice",
                "label": "Voice",
                "type": "select",
                "value": current_voice.strip(),
                "options": voices,
            }
        )
        return {"app": self.app_name, "title": self.title, "fields": fields}

    async def set_value(self, key: str, value: str) -> Any:
        if key == "api_key":
            status = await self._json_request("GET", "/status")
            if not isinstance(status, dict):
                raise InvalidUpstreamResponseError
            provider = self._backend_provider(status)
            if self._credential_definition(status) is None:
                raise KeyError(key)
            response = await self._json_request(
                "POST",
                "/backend_config",
                json={"backend": provider, "api_key": value},
                contains_secret=True,
            )
            return _sanitize_payload(response)

        if key == "voice":
            response = await self._json_request(
                "POST",
                "/voices/apply",
                json={"voice": value},
            )
            return _sanitize_payload(response)

        raise KeyError(key)


router = APIRouter(prefix="/api", tags=["app-settings"])


def _sanitize_payload(payload: Any) -> Any:
    """Remove secret-bearing fields from an upstream JSON payload."""
    if isinstance(payload, dict):
        return {
            key: _sanitize_payload(value)
            for key, value in payload.items()
            if not any(token in str(key).lower() for token in ("key", "token", "secret"))
        }
    if isinstance(payload, list):
        return [_sanitize_payload(item) for item in payload]
    return payload


def _upstream_error_response(error: UpstreamHTTPError) -> Response:
    """Preserve upstream status while keeping credential requests opaque."""
    if error.contains_secret:
        return JSONResponse(
            {"detail": "Reachy Chat settings request failed"},
            status_code=error.response.status_code,
        )
    try:
        payload = _sanitize_payload(error.response.json())
    except ValueError:
        return JSONResponse(
            {"detail": "Reachy Chat settings request failed"},
            status_code=error.response.status_code,
        )
    return JSONResponse(payload, status_code=error.response.status_code)


def _provider_or_404(request: Request, app_name: str) -> SettingsProvider:
    registry: AppSettingsRegistry = request.app.state.app_settings
    provider = registry.provider_for(app_name)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"No settings provider for app: {app_name}")
    return provider


@router.get("/apps/{app_name}/settings", response_model=None)
async def get_app_settings(app_name: str, request: Request) -> dict[str, Any] | Response:
    provider = _provider_or_404(request, app_name)
    try:
        return await provider.get_schema()
    except SettingsUnavailableError:
        return JSONResponse({"detail": SETTINGS_UNAVAILABLE_DETAIL}, status_code=503)
    except UpstreamHTTPError as error:
        return _upstream_error_response(error)
    except InvalidUpstreamResponseError:
        return JSONResponse({"detail": INVALID_UPSTREAM_DETAIL}, status_code=502)


@router.post("/apps/{app_name}/settings/{key}")
async def set_app_setting(
    app_name: str,
    key: str,
    payload: SettingValuePayload,
    request: Request,
) -> Any:
    provider = _provider_or_404(request, app_name)
    try:
        return await provider.set_value(key, payload.value)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}") from None
    except SettingsUnavailableError:
        return JSONResponse({"detail": SETTINGS_UNAVAILABLE_DETAIL}, status_code=503)
    except UpstreamHTTPError as error:
        return _upstream_error_response(error)
    except InvalidUpstreamResponseError:
        return JSONResponse({"detail": INVALID_UPSTREAM_DETAIL}, status_code=502)
