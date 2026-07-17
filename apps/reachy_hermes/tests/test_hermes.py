from __future__ import annotations
import runpy

import asyncio
import signal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from reachy_chat.tools.core_tools import ToolDependencies
from reachy_hermes.hermes import (
    HERMES_TOOLSETS,
    HISTORY_MAX_BYTES,
    HISTORY_MAX_MESSAGES,
    HermesCancelled,
    HermesHandler,
    HermesInvocationError,
    HermesTimeout,
    is_cancel_phrase,
)


class FakeStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = iter(chunks)

    async def read(self, size: int) -> bytes:
        del size
        try:
            return next(self.chunks)
        except StopIteration:
            return b""


class FakeProcess:
    def __init__(self, *, stdout: list[bytes] | None = None, stderr: list[bytes] | None = None, returncode: int | None = 0) -> None:
        self.pid = 12345
        self.stdout = FakeStream(stdout or [])
        self.stderr = FakeStream(stderr or [])
        self.returncode = returncode
        self.done = asyncio.Event()
        if returncode is not None:
            self.done.set()

    async def wait(self) -> int | None:
        await self.done.wait()
        return self.returncode


def build_handler(**kwargs: Any) -> HermesHandler:
    deps = ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock(), camera_worker=None)
    return HermesHandler(deps, **kwargs)


@pytest.mark.asyncio
async def test_invocation_uses_fixed_binary_toolsets_and_bounded_history(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setenv("HERMES_KANBAN_TASK", "unsafe-worker")
    process = FakeProcess(stdout=[b"read-only answer\n"])

    async def spawn(*args: Any, **kwargs: Any) -> FakeProcess:
        calls.append((args, kwargs))
        return process

    handler = build_handler(hermes_binary="/tmp/hermes", process_factory=spawn)
    for index in range(HISTORY_MAX_MESSAGES + 3):
        handler._record_message("user", f"message-{index}")
    assert len(handler.conversation_history) == HISTORY_MAX_MESSAGES
    assert handler._history_bytes() <= HISTORY_MAX_BYTES

    answer = await handler.invoke_hermes("what is next?")
    args, kwargs = calls[0]
    assert answer == "read-only answer"
    assert args == (
        "/tmp/hermes",
        "--oneshot",
        handler.build_prompt("what is next?"),
        "--toolsets",
        HERMES_TOOLSETS,
    )
    assert HERMES_TOOLSETS == "web,session_search"
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] is asyncio.subprocess.PIPE
    assert kwargs["stderr"] is asyncio.subprocess.PIPE
    assert "HERMES_KANBAN_TASK" not in kwargs["env"]
    await handler.shutdown()


def test_cancel_phrase_matching_is_conservative_and_case_insensitive() -> None:
    assert is_cancel_phrase("STOP!")
    assert is_cancel_phrase("never   mind")
    assert is_cancel_phrase("please cancel that")
    assert not is_cancel_phrase("what is the weather")


