from __future__ import annotations

import asyncio
import json
import runpy
import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from reachy_chat.tools.core_tools import ToolDependencies
from reachy_hermes.gateway import (
    HERMES_GATEWAY_PORT,
    HERMES_MAX_SESSION_TURNS,
    HERMES_TOOLSETS,
    HermesCancelled,
    HermesGatewayClient,
    HermesInvocationError,
)
from reachy_hermes.hermes import HermesHandler, is_cancel_phrase, to_spoken_text
from reachy_hermes.tts import NeuralTTS


class FakeGateway:
    def __init__(self, answer: str = "read-only answer") -> None:
        self.answer = answer
        self.prompts: list[str] = []
        self.histories: list[list[dict[str, str]]] = []
        self.interrupted = False
        self.closed = False
        self.active = False
        self.wait_for_interrupt = False
        self._interrupt_event = asyncio.Event()

    async def ask(
        self,
        prompt: str,
        history: list[dict[str, str]] | None = None,
        status_callback: Any = None,
    ) -> str:
        self.prompts.append(prompt)
        self.histories.append(history or [])
        self.active = True
        try:
            if status_callback is not None:
                status_callback("searching")
            if self.wait_for_interrupt:
                await self._interrupt_event.wait()
                raise HermesCancelled("cancelled")
            return self.answer
        finally:
            self.active = False

    async def interrupt(self) -> None:
        self.interrupted = True
        self._interrupt_event.set()

    async def close(self) -> None:
        self.closed = True
        await self.interrupt()


class FakeStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)

    async def readline(self) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    async def read(self, size: int) -> bytes:
        del size
        return self.chunks.pop(0) if self.chunks else b""


class FakeProcess:
    def __init__(self, stdout: list[bytes] | None = None, stderr: list[bytes] | None = None) -> None:
        self.pid = 12345
        self.stdout = FakeStream(stdout or [])
        self.stderr = FakeStream(stderr or [])
        self.returncode: int | None = None
        self.done = asyncio.Event()

    async def wait(self) -> int | None:
        await self.done.wait()
        return self.returncode


class FakeWebSocket:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = [json.dumps(message) for message in messages]
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if not self.messages:
            raise RuntimeError("fake WebSocket exhausted")
        return self.messages.pop(0)

    async def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, websocket: FakeWebSocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> FakeWebSocket:
        return self.websocket

    async def __aexit__(self, *args: Any) -> None:
        await self.websocket.close()


def build_handler(gateway: FakeGateway | None = None, **kwargs: Any) -> HermesHandler:
    deps = ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock(), camera_worker=None)
    return HermesHandler(deps, gateway=gateway or FakeGateway(), **kwargs)  # type: ignore[arg-type]


def event(event_type: str, payload: dict[str, Any], session_id: str = "session-1") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": event_type, "session_id": session_id, "payload": payload},
    }


def test_cancel_phrase_matching_is_conservative_and_case_insensitive() -> None:
    assert is_cancel_phrase("STOP!")
    assert is_cancel_phrase("never   mind")
    assert is_cancel_phrase("please cancel that")
    assert not is_cancel_phrase("what is the weather")

def test_markdown_is_defensively_normalized_for_speech() -> None:
    response = "## Update\n* **First:** done [source](https://example.com)\n* `Second` #2 [1]"
    assert to_spoken_text(response) == "Update First: done source Second 2"


@pytest.mark.asyncio
async def test_handler_sends_only_current_turn_to_persistent_gateway() -> None:
    gateway = FakeGateway()
    handler = build_handler(gateway)
    handler._record_message("user", "older question")
    handler._record_message("assistant", "older answer")
    handler._record_message("user", "what is next?")

    assert await handler.invoke_hermes() == "read-only answer"
    assert gateway.prompts == ["what is next?"]
    assert gateway.histories == [
        [
            {"role": "user", "content": "older question"},
            {"role": "assistant", "content": "older answer"},
        ]
    ]
    assert HERMES_TOOLSETS == "all"
    await handler.shutdown()


