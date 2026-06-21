from __future__ import annotations
import sys
import asyncio
import subprocess
from types import SimpleNamespace
from typing import Any
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
import tomllib
from fastrtc import AdditionalOutputs

from reachy_chat import config as config_module
from reachy_chat.config import config
from reachy_chat.openrouter_chat import OpenRouterChatHandler
from reachy_chat.tools.core_tools import ToolDependencies
from reachy_chat.conversation_handler import ConversationHandler


PROJECT_ROOT = Path(__file__).parents[1]


def _build_handler() -> OpenRouterChatHandler:
    deps = ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock())
    return OpenRouterChatHandler(deps)


def test_discrete_dependencies_are_declared() -> None:
    """The package must install both local speech engines."""
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert "faster-whisper>=1.0.0" in project["dependencies"]
    assert "pyttsx3>=2.90" in project["dependencies"]


def test_handler_is_concrete_and_resources_are_lazy() -> None:
    """Construction must not initialize speech engines or an API client."""
    handler = _build_handler()

    assert isinstance(handler, ConversationHandler)
    assert handler._client is None
    assert handler._whisper_model is None
    assert handler._tts_engine is None
    assert "faster_whisper" not in sys.modules
    assert "pyttsx3" not in sys.modules


@pytest.mark.asyncio
async def test_startup_exposes_readiness_until_graceful_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """The discrete startup task must stay ready until shutdown releases it."""
    handler = _build_handler()
    client = SimpleNamespace(close=MagicMock())
    monkeypatch.setattr(handler, "_ensure_client", MagicMock(return_value=client))

    startup = asyncio.create_task(handler.start_up())
    await asyncio.sleep(0)

    assert handler.connection is client
    assert not startup.done()

    await handler.shutdown()
    await startup
    assert handler.connection is None


def test_client_uses_configured_openrouter_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The OpenAI-compatible client must target OpenRouter, not OpenAI."""
    handler = _build_handler()
    constructor = MagicMock(return_value=SimpleNamespace())
    monkeypatch.setattr("reachy_chat.openrouter_chat.AsyncOpenAI", constructor)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(config, "OPENROUTER_BASE_URL", "https://openrouter.test/api/v1")

    handler._ensure_client()

    constructor.assert_called_once_with(
        api_key="test-key",
        base_url="https://openrouter.test/api/v1",
    )


@pytest.mark.asyncio
async def test_shutdown_stops_initialized_tts_engine() -> None:
    """Graceful shutdown must stop the platform speech engine."""
    handler = _build_handler()
    engine = MagicMock()
    handler._tts_engine = engine

    await handler.shutdown()

    engine.stop.assert_called_once_with()
    assert handler._tts_engine is None


@pytest.mark.asyncio
async def test_chat_completion_uses_openrouter_model_and_bounded_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chat completions must use the configured model and last ten messages."""
    calls: list[dict[str, Any]] = []

    class FakeCompletions:
        async def create(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="A concise reply."))])

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    handler = _build_handler()
    handler._client = fake_client
    monkeypatch.setattr(config, "MODEL_NAME", "deepseek/deepseek-v4-flash")
    handler._system_prompt = "You are Reachy."

    for index in range(12):
        handler._record_message("user", f"message-{index}")

    reply = await handler._request_completion()

    assert reply == "A concise reply."
    assert calls == [
        {
            "model": "deepseek/deepseek-v4-flash",
            "messages": [
                {"role": "system", "content": "You are Reachy."},
                *[{"role": "user", "content": f"message-{index}"} for index in range(2, 12)],
            ],
        }
    ]


@pytest.mark.asyncio
async def test_receive_finalizes_a_turn_after_trailing_silence() -> None:
    """Trailing silence must finalize buffered SDK microphone samples."""
    handler = _build_handler()
    activities: list[str] = []
    handler.set_activity_observer(activities.append)
    handler.SILENCE_DURATION_S = 0.01
    handler.MIN_SPEECH_DURATION_S = 0.01
    processed = asyncio.Event()
    captured: list[np.ndarray[Any, np.dtype[np.float32]]] = []

    async def fake_process_turn(audio: np.ndarray[Any, np.dtype[np.float32]]) -> None:
        captured.append(audio)
        processed.set()

    handler._process_turn = fake_process_turn  # type: ignore[method-assign]

    await handler.receive((16_000, np.full(320, 12_000, dtype=np.int16)))
    await handler.receive((16_000, np.zeros(320, dtype=np.int16)))
    await asyncio.wait_for(processed.wait(), timeout=1.0)

    assert captured
    assert captured[0].dtype == np.float32
    assert captured[0].size >= 320
    handler.deps.movement_manager.set_listening.assert_any_call(True)
    handler.deps.movement_manager.set_listening.assert_any_call(False)
    assert activities[:2] == ["user_speech_started", "user_speech_stopped"]


