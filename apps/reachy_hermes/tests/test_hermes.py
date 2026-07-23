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
from reachy_hermes.hermes import (
    HermesHandler,
    is_cancel_phrase,
    split_for_speech,
    to_spoken_text,
)
from reachy_hermes.tts import NeuralTTS


class FakeGateway:
    def __init__(self, answer: str = "read-only answer") -> None:
        self.answer = answer
        self.prompts: list[str] = []
        self.histories: list[list[dict[str, str]]] = []
        self.queued_prompts: list[str] = []
        self.message_batches: list[list[dict[str, Any]]] = []
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

    async def queue_prompt(self, prompt: str) -> bool:
        if not self.active:
            return False
        self.queued_prompts.append(prompt)
        return True

    async def prepare(self) -> None:
        return None

    async def fetch_messages(self) -> list[dict[str, Any]]:
        return self.message_batches.pop(0) if self.message_batches else []

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


@pytest.mark.parametrize(
    "transcript",
    ["stop", "please stop", "cancel that", "never mind", "okay stop"],
)
def test_cancel_phrase_matches_bare_stop_commands(transcript: str) -> None:
    assert is_cancel_phrase(transcript)


@pytest.mark.parametrize(
    "transcript",
    ["don't stop, also check tomorrow", "and cancel my 3pm meeting"],
)
def test_cancel_phrase_ignores_keyword_inside_a_longer_sentence(transcript: str) -> None:
    assert not is_cancel_phrase(transcript)

def test_markdown_is_defensively_normalized_for_speech() -> None:
    response = "## Update\n* **First:** done [source](https://example.com)\n* `Second` #2 [1]"
    assert to_spoken_text(response) == "Update First: done source Second 2"

def test_speech_text_is_split_into_bounded_sentence_chunks() -> None:
    text = (
        "First, this answer should begin playing quickly. "
        "Second, synthesis should continue while the first part plays. "
        "Finally, the reply remains natural."
    )
    chunks = split_for_speech(text)
    assert len(chunks) == 3
    assert all(len(chunk) <= 70 for chunk in chunks)
    assert " ".join(chunks) == text

def test_speech_chunks_merge_a_tiny_trailing_fragment() -> None:
    text = "one two three four five six seven eight nine tiny"
    chunks = split_for_speech(text, maximum=20)
    assert len(chunks[-1]) >= 24
    assert " ".join(chunks) == text


@pytest.mark.asyncio
async def test_gateway_rejects_raw_tool_output_as_final_speech(tmp_path: Path) -> None:
    binary = tmp_path / "hermes"
    binary.touch()
    client = HermesGatewayClient(
        hermes_binary=binary,
        runtime_dir=tmp_path / "runtime",
    )
    client._session_id = "session-1"
    websocket = FakeWebSocket(
        [
            event(
                "message.complete",
                {"text": '{"number":1177,"body":"tool result"}', "status": "complete"},
            )
        ]
    )
    with pytest.raises(HermesInvocationError, match="raw tool output"):
        await client._collect_response(websocket, [], None)


@pytest.mark.asyncio
async def test_gateway_queues_followup_without_replacing_current_answer(tmp_path: Path) -> None:
    binary = tmp_path / "hermes"
    binary.touch()
    client = HermesGatewayClient(hermes_binary=binary)
    websocket = FakeWebSocket(
        [
            event("message.complete", {"text": "first answer", "status": "complete"}),
            event("message.complete", {"text": "follow-up answer", "status": "complete"}),
        ]
    )
    client._active_websocket = websocket
    client._session_id = "session-1"

    assert await client.queue_prompt("also compare the alternatives") is True
    assert websocket.sent[-2]["method"] == "config.set"
    assert websocket.sent[-2]["params"]["key"] == "busy"
    assert websocket.sent[-2]["params"]["value"] == "queue"
    assert websocket.sent[-1]["method"] == "prompt.submit"
    assert websocket.sent[-1]["params"] == {
        "session_id": "session-1",
        "text": "also compare the alternatives",
    }
    assert await client._collect_response(websocket, [], None) == "follow-up answer"


