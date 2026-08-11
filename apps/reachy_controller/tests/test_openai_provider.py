from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from reachy_controller.openai_provider import OpenAIProviderError, OpenAIRealtimeProvider, _keyring


@pytest.mark.asyncio
async def test_configure_validates_before_writing_to_host_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored: list[tuple[str, str, str]] = []
    keyring = MagicMock()
    def store(service: str, account: str, value: str) -> None:
        stored.append((service, account, value))

    keyring.set_password.side_effect = store
    monkeypatch.setattr("reachy_controller.openai_provider._keyring", lambda: keyring)

    provider = OpenAIRealtimeProvider()
    mint = AsyncMock(return_value="ephemeral-test-token")
    monkeypatch.setattr(provider, "_mint", mint)

    await provider.configure("test-credential", "gpt-realtime")

    assert len(stored) == 1
    assert stored[0][2] == "test-credential"
    mint.assert_awaited_once_with("test-credential", "gpt-realtime")


@pytest.mark.asyncio
async def test_configure_does_not_write_when_validation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid key never reaches the host store."""
    keyring = MagicMock()
    monkeypatch.setattr("reachy_controller.openai_provider._keyring", lambda: keyring)
    provider = OpenAIRealtimeProvider()
    monkeypatch.setattr(
        provider,
        "_mint",
        AsyncMock(side_effect=OpenAIProviderError("OpenAI Realtime validation failed")),
    )

    with pytest.raises(OpenAIProviderError, match="validation failed"):
        await provider.configure("test-credential", "gpt-realtime")

    keyring.set_password.assert_not_called()


def test_keyring_rejects_unknown_positive_priority_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A positive keyring priority is insufficient without an OS backend."""
    import keyring

    insecure_backend = type(
        "CustomKeyring",
        (),
        {"__module__": "custom_keyring", "priority": 1.0},
    )()
    monkeypatch.setattr(keyring, "get_keyring", lambda: insecure_backend)

    with pytest.raises(OpenAIProviderError, match="host credential store is unavailable"):
        _keyring()


def test_keyring_accepts_supported_os_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring

    os_backend = type(
        "Keyring",
        (),
        {"__module__": "keyring.backends.SecretService", "priority": 1.0},
    )()
    monkeypatch.setattr(keyring, "get_keyring", lambda: os_backend)

    assert _keyring() is keyring


def test_remove_is_idempotent_when_keyring_has_no_stored_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing an already-absent stored credential is a successful no-op."""
    password_delete_error = type("PasswordDeleteError", (Exception,), {})
    keyring = MagicMock()
    keyring.delete_password.side_effect = password_delete_error()
    monkeypatch.setattr("reachy_controller.openai_provider._keyring", lambda: keyring)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    OpenAIRealtimeProvider().remove()

    keyring.delete_password.assert_called_once()


def test_remove_reports_host_store_failure_without_secret_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keyring = MagicMock()
    keyring.delete_password.side_effect = RuntimeError("store failure")
    monkeypatch.setattr("reachy_controller.openai_provider._keyring", lambda: keyring)

    with pytest.raises(OpenAIProviderError, match="could not remove"):
        OpenAIRealtimeProvider().remove()


def test_remove_reports_when_service_managed_key_remains_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keyring = MagicMock()
    monkeypatch.setattr("reachy_controller.openai_provider._keyring", lambda: keyring)
    monkeypatch.setenv("OPENAI_API_KEY", "service-managed-test-credential")

    with pytest.raises(
        OpenAIProviderError,
        match="service-managed OpenAI credential remains configured",
    ):
        OpenAIRealtimeProvider().remove()

    keyring.delete_password.assert_called_once()