@pytest.mark.asyncio
async def test_process_turn_emits_transcripts_and_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    """A completed turn must emit both transcripts and synthesized audio."""
    handler = _build_handler()
    monkeypatch.setattr(handler, "_transcribe", lambda _audio: asyncio.sleep(0, result="hello reachy"))
    monkeypatch.setattr(handler, "_request_completion", lambda: asyncio.sleep(0, result="hello human"))
    monkeypatch.setattr(
        handler,
        "_synthesize",
        lambda _text: asyncio.sleep(0, result=(16_000, np.ones(160, dtype=np.int16))),
    )

    await handler._process_turn(np.ones(320, dtype=np.float32))

    outputs = []
    while not handler.output_queue.empty():
        outputs.append(handler.output_queue.get_nowait())

    messages = [output.args[0] for output in outputs if isinstance(output, AdditionalOutputs)]
    audio = [output for output in outputs if isinstance(output, tuple)]
    assert messages == [
        {"role": "user", "content": "hello reachy"},
        {"role": "assistant", "content": "hello human"},
    ]
    assert len(audio) == 1
    assert audio[0][0] == 16_000
    assert handler.conversation_history == [
        {"role": "user", "content": "hello reachy"},
        {"role": "assistant", "content": "hello human"},
    ]


@pytest.mark.asyncio
async def test_apply_personality_replaces_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Applying a personality must affect subsequent chat completions."""
    handler = _build_handler()
    selected: list[str | None] = []
    monkeypatch.setattr(config_module, "set_custom_profile", selected.append)
    monkeypatch.setattr("reachy_chat.openrouter_chat.get_session_instructions", lambda _path=None: "New prompt")

    result = await handler.apply_personality("captain_circuit")

    assert result == "Applied personality."
    assert selected == ["captain_circuit"]
    assert handler._system_prompt == "New prompt"


def test_main_uses_discrete_handler_for_openai_backend() -> None:
    """The active OpenAI backend path must no longer import Realtime code."""
    source = (PROJECT_ROOT / "src/reachy_chat/main.py").read_text(encoding="utf-8")

    assert "from reachy_chat.openrouter_chat import OpenRouterChatHandler" in source
    assert "return OpenRouterChatHandler(" in source
    assert "openai_realtime" not in source


def test_macos_synthesis_uses_say_with_selected_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Darwin synthesis must invoke say without initializing pyttsx3."""
    handler = _build_handler()
    handler._voice_override = "samantha"
    expected = np.array([1, -2, 3], dtype=np.int16)
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        commands.append(command)
        if command == ["say", "-v", "?"]:
            return SimpleNamespace(stdout="Samantha            en_US    # Hello!\n", stderr="")
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_bytes(b"AIFF")
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("reachy_chat.openrouter_chat.subprocess.run", fake_run)
    monkeypatch.setattr(handler, "_decode_audio_blocking", lambda path: (16_000, expected))
    monkeypatch.setattr(
        handler,
        "_ensure_tts_engine_blocking",
        MagicMock(side_effect=AssertionError("pyttsx3 must stay lazy on macOS")),
    )

    sample_rate, samples = handler._synthesize_blocking("Hello, this is a test.")

    assert sample_rate == 16_000
    assert np.array_equal(samples, expected)
    assert commands[0] == ["say", "-v", "?"]
    assert commands[1][:4] == ["say", "-v", "Samantha", "-o"]
    assert commands[1][-1] == "Hello, this is a test."
    assert Path(commands[1][4]).suffix == ".aiff"


