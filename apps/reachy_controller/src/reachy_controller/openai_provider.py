"""Console-owned OpenAI Realtime provider capability.

The Console is the only component allowed to load the durable OpenAI API key.
Apps receive a short-lived Realtime client secret after presenting their
per-launch Console capability; they never receive the durable provider key.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

_SERVICE_NAME = "reachy-console.openai-provider"
_ACCOUNT_NAME = "openai-api-key"
_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
_OS_KEYRING_MODULE_PREFIXES = (
    "keyring.backends.macos",
    "keyring.backends.secretservice",
    "keyring.backends.windows",
    "keyring.backends.kwallet",
    "keyring.backends.libsecret",
)


class OpenAIProviderError(RuntimeError):
    """A safe, non-secret-bearing provider error."""


def _keyring() -> Any:
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError as exc:
        raise OpenAIProviderError("host credential store is unavailable") from exc
    try:
        backend = keyring.get_keyring()
        if float(getattr(backend, "priority", 0)) <= 0 or not _is_os_keyring_backend(backend):
            raise OpenAIProviderError("host credential store is unavailable")
    except KeyringError as exc:
        raise OpenAIProviderError("host credential store is unavailable") from exc
    return keyring


def _is_os_keyring_backend(backend: Any) -> bool:
    """Return whether ``backend`` is an approved OS-backed credential store.

    Keyring's priority only identifies a usable backend.  Console deliberately
    uses a small allowlist of native keychain, Secret Service, and KWallet
    modules instead of trusting arbitrary positive-priority backends.
    """
    backend_type = type(backend)
    module = (backend_type.__module__ or "").lower()

    # A ChainerBackend is acceptable only when every fallback remains an
    # approved OS credential store.
    if module.startswith("keyring.backends.chainer"):
        try:
            backends = tuple(backend.backends)
        except (AttributeError, TypeError):
            return False
        return bool(backends) and all(_is_os_keyring_backend(item) for item in backends)
    return module.startswith(_OS_KEYRING_MODULE_PREFIXES)


class OpenAIRealtimeProvider:
    """Own one durable host secret and mint constrained client capabilities."""

    def __init__(self, *, client_secrets_url: str = _CLIENT_SECRETS_URL) -> None:
        self._client_secrets_url = client_secrets_url

    def _load_key(self) -> str | None:
        try:
            value = _keyring().get_password(_SERVICE_NAME, _ACCOUNT_NAME)
        except OpenAIProviderError:
            value = None
        except Exception:
            value = None
        if isinstance(value, str) and value.strip():
            return value.strip()
        # Support an existing service-managed credential without writing it to
        # project config or copying it into child-app environments.
        service_value = (os.getenv("OPENAI_API_KEY") or "").strip()
        return service_value or None

    def is_configured(self) -> bool:
        """Return only presence, never the key or its source."""
        return self._load_key() is not None

    async def _mint(self, api_key: str, model_name: str) -> str:
        payload = {"session": {"type": "realtime", "model": model_name}}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    self._client_secrets_url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise OpenAIProviderError("OpenAI Realtime validation failed") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise OpenAIProviderError("OpenAI Realtime validation failed")
        try:
            data = response.json()
        except ValueError as exc:
            raise OpenAIProviderError("OpenAI Realtime validation failed") from exc
        value = data.get("value") if isinstance(data, dict) else None
        if not isinstance(value, str) and isinstance(data, dict):
            secret = data.get("client_secret")
            value = secret.get("value") if isinstance(secret, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise OpenAIProviderError("OpenAI Realtime validation failed")
        return value.strip()

    async def configure(self, api_key: str, model_name: str) -> None:
        """Validate first, then persist only to the host credential store."""
        normalized = (api_key or "").strip()
        if not normalized:
            raise OpenAIProviderError("OpenAI API key is required")
        await self._mint(normalized, model_name)
        try:
            _keyring().set_password(_SERVICE_NAME, _ACCOUNT_NAME, normalized)
        except OpenAIProviderError:
            raise
        except Exception as exc:
            raise OpenAIProviderError("host credential store rejected the credential") from exc

    async def issue_client_secret(self, model_name: str) -> str:
        """Mint one short-lived capability for an authenticated local app."""
        api_key = self._load_key()
        if api_key is None:
            raise OpenAIProviderError("OpenAI Realtime setup is required")
        return await self._mint(api_key, model_name)

    def remove(self) -> None:
        """Remove a stored key, refusing to misreport an injected key as removed."""
        try:
            _keyring().delete_password(_SERVICE_NAME, _ACCOUNT_NAME)
        except OpenAIProviderError:
            raise
        except Exception as exc:
            if type(exc).__name__ != "PasswordDeleteError":
                raise OpenAIProviderError(
                    "host credential store could not remove the credential"
                ) from exc
        if (os.getenv("OPENAI_API_KEY") or "").strip():
            raise OpenAIProviderError("service-managed OpenAI credential remains configured")