@pytest.mark.asyncio
async def test_cancellation_terminates_active_process(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(stdout=[b""], returncode=None)
    signals: list[int] = []

    async def spawn(*args: Any, **kwargs: Any) -> FakeProcess:
        return process

    def signal_group(proc: Any, signum: int) -> None:
        signals.append(signum)
        proc.returncode = 143
        proc.done.set()

    handler = build_handler(process_factory=spawn)
    monkeypatch.setattr(handler, "_signal_process_group", signal_group)
    invocation = asyncio.create_task(handler.invoke_hermes("wait"))
    for _ in range(20):
        if handler._active_process is not None:
            break
        await asyncio.sleep(0)
    await handler.cancel_current()
    with pytest.raises(HermesCancelled):
        await invocation
    assert signals == [signal.SIGTERM]
    await handler.shutdown()


@pytest.mark.asyncio
async def test_timeout_escalates_process_group_term_then_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(returncode=None)
    signals: list[int] = []

    async def spawn(*args: Any, **kwargs: Any) -> FakeProcess:
        return process

    def signal_group(proc: Any, signum: int) -> None:
        signals.append(signum)
        if signum == signal.SIGKILL:
            proc.returncode = -signal.SIGKILL
            proc.done.set()

    handler = build_handler(process_factory=spawn)
    handler.HERMES_TIMEOUT_SECONDS = 0.01
    monkeypatch.setattr("reachy_hermes.hermes.HERMES_TERM_GRACE_SECONDS", 0.001)
    monkeypatch.setattr(handler, "_signal_process_group", signal_group)
    with pytest.raises(HermesTimeout):
        await handler.invoke_hermes("hang")
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    await handler.shutdown()


@pytest.mark.asyncio
async def test_nonzero_exit_includes_bounded_stderr() -> None:
    process = FakeProcess(stderr=[b"bad hermes\n"], returncode=7)

    async def spawn(*args: Any, **kwargs: Any) -> FakeProcess:
        return process

    handler = build_handler(process_factory=spawn)
    with pytest.raises(HermesInvocationError, match="code 7: bad hermes"):
        await handler.invoke_hermes("fail")
    await handler.shutdown()


@pytest.mark.asyncio
async def test_oversized_output_is_rejected_without_unbounded_buffer() -> None:
    process = FakeProcess(stdout=[b"x" * (32 * 1024 + 1)])

    async def spawn(*args: Any, **kwargs: Any) -> FakeProcess:
        return process

    handler = build_handler(process_factory=spawn)
    with pytest.raises(HermesInvocationError, match="32 KiB"):
        await handler.invoke_hermes("too much")
    await handler.shutdown()


@pytest.mark.asyncio
async def test_invalid_utf8_stdout_is_a_spoken_failure() -> None:
    async def spawn(*args: Any, **kwargs: Any) -> FakeProcess:
        return FakeProcess(stdout=[b"\xff\xfe"], returncode=0)

    handler = build_handler(process_factory=spawn)
    with pytest.raises(HermesInvocationError, match="invalid UTF-8"):
        await handler.invoke_hermes("invalid")
    assert await handler._request_completion() == "I couldn't complete that request because Hermes failed."
    await handler.shutdown()


@pytest.mark.asyncio
async def test_missing_binary_is_spoken_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    async def spawn(*args: Any, **kwargs: Any) -> FakeProcess:
        raise FileNotFoundError("missing")

    handler = build_handler(hermes_binary="/missing/hermes", process_factory=spawn)
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
    await handler._check_cancel_utterance(MagicMock())
    cancelled.assert_awaited_once_with()
    await handler.shutdown()

@pytest.mark.asyncio
async def test_working_updates_speak_after_30_seconds_then_every_60(monkeypatch: pytest.MonkeyPatch) -> None:
    import reachy_hermes.hermes as hermes_module

    handler = build_handler()
    handler._active_process = object()
    handler.WORKING_UPDATE_INITIAL_DELAY_SECONDS = 30.0
    handler.WORKING_UPDATE_INTERVAL_SECONDS = 60.0
    delays: list[float] = []
    updates: list[str] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        await real_sleep(0)

    async def fake_update() -> None:
        updates.append(handler.WORKING_UPDATE_MESSAGE)
        if len(updates) == 2:
            handler._active_process = None

    monkeypatch.setattr(hermes_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(handler, "_speak_working_update", fake_update)
    await handler._working_status_updates()
    assert updates == [handler.WORKING_UPDATE_MESSAGE, handler.WORKING_UPDATE_MESSAGE]
    assert delays[:2] == [30.0, 60.0]
    await handler.shutdown()


@pytest.mark.asyncio
async def test_working_status_task_stops_before_final_completion_reply() -> None:
    handler = build_handler()
    # partial side-channel audio must not leak into a later request.
    handler._cancel_speech_active = True
    handler._cancel_speech_chunks.append(MagicMock())
    handler._cancel_speech_samples = 10
    handler._cancel_silence_samples = 5
    handler.invoke_hermes = AsyncMock(return_value="final answer")  # type: ignore[method-assign]
    reply = await handler._request_completion()
    assert reply == "final answer"
    assert handler._working_status_task is None
    assert handler.output_queue.empty()
    assert handler._cancel_speech_active is False
    assert handler._cancel_speech_chunks == []
    assert handler._cancel_speech_samples == 0
    assert handler._cancel_silence_samples == 0
    await handler.shutdown()


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

    signal_ready.assert_not_called()
    stream.launch.assert_not_called()
    stream.close.assert_called_once_with()
    return_home.assert_called_once_with(robot)


def test_module_entrypoint_invokes_sdk_wrapper_without_robot(monkeypatch: pytest.MonkeyPatch) -> None:
    from reachy_mini import ReachyMiniApp

    called: list[bool] = []
    monkeypatch.setattr(
        ReachyMiniApp,
        "wrapped_run",
        lambda self: called.append(True),
        raising=False,
    )
    runpy.run_module("reachy_hermes.main", run_name="__main__")
    assert called == [True]