def test_macos_synthesis_retries_as_wav_when_aiff_decode_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Darwin synthesis must retry with WAV when AV cannot decode AIFF."""
    handler = _build_handler()
    expected = np.array([4, 5, 6], dtype=np.int16)
    generated_paths: list[Path] = []

    def fake_run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        output_path = Path(command[command.index("-o") + 1])
        generated_paths.append(output_path)
        output_path.write_bytes(b"audio")
        return SimpleNamespace(stdout="", stderr="")

    def fake_decode(path: Path) -> tuple[int, np.ndarray[Any, np.dtype[np.int16]]]:
        if path.suffix == ".aiff":
            raise RuntimeError("undecodable AIFF")
        return 16_000, expected

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("reachy_chat.openrouter_chat.subprocess.run", fake_run)
    monkeypatch.setattr(handler, "_decode_audio_blocking", fake_decode)

    sample_rate, samples = handler._synthesize_blocking("Fallback test")

    assert sample_rate == 16_000
    assert np.array_equal(samples, expected)
    assert [path.suffix for path in generated_paths] == [".aiff", ".wav"]


def test_macos_available_voices_parses_multiword_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Voice enumeration must preserve multi-word say voice names."""
    handler = _build_handler()
    output = (
        "Albert              en_US    # Hello! My name is Albert.\n"
        "Bad News            en_US    # Hello! My name is Bad News.\n"
        "Amélie              fr_CA    # Bonjour!\n"
    )

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "reachy_chat.openrouter_chat.subprocess.run",
        MagicMock(return_value=SimpleNamespace(stdout=output, stderr="")),
    )

    assert handler._available_voices_blocking() == ["Albert", "Bad News", "Amélie"]


def test_macos_voice_selection_returns_canonical_say_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Voice selection must validate case-insensitively and retain canonical spelling."""
    handler = _build_handler()
    output = "Samantha            en_US    # Hello!\nBad News            en_US    # Hello!\n"

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "reachy_chat.openrouter_chat.subprocess.run",
        MagicMock(return_value=SimpleNamespace(stdout=output, stderr="")),
    )

    assert handler._select_voice_blocking("bad news") == "Bad News"
    with pytest.raises(ValueError, match="Unknown voice: Missing"):
        handler._select_voice_blocking("Missing")


def test_macos_voice_enumeration_falls_back_when_say_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Voice enumeration must use pyttsx3 if say cannot be executed."""
    handler = _build_handler()
    engine = MagicMock()
    engine.getProperty.return_value = [SimpleNamespace(id="fallback-id")]
    run = MagicMock(side_effect=FileNotFoundError)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("reachy_chat.openrouter_chat.subprocess.run", run)
    monkeypatch.setattr(handler, "_ensure_tts_engine_blocking", MagicMock(return_value=engine))

    assert handler._available_voices_blocking() == ["fallback-id"]
    run.assert_called_once()


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (FileNotFoundError(), "macOS 'say' command is unavailable"),
        (
            subprocess.CalledProcessError(1, ["say"], stderr="Voice not found"),
            "macOS 'say' command failed: Voice not found",
        ),
    ],
)
def test_macos_synthesis_reports_clear_say_errors(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    message: str,
) -> None:
    """Darwin synthesis must explain missing and failed say commands."""
    handler = _build_handler()

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("reachy_chat.openrouter_chat.subprocess.run", MagicMock(side_effect=failure))

    with pytest.raises(RuntimeError, match=message):
        handler._synthesize_blocking("Hello")


def test_non_macos_synthesis_keeps_pyttsx3_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-Darwin synthesis must retain the existing pyttsx3 path."""
    handler = _build_handler()
    engine = MagicMock()
    expected = np.array([7, 8], dtype=np.int16)

    def save_to_file(_text: str, filename: str) -> None:
        Path(filename).write_bytes(b"WAVE")

    engine.save_to_file.side_effect = save_to_file
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(handler, "_ensure_tts_engine_blocking", MagicMock(return_value=engine))
    monkeypatch.setattr(handler, "_decode_audio_blocking", lambda path: (16_000, expected))

    sample_rate, samples = handler._synthesize_blocking("Hello")

    assert sample_rate == 16_000
    assert np.array_equal(samples, expected)
    engine.save_to_file.assert_called_once()
    engine.runAndWait.assert_called_once_with()