@pytest.mark.asyncio
async def test_isolated_gateway_uses_fixed_toolsets_sessions_and_streamed_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "hermes"
    binary.touch()
    process = FakeProcess(stdout=[f"HERMES_BACKEND_READY port={HERMES_GATEWAY_PORT}\n".encode()])
    spawn_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def spawn(*args: Any, **kwargs: Any) -> FakeProcess:
        spawn_calls.append((args, kwargs))
        return process

    websocket = FakeWebSocket(
        [
            event("gateway.ready", {}, session_id=""),
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"session_id": "session-1", "stored_session_id": "stored-1"},
            },
            {"jsonrpc": "2.0", "id": 2, "result": {"value": "1"}},
            {"jsonrpc": "2.0", "id": 3, "result": {"accepted": True}},
            event("status.update", {"text": "searching"}),
            event("message.delta", {"text": "read-only "}),
            event("message.delta", {"text": "answer"}),
            event("message.complete", {"status": "complete"}),
        ]
    )
    statuses: list[str] = []
    client = HermesGatewayClient(
        hermes_binary=binary,
        process_factory=spawn,
        connect_factory=lambda url: FakeConnection(websocket),
        runtime_dir=tmp_path / "runtime",
    )

    answer = await client.ask("question", status_callback=statuses.append)
    assert answer == "read-only answer"
    assert statuses == ["searching"]
    args, kwargs = spawn_calls[0]
    assert args[:2] == (str(binary), "serve")
    assert "--isolated" in args
    assert kwargs["env"]["HERMES_TUI_TOOLSETS"] == "all"
    assert "HERMES_KANBAN_TASK" not in kwargs["env"]
    assert (tmp_path / "runtime" / "AGENTS.md").read_text().startswith("# Reachy Hermes voice policy")
    methods = [message["method"] for message in websocket.sent]
    assert methods == ["session.create", "config.set", "prompt.submit"]
    assert websocket.sent[0]["params"]["messages"] == []
    assert websocket.sent[-1]["params"]["text"] == "question"

    signals: list[int] = []

    def signal_process(proc: Any, signum: int) -> None:
        signals.append(signum)
        proc.returncode = 143
        proc.done.set()

    monkeypatch.setattr(client, "_signal_process_group", signal_process)
    await client.close()
    assert signals == [signal.SIGTERM]


@pytest.mark.asyncio
async def test_gateway_cycles_bounded_agent_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "hermes"
    binary.touch()
    process = FakeProcess(stdout=[f"HERMES_BACKEND_READY port={HERMES_GATEWAY_PORT}\n".encode()])

    async def spawn(*args: Any, **kwargs: Any) -> FakeProcess:
        return process

    websocket = FakeWebSocket(
        [
            event("gateway.ready", {}, session_id=""),
            {"jsonrpc": "2.0", "id": 1, "result": {"session_id": "old-live"}},
            {"jsonrpc": "2.0", "id": 2, "result": {"closed": True}},
            {"jsonrpc": "2.0", "id": 3, "result": {"deleted": "stored-old"}},
            {
                "jsonrpc": "2.0",
                "id": 4,
                "result": {"session_id": "session-new", "stored_session_id": "stored-new"},
            },
            {"jsonrpc": "2.0", "id": 5, "result": {"value": "1"}},
            {"jsonrpc": "2.0", "id": 6, "result": {"accepted": True}},
            event("message.delta", {"text": "fresh answer"}, session_id="session-new"),
            event("message.complete", {"status": "complete"}, session_id="session-new"),
        ]
    )
    client = HermesGatewayClient(
        hermes_binary=binary,
        process_factory=spawn,
        connect_factory=lambda url: FakeConnection(websocket),
        runtime_dir=tmp_path / "runtime",
    )
    client._stored_session_id = "stored-old"
    client._turns_in_session = HERMES_MAX_SESSION_TURNS
    history = [{"role": "user", "content": "bounded context"}]

    assert await client.ask("fresh question", history=history) == "fresh answer"
    methods = [message["method"] for message in websocket.sent]
    assert methods == [
        "session.resume",
        "session.close",
        "session.delete",
        "session.create",
        "config.set",
        "prompt.submit",
    ]
    assert websocket.sent[3]["params"]["messages"] == history

    def signal_process(proc: Any, signum: int) -> None:
        proc.returncode = 143
        proc.done.set()

    monkeypatch.setattr(client, "_signal_process_group", signal_process)
    await client.close()