@pytest.mark.asyncio
async def test_gateway_queues_two_followups_and_returns_final_answer(tmp_path: Path) -> None:
    binary = tmp_path / "hermes"
    binary.touch()
    client = HermesGatewayClient(hermes_binary=binary)
    websocket = FakeWebSocket(
        [
            event("message.complete", {"text": "first answer", "status": "complete"}),
            event("message.complete", {"text": "second answer", "status": "complete"}),
            event("message.complete", {"text": "final answer", "status": "complete"}),
        ]
    )
    client._active_websocket = websocket
    client._session_id = "session-1"

    assert await client.queue_prompt("first follow-up") is True
    assert await client.queue_prompt("second follow-up") is True
    assert client._followups_queued == 2

    assert await client._collect_response(websocket, [], None) == "final answer"
    assert client._followups_queued == 0


@pytest.mark.asyncio
async def test_close_with_stored_session_skips_external_interrupt_dial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "hermes"
    binary.touch()
    connect_calls: list[str] = []

    def connect_factory(url: str) -> Any:
        connect_calls.append(url)
        raise AssertionError("close() must not dial a new external connection")

    client = HermesGatewayClient(
        hermes_binary=binary,
        connect_factory=connect_factory,
        runtime_dir=tmp_path / "runtime",
    )
    client._process = FakeProcess()
    client._stored_session_id = "stored-1"
    # No active websocket/session id: simulates close() running after ask() already exited.

    def signal_process(proc: Any, signum: int) -> None:
        proc.returncode = 143
        proc.done.set()

    monkeypatch.setattr(client, "_signal_process_group", signal_process)

    await client.close()

    assert connect_calls == []


@pytest.mark.asyncio
async def test_gateway_interrupts_session_owned_by_native_desktop(tmp_path: Path) -> None:
    binary = tmp_path / "hermes"
    binary.touch()
    websocket = FakeWebSocket(
        [
            event("gateway.ready", {}, session_id=""),
            {"jsonrpc": "2.0", "id": 1, "result": {"session_id": "external-live"}},
            {"jsonrpc": "2.0", "id": 2, "result": {"interrupted": True}},
        ]
    )
    client = HermesGatewayClient(
        hermes_binary=binary,
        connect_factory=lambda url: FakeConnection(websocket),
    )
    client._process = FakeProcess()
    client._stored_session_id = "stored-1"

    await client.interrupt()

    assert [message["method"] for message in websocket.sent] == [
        "session.resume",
        "session.interrupt",
    ]
    assert websocket.sent[-1]["params"]["session_id"] == "external-live"


@pytest.mark.asyncio
async def test_spoken_followup_uses_live_hermes_queue() -> None:
    gateway = FakeGateway()
    gateway.active = True
    handler = build_handler(gateway)
    handler._processing_turn = True
    handler._transcribe = MagicMock(  # type: ignore[method-assign]
        return_value=asyncio.sleep(0, result="also compare the alternatives")
    )

    await handler._check_cancel_utterance(np.ones(100, dtype=np.float32))

    assert gateway.queued_prompts == ["also compare the alternatives"]
    assert handler.conversation_history[-1] == {
        "role": "user",
        "content": "also compare the alternatives",
    }
    await handler.shutdown()


@pytest.mark.asyncio
async def test_late_followup_with_no_active_turn_starts_a_new_gateway_turn() -> None:
    gateway = FakeGateway(answer="late follow-up answer")
    handler = build_handler(gateway)
    handler._synthesize = AsyncMock(  # type: ignore[method-assign]
        return_value=(16_000, np.zeros(160, dtype=np.int16))
    )
    handler._transcribe = MagicMock(  # type: ignore[method-assign]
        return_value=asyncio.sleep(0, result="what about tomorrow")
    )
    assert handler._processing_turn is False  # no active turn to flush into

    await handler._check_cancel_utterance(np.ones(100, dtype=np.float32))

    assert handler._processing_task is not None
    await handler._processing_task

    assert gateway.prompts == ["what about tomorrow"]
    assert handler.conversation_history[-1] == {
        "role": "assistant",
        "content": "late follow-up answer",
    }
    assert handler._processing_turn is False
    await handler.shutdown()