@pytest.mark.asyncio
async def test_voice_cancellation_interrupts_gateway_and_returns_spoken_ack() -> None:
    gateway = FakeGateway()
    gateway.wait_for_interrupt = True
    handler = build_handler(gateway)
    handler._record_message("user", "long request")
    request = asyncio.create_task(handler._request_completion())
    for _ in range(20):
        if gateway.active:
            break
        await asyncio.sleep(0)
    await handler.cancel_current()
    assert await request == "Okay, I stopped."
    assert gateway.interrupted is True
    await handler.shutdown()


@pytest.mark.asyncio
async def test_gateway_rejects_oversized_stream_and_sends_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "hermes"
    binary.touch()
    process = FakeProcess(stdout=[f"HERMES_BACKEND_READY port={HERMES_GATEWAY_PORT}\n".encode()])

    async def spawn(*args: Any, **kwargs: Any) -> FakeProcess:
        return process

    websocket = FakeWebSocket(
        [
            event("gateway.ready", {}, session_id=""),
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"session_id": "session-1", "stored_session_id": "stored-1"},
            },
            {"jsonrpc": "2.0", "id": 2, "result": {"value": "1"}},
            {"jsonrpc": "2.0", "id": 3, "result": {"accepted": True}},
            event("message.delta", {"text": "x" * (32 * 1024 + 1)}),
        ]
    )
    client = HermesGatewayClient(
        hermes_binary=binary,
        process_factory=spawn,
        connect_factory=lambda url: FakeConnection(websocket),
        runtime_dir=tmp_path / "runtime",
    )
    with pytest.raises(HermesInvocationError, match="32 KiB"):
        await client.ask("too much")
    assert websocket.sent[-1]["method"] == "session.interrupt"

    def signal_process(proc: Any, signum: int) -> None:
        proc.returncode = 143
        proc.done.set()

    monkeypatch.setattr(client, "_signal_process_group", signal_process)
    await client.close()


@pytest.mark.asyncio
async def test_missing_binary_is_spoken_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    handler = build_handler(hermes_binary="/missing/hermes", gateway=None)
    handler.gateway = HermesGatewayClient(hermes_binary="/missing/hermes")
    handler._record_message("user", "hello")
    with caplog.at_level("ERROR"):
        reply = await handler._request_completion()
    assert reply == "I can't start Hermes because its executable is missing."
    assert "/missing/hermes" in caplog.text
    await handler.shutdown()


@pytest.mark.asyncio
async def test_voice_cancel_transcript_stops_current_turn() -> None:
    handler = build_handler()
    cancelled = AsyncMock()
    handler.cancel_current = cancelled  # type: ignore[method-assign]
    handler._processing_turn = True
    handler._transcribe = MagicMock(return_value=asyncio.sleep(0, result="never mind"))  # type: ignore[method-assign]
    await handler._check_cancel_utterance(np.ones(100, dtype=np.float32))
    cancelled.assert_awaited_once_with()
    await handler.shutdown()


@pytest.mark.asyncio
async def test_vad_keeps_preroll_and_finalizes_after_shorter_silence() -> None:
    handler = build_handler()
    captured: list[np.ndarray[Any, Any]] = []

    async def capture(audio: np.ndarray[Any, Any]) -> None:
        captured.append(audio)
        handler._processing_turn = False

    handler._process_turn = capture  # type: ignore[method-assign]
    silence = np.zeros(1_600, dtype=np.float32)
    speech = np.full(1_600, 0.03, dtype=np.float32)
    for frame in [silence, silence, speech, speech, speech, *([silence] * 6)]:
        await handler.receive((16_000, frame))
    assert handler._processing_task is not None
    await handler._processing_task
    assert len(captured) == 1
    assert np.all(captured[0][:3_200] == 0)
    assert captured[0].size == 17_600
    await handler.shutdown()


@pytest.mark.asyncio
async def test_completion_emits_immediate_processing_chime() -> None:
    gateway = FakeGateway("final answer")
    handler = build_handler(gateway)
    handler._record_message("user", "question")
    reply = await handler._request_completion()
    assert reply == "final answer"
    sample_rate, chime = await handler.output_queue.get()
    assert sample_rate == 16_000
    assert chime.dtype == np.int16
    assert chime.size == 2_880
    assert np.max(np.abs(chime)) > 0
    assert handler._working_status_task is None
    await handler.shutdown()


@pytest.mark.asyncio
async def test_working_updates_tick_every_eight_seconds_and_speak_periodically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import reachy_hermes.hermes as hermes_module

    handler = build_handler()
    handler._gateway_turn_active = True
    delays: list[float] = []
    ticks: list[bool] = []
    updates: list[str] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        await real_sleep(0)

    async def fake_tick() -> None:
        ticks.append(True)

    async def fake_update() -> None:
        updates.append(handler.WORKING_UPDATE_MESSAGE)
        if len(updates) == 2:
            handler._gateway_turn_active = False

    monkeypatch.setattr(hermes_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(handler, "_queue_progress_tick", fake_tick)
    monkeypatch.setattr(handler, "_speak_working_update", fake_update)
    await handler._working_status_updates()
    assert updates == [handler.WORKING_UPDATE_MESSAGE, handler.WORKING_UPDATE_MESSAGE]
    assert delays == [8.0] * 7
    assert ticks == [True] * 7
    await handler.shutdown()


def test_neural_tts_converts_float_audio_to_int16(monkeypatch: pytest.MonkeyPatch) -> None:
    tts = NeuralTTS()
    engine = MagicMock()
    engine.generate.return_value = SimpleNamespace(sample_rate=24_000, samples=[-2.0, -0.5, 0.0, 0.5, 2.0])
    tts._engine = engine
    monkeypatch.setenv("REACHY_HERMES_TTS_SPEAKER", "3")

    sample_rate, samples = tts.synthesize("hello")
    assert sample_rate == 24_000
    assert samples.tolist() == [-32767, -16384, 0, 16384, 32767]
    engine.generate.assert_called_once_with("hello", sid=3, speed=1.05)


def test_stop_during_whisper_prewarm_skips_readiness_and_stream_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import reachy_hermes.main as hermes_main

    stop_event = hermes_main.threading.Event()
    movement = MagicMock()
    handler = MagicMock()
    handler.ensure_whisper_loaded.side_effect = stop_event.set
    stream = MagicMock()
    signal_ready = MagicMock()
    return_home = MagicMock()
    monkeypatch.setattr(hermes_main, "MovementManager", MagicMock(return_value=movement))
    monkeypatch.setattr(hermes_main, "HermesHandler", MagicMock(return_value=handler))
    monkeypatch.setattr(hermes_main, "HermesLocalStream", MagicMock(return_value=stream))
    monkeypatch.setattr(hermes_main, "_signal_ready", signal_ready)
    monkeypatch.setattr(hermes_main, "_return_home", return_home)
    monkeypatch.setattr(hermes_main.threading, "Thread", MagicMock())
    robot = MagicMock()

    hermes_main.ReachyHermes().run(robot, stop_event)

    handler.ensure_tts_loaded.assert_not_called()
    signal_ready.assert_not_called()
    stream.launch.assert_not_called()
    stream.close.assert_called_once_with()
    return_home.assert_called_once_with(robot)


def test_module_entrypoint_invokes_sdk_wrapper_without_robot(monkeypatch: pytest.MonkeyPatch) -> None:
    from reachy_mini import ReachyMiniApp

    called: list[bool] = []
    monkeypatch.setattr(ReachyMiniApp, "wrapped_run", lambda self: called.append(True), raising=False)
    runpy.run_module("reachy_hermes.main", run_name="__main__")
    assert called == [True]