@pytest.mark.asyncio
async def test_native_desktop_turn_is_relayed_through_reachy_speech(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import reachy_hermes.hermes as hermes_module

    gateway = FakeGateway()
    gateway.message_batches = [
        [],
        [{"id": 1, "role": "user", "content": "typed from my Mac"}],
        [
            {"id": 1, "role": "user", "content": "typed from my Mac"},
            {"id": 2, "role": "assistant", "content": "Reply from the shared session."},
        ],
    ]
    handler = build_handler(gateway)
    handler._synthesize = AsyncMock(  # type: ignore[method-assign]
        return_value=(16_000, np.zeros(160, dtype=np.int16))
    )
    monkeypatch.setattr(hermes_module, "NATIVE_SESSION_POLL_SECONDS", 0.001)
    monkeypatch.setattr(hermes_module, "NATIVE_SESSION_QUIET_SECONDS", 0.0)

    relay = asyncio.create_task(handler._relay_native_session())
    for _ in range(100):
        if any(
            message.get("content") == "Reply from the shared session."
            for message in handler.conversation_history
        ):
            break
        await asyncio.sleep(0.001)
    handler._stop_event.set()
    await asyncio.wait_for(relay, timeout=1)

    assert handler.conversation_history[-2:] == [
        {"role": "user", "content": "typed from my Mac"},
        {"role": "assistant", "content": "Reply from the shared session."},
    ]
    assert handler._processing_turn is False
    await handler.shutdown()


@pytest.mark.asyncio
async def test_relay_skips_local_voice_answer_and_speaks_genuine_native_answer_despite_malformed_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import reachy_hermes.hermes as hermes_module

    gateway = FakeGateway()
    malformed = {"id": "abc", "role": "assistant", "content": "ignored"}
    typed = {"id": 1, "role": "user", "content": "typed from my Mac"}
    echoed_local_answer = {"id": 2, "role": "assistant", "content": "voice turn raw answer"}
    native_answer = {"id": 3, "role": "assistant", "content": "Reply from the shared session."}
    gateway.message_batches = [
        [],
        [malformed],
        [malformed, typed],
        [malformed, typed, echoed_local_answer],
        [malformed, typed, echoed_local_answer, native_answer],
    ]
    handler = build_handler(gateway)
    handler._local_session_answers.append("voice turn raw answer")
    handler._synthesize = AsyncMock(  # type: ignore[method-assign]
        return_value=(16_000, np.zeros(160, dtype=np.int16))
    )
    monkeypatch.setattr(hermes_module, "NATIVE_SESSION_POLL_SECONDS", 0.001)
    monkeypatch.setattr(hermes_module, "NATIVE_SESSION_QUIET_SECONDS", 0.0)

    relay = asyncio.create_task(handler._relay_native_session())
    for _ in range(200):
        if any(
            message.get("content") == "Reply from the shared session."
            for message in handler.conversation_history
        ):
            break
        await asyncio.sleep(0.001)
    handler._stop_event.set()
    await asyncio.wait_for(relay, timeout=1)

    # The malformed id ("abc") didn't kill the relay loop, and the locally-spoken voice
    # answer echoing back through the shared session was never re-spoken.
    assert not any(
        message.get("content") == "voice turn raw answer"
        for message in handler.conversation_history
    )
    assert handler.conversation_history[-1] == {
        "role": "assistant",
        "content": "Reply from the shared session.",
    }
    assert len(handler._local_session_answers) == 0
    await handler.shutdown()


@pytest.mark.asyncio
async def test_spoken_stop_interrupts_external_text_turn() -> None:
    gateway = FakeGateway()
    handler = build_handler(gateway)
    handler._native_text_turn_active = True
    handler._processing_turn = True
    handler._synthesize = AsyncMock(  # type: ignore[method-assign]
        return_value=(16_000, np.zeros(160, dtype=np.int16))
    )

    await handler.cancel_current()

    assert gateway.interrupted is True
    assert handler._native_text_turn_active is False
    assert handler._processing_turn is False
    assert handler.conversation_history[-1] == {
        "role": "assistant",
        "content": "Okay, I stopped.",
    }
    await handler.shutdown()


@pytest.mark.asyncio
async def test_failed_gateway_call_clears_stale_local_prompt() -> None:
    gateway = FakeGateway()

    async def failing_ask(*args: Any, **kwargs: Any) -> str:
        raise HermesInvocationError("connection refused")

    gateway.ask = failing_ask  # type: ignore[method-assign]
    handler = build_handler(gateway)
    handler.address_gate.semantic_check = MagicMock(
        return_value=SimpleNamespace(is_open=True, confidence=1.0, reason="test")
    )
    handler._transcribe = MagicMock(  # type: ignore[method-assign]
        return_value=asyncio.sleep(0, result="hello there")
    )
    handler._synthesize = AsyncMock(  # type: ignore[method-assign]
        return_value=(16_000, np.zeros(160, dtype=np.int16))
    )

    await handler._process_turn(np.ones(1_600, dtype=np.float32))

    assert list(handler._local_session_prompts) == []
    await handler.shutdown()

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
            event("message.delta", {"text": '{"number":1177,"body":"raw tool output"}'}),
            event("message.complete", {"text": "read-only answer", "status": "complete"}),
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
    descriptor = json.loads(client.connection_file.read_text())
    assert descriptor["gateway_url"] == f"http://127.0.0.1:{HERMES_GATEWAY_PORT}"
    assert descriptor["session_id"] == "stored-1"
    assert descriptor["session_title"] == "reachy-hermes"
    assert client.connection_file.stat().st_mode & 0o777 == 0o600

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
            event("message.delta", {"text": "intermediate tool output"}, session_id="session-new"),
            event(
                "message.complete",
                {"text": "fresh answer", "status": "complete"},
                session_id="session-new",
            ),
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
            event(
                "message.complete",
                {"text": "x" * (32 * 1024 + 1), "status": "complete"},
            ),
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
async def test_midtask_followup_uses_calibrated_speech_threshold() -> None:
    gateway = FakeGateway()
    gateway.active = True
    handler = build_handler(gateway)
    handler._processing_turn = True
    handler._speech_on_threshold = 0.015
    handler._speech_off_threshold = 0.01
    handler._transcribe = MagicMock(  # type: ignore[method-assign]
        return_value=asyncio.sleep(0, result="also compare the alternatives")
    )
    speech = np.full(1_600, 0.02, dtype=np.float32)
    silence = np.zeros(1_600, dtype=np.float32)

    for frame in [speech, speech, *([silence] * 12)]:
        handler._receive_cancel_frame((16_000, frame))

    assert handler._cancel_detection_task is not None
    await handler._cancel_detection_task
    assert gateway.queued_prompts == ["also compare the alternatives"]
    await handler.shutdown()


@pytest.mark.asyncio
async def test_robot_playback_and_echo_tail_are_never_retranscribed() -> None:
    handler = build_handler()
    handler._output_playback_until = float("inf")
    handler._speech_active = True
    handler._speech_chunks.append(np.ones(1_600, dtype=np.float32))
    handler._cancel_speech_active = True
    handler._cancel_speech_chunks.append(np.ones(1_600, dtype=np.float32))

    await handler.receive((16_000, np.ones(1_600, dtype=np.float32)))

    assert handler._processing_task is None
    assert handler._speech_active is False
    assert handler._speech_chunks == []
    assert handler._cancel_speech_active is False
    assert handler._cancel_speech_chunks == []
    await handler.shutdown()


@pytest.mark.asyncio
async def test_vad_keeps_preroll_and_waits_for_natural_pause() -> None:
    handler = build_handler()
    captured: list[np.ndarray[Any, Any]] = []

    async def capture(audio: np.ndarray[Any, Any]) -> None:
        captured.append(audio)
        handler._processing_turn = False

    handler._process_turn = capture  # type: ignore[method-assign]
    silence = np.zeros(1_600, dtype=np.float32)
    speech = np.full(1_600, 0.03, dtype=np.float32)
    for frame in [*([silence] * 5), speech, speech, speech, *([silence] * 12)]:
        await handler.receive((16_000, frame))
    assert handler._processing_task is not None
    await handler._processing_task
    assert len(captured) == 1
    assert np.all(captured[0][:3_200] == 0)
    assert captured[0].size == 30_400
    await handler.shutdown()

@pytest.mark.asyncio
async def test_vad_calibrates_above_steady_ambient_noise() -> None:
    handler = build_handler()
    ambient = np.full(1_600, 0.03, dtype=np.float32)

    for _ in range(15):
        await handler.receive((16_000, ambient))

    assert handler._processing_task is None
    assert not handler._speech_active
    assert handler._speech_on_threshold >= 0.045
    await handler.shutdown()



@pytest.mark.asyncio
async def test_processing_feedback_precedes_stt_and_uses_idle_choreography() -> None:
    handler = build_handler()

    async def transcribe(audio: np.ndarray[Any, Any]) -> str:
        assert audio.size
        assert not handler.output_queue.empty()
        return "question"

    handler._transcribe = transcribe  # type: ignore[method-assign]
    handler.address_gate.semantic_check = MagicMock(
        return_value=SimpleNamespace(
            is_open=True,
            confidence=1.0,
            reason="test",
        )
    )
    reply = (
        "First, this complete sentence should play without clipping any words. "
        "Second, a short pause should separate independently synthesized phrases."
    )
    handler._request_completion = AsyncMock(return_value=reply)  # type: ignore[method-assign]
    handler._synthesize = AsyncMock(  # type: ignore[method-assign]
        return_value=(16_000, np.ones(160, dtype=np.int16))
    )

    await handler._process_turn(np.ones(1_600, dtype=np.float32))

    sample_rate, chime = await handler.output_queue.get()
    assert sample_rate == 16_000
    assert chime.dtype == np.int16
    assert chime.size == 13_200
    peaks = [
        np.max(np.abs(chime[index * 5_000 : index * 5_000 + 3_200]))
        for index in range(3)
    ]
    assert peaks == sorted(peaks, reverse=True)
    assert np.all(chime[3_200:5_000] == 0)
    assert np.all(chime[8_200:10_000] == 0)
    dominant_bin = int(np.argmax(np.abs(np.fft.rfft(chime[:3_200]))))
    dominant_frequency = dominant_bin * sample_rate / 3_200
    assert 125.0 <= dominant_frequency <= 140.0
    handler.deps.movement_manager.wiggle_antennas.assert_not_called()
    handler.deps.movement_manager.set_processing.assert_not_called()
    handler.deps.reachy_mini.disable_wobbling.assert_called_once_with()
    handler.deps.reachy_mini.enable_wobbling.assert_called_once_with()
    assert handler._working_status_task is None
    queued = []
    while not handler.output_queue.empty():
        queued.append(handler.output_queue.get_nowait())
    assert any(
        isinstance(item, tuple)
        and item[1].size == 1_280
        and np.all(item[1] == 0)
        for item in queued
    )
    await handler.shutdown()


@pytest.mark.asyncio
async def test_processing_earcon_pauses_while_followup_speech_is_active() -> None:
    handler = build_handler()
    queue_burst = AsyncMock()
    handler._queue_processing_burst = queue_burst  # type: ignore[method-assign]

    handler._cancel_speech_active = True
    await handler._queue_progress_tick()
    queue_burst.assert_not_awaited()

    handler._cancel_speech_active = False
    await handler._queue_progress_tick()
    queue_burst.assert_awaited_once_with()
    await handler.shutdown()


def test_working_updates_rotate_without_immediate_repeats() -> None:
    handler = build_handler()
    count = len(handler.WORKING_UPDATE_MESSAGES)
    messages = [
        handler._next_working_update_message()
        for _ in range(count * 2)
    ]
    assert set(messages[:count]) == set(handler.WORKING_UPDATE_MESSAGES)
    assert set(messages[count:]) == set(handler.WORKING_UPDATE_MESSAGES)
    assert all(left != right for left, right in zip(messages, messages[1:]))


@pytest.mark.asyncio
async def test_working_updates_repeat_bursts_and_speak_periodically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import reachy_hermes.hermes as hermes_module

    handler = build_handler()
    handler._gateway_turn_active = True
    delays: list[float] = []
    ticks: list[bool] = []
    updates: list[bool] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        await real_sleep(0)

    async def fake_tick() -> None:
        ticks.append(True)

    async def fake_update() -> None:
        updates.append(True)
        if len(updates) == 2:
            handler._gateway_turn_active = False
    monkeypatch.setattr(hermes_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(handler, "_queue_progress_tick", fake_tick)
    monkeypatch.setattr(handler, "_speak_working_update", fake_update)
    await handler._working_status_updates()
    assert updates == [True, True]
    assert delays == [1.45] * 39
    assert ticks == [True] * 39
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
    engine.generate.assert_called_once_with("hello", sid=3, speed=0.95)


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


def test_startup_stage_writer_is_best_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import reachy_hermes.main as hermes_main

    monkeypatch.delenv("REACHY_READY_FILE", raising=False)
    hermes_main._signal_stage("importing")

    ready_file = tmp_path / "hermes.ready"
    monkeypatch.setenv("REACHY_READY_FILE", str(ready_file))
    hermes_main._signal_stage("connecting-daemon")
    assert Path(f"{ready_file}.stage").read_text() == "connecting-daemon"

    def fail_write(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise OSError("unwritable")

    monkeypatch.setattr(hermes_main.Path, "write_text", fail_write)
    hermes_main._signal_stage("starting-gateway")


def test_signal_ready_writes_ready_stage_and_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import reachy_hermes.main as hermes_main

    ready_file = tmp_path / "hermes.ready"
    monkeypatch.setenv("REACHY_READY_FILE", str(ready_file))

    hermes_main._signal_ready()

    assert ready_file.exists()
    assert Path(f"{ready_file}.stage").read_text() == "ready"


def test_module_entrypoint_invokes_sdk_wrapper_without_robot(monkeypatch: pytest.MonkeyPatch) -> None:
    from reachy_mini import ReachyMiniApp

    called: list[bool] = []
    monkeypatch.setattr(ReachyMiniApp, "wrapped_run", lambda self: called.append(True), raising=False)
    runpy.run_module("reachy_hermes.main", run_name="__main__")
    assert called == [True]


def test_wrapped_run_skips_unused_local_camera_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import reachy_hermes.main as hermes_main
    from reachy_mini import ReachyMiniApp
    from reachy_mini.media.media_manager import MediaBackend, MediaManager

    camera_calls: list[bool] = []
    audio_calls: list[bool] = []

    def unexpected_camera_init(self: Any, *args: Any, **kwargs: Any) -> None:
        del self, args, kwargs
        camera_calls.append(True)

    class FakeAudio:
        def stop_recording(self) -> None:
            return None

        def stop_playing(self) -> None:
            return None

        def cleanup(self) -> None:
            return None

    def fake_audio_init(self: Any, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        audio_calls.append(True)
        self.audio = FakeAudio()

    monkeypatch.setattr(MediaManager, "_init_camera", unexpected_camera_init)
    monkeypatch.setattr(MediaManager, "_init_audio", fake_audio_init)

    def fake_wrapped_run(self: ReachyMiniApp) -> None:
        del self
        media = MediaManager(backend=MediaBackend.LOCAL, log_level="WARNING")
        assert media.camera is None
        assert isinstance(media.audio, FakeAudio)
        media.close()

    monkeypatch.setattr(ReachyMiniApp, "wrapped_run", fake_wrapped_run)
    hermes_main.ReachyHermes().wrapped_run()

    assert camera_calls == []
    assert audio_calls == [True]
    assert MediaManager._init_camera is unexpected_camera_init


def test_wrapped_run_restores_camera_initializer_on_startup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import reachy_hermes.main as hermes_main
    from reachy_mini import ReachyMiniApp
    from reachy_mini.media.media_manager import MediaManager

    def camera_init(self: Any, *args: Any, **kwargs: Any) -> None:
        del self, args, kwargs

    def fail_wrapped_run(self: ReachyMiniApp) -> None:
        del self
        raise RuntimeError("startup failed")

    monkeypatch.setattr(MediaManager, "_init_camera", camera_init)
    monkeypatch.setattr(ReachyMiniApp, "wrapped_run", fail_wrapped_run)

    with pytest.raises(RuntimeError, match="startup failed"):
        hermes_main.ReachyHermes().wrapped_run()

    assert MediaManager._init_camera is camera_init
