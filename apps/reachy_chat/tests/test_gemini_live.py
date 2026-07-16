"""Behavior tests for the Gemini Live handler."""

import base64
import asyncio
from types import SimpleNamespace
from typing import Any, Callable, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, call

import numpy as np
import pytest
from fastrtc import AdditionalOutputs

import reachy_chat.gemini_live as gemini_mod
import reachy_chat.idle_policy as idle_policy_mod
import reachy_chat.tools.core_tools as ct_mod
from reachy_chat.address_gate import AddressResult
from reachy_chat.gemini_live import GeminiLiveHandler
from reachy_chat.tools.core_tools import ToolDependencies
from reachy_chat.tools.tool_constants import ToolState
from reachy_chat.tools.background_tool_manager import ToolNotification


def _server_content(**kwargs: Any) -> SimpleNamespace:
    defaults = {
        "model_turn": None,
        "turn_complete": None,
        "interrupted": None,
        "grounding_metadata": None,
        "generation_complete": None,
        "input_transcription": None,
        "output_transcription": None,
        "url_context_metadata": None,
        "turn_complete_reason": None,
        "waiting_for_input": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _response(server_content: Any = None, tool_call: Any = None) -> SimpleNamespace:
    return SimpleNamespace(server_content=server_content, tool_call=tool_call)


async def _wait_for(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Timed out waiting for condition")


class _FakeSession:
    def __init__(self, batches: list[list[SimpleNamespace]], stop_event: asyncio.Event) -> None:
        self._batches = list(batches)
        self._stop_event = stop_event
        self.realtime_inputs: list[dict[str, Any]] = []
        self.tool_responses: list[dict[str, Any]] = []

    async def close(self) -> None:
        self._stop_event.set()

    async def send_realtime_input(self, **kwargs: Any) -> None:
        self.realtime_inputs.append(kwargs)
        return None

    async def send_tool_response(self, **kwargs: Any) -> None:
        self.tool_responses.append(kwargs)
        return None

    async def receive(self) -> AsyncIterator[SimpleNamespace]:
        if self._batches:
            for response in self._batches.pop(0):
                yield response
            return

        await self._stop_event.wait()
        return
        yield


class _FakeConnectContext:
    def __init__(self, session: _FakeSession):
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _FakeLiveClient:
    def __init__(self, session: _FakeSession) -> None:
        self.aio = SimpleNamespace(live=SimpleNamespace(connect=lambda **_kwargs: _FakeConnectContext(session)))


def _build_audio_receive_handler() -> tuple[GeminiLiveHandler, _FakeSession]:
    handler = GeminiLiveHandler(
        ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock())
    )
    session = _FakeSession([], handler._stop_event)
    handler.session = session
    return handler, session


@pytest.mark.asyncio
async def test_receive_drops_mic_frames_while_assistant_speaks_and_gate_is_closed() -> None:
    handler, session = _build_audio_receive_handler()
    handler._assistant_is_speaking = True
    handler.address_gate.fast_check = MagicMock(
        return_value=AddressResult(False, 0.0, "no_address_cues")
    )
    frame = (handler.input_sample_rate, np.zeros(160, dtype=np.int16))

    await handler.receive(frame)
    await handler.receive(frame)

    assert session.realtime_inputs == []
    handler.address_gate.fast_check.assert_called_once_with()


@pytest.mark.asyncio
async def test_receive_forwards_mic_frame_while_assistant_speaks_and_gate_is_open() -> None:
    handler, session = _build_audio_receive_handler()
    handler._assistant_is_speaking = True
    handler.address_gate.fast_check = MagicMock(
        return_value=AddressResult(True, 0.5, "front_facing")
    )

    await handler.receive((handler.input_sample_rate, np.zeros(160, dtype=np.int16)))

    assert len(session.realtime_inputs) == 1


@pytest.mark.asyncio
async def test_receive_forwards_mic_frame_while_assistant_is_idle() -> None:
    handler, session = _build_audio_receive_handler()
    handler._assistant_is_speaking = False
    handler.address_gate.fast_check = MagicMock()

    await handler.receive((handler.input_sample_rate, np.zeros(160, dtype=np.int16)))

    assert len(session.realtime_inputs) == 1
    handler.address_gate.fast_check.assert_not_called()


@pytest.mark.asyncio
async def test_receive_forwards_mic_frame_when_address_gate_errors() -> None:
    handler, session = _build_audio_receive_handler()
    handler._assistant_is_speaking = True
    handler.address_gate.fast_check = MagicMock(side_effect=RuntimeError("gate unavailable"))

    await handler.receive((handler.input_sample_rate, np.zeros(160, dtype=np.int16)))

    assert len(session.realtime_inputs) == 1


@pytest.mark.asyncio
async def test_unaddressed_interruption_does_not_clear_gemini_playback() -> None:
    """Gemini interruptions should preserve playback when the gate is closed."""
    handler = GeminiLiveHandler(
        ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock())
    )
    clear_queue = MagicMock()
    handler._clear_queue = clear_queue
    handler.address_gate.fast_check = MagicMock(
        return_value=AddressResult(False, 0.0, "no_address_cues")
    )

    await handler._handle_interruption()

    handler.address_gate.fast_check.assert_called_once_with()
    clear_queue.assert_not_called()


@pytest.mark.asyncio
async def test_addressed_interruption_clears_gemini_playback() -> None:
    """Gemini interruptions should clear playback when the gate is open."""
    handler = GeminiLiveHandler(
        ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock())
    )
    clear_queue = MagicMock()
    handler._clear_queue = clear_queue
    handler.address_gate.fast_check = MagicMock(
        return_value=AddressResult(True, 0.5, "face_detected")
    )

    await handler._handle_interruption()

    handler.address_gate.fast_check.assert_called_once_with()
    clear_queue.assert_called_once_with()


@pytest.mark.asyncio
async def test_gemini_turn_buffers_transcripts_and_schedules_motion_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini turns should emit one transcript per role and let the wobbler reset after speech."""
    monkeypatch.setattr(gemini_mod, "get_session_instructions", lambda _instance_path=None: "test")
    monkeypatch.setattr(gemini_mod, "get_session_voice", lambda: "Kore")
    monkeypatch.setattr(gemini_mod, "get_active_tool_specs", lambda _: [])

    movement_manager = MagicMock()
    movement_manager.is_idle.return_value = False
    robot = SimpleNamespace(media=SimpleNamespace(audio=None))
    deps = ToolDependencies(
        reachy_mini=robot,
        movement_manager=movement_manager,
    )
    handler = GeminiLiveHandler(deps)
    monkeypatch.setattr(type(handler.tool_manager), "start_up", MagicMock())
    monkeypatch.setattr(type(handler.tool_manager), "shutdown", AsyncMock())

    speaking_states_during_audio_output: list[bool] = []

    class RecordingOutputQueue(asyncio.Queue[Any]):
        async def put(self, item: Any) -> None:
            if isinstance(item, tuple):
                speaking_states_during_audio_output.append(handler._assistant_is_speaking)
            await super().put(item)

    handler.output_queue = RecordingOutputQueue()

    audio_bytes = b"\x00\x00\x10\x00" * 256
    session = _FakeSession(
        batches=[
            [
                _response(
                    _server_content(
                        input_transcription=SimpleNamespace(text="How's it going, Reachy?"),
                    )
                ),
                _response(
                    _server_content(
                        model_turn=SimpleNamespace(
                            parts=[SimpleNamespace(inline_data=SimpleNamespace(data=audio_bytes))]
                        ),
                    )
                ),
                _response(
                    _server_content(
                        output_transcription=SimpleNamespace(text="Doing"),
                    )
                ),
                _response(
                    _server_content(
                        output_transcription=SimpleNamespace(text=" great."),
                    )
                ),
                _response(
                    _server_content(
                        turn_complete=True,
                    )
                ),
            ]
        ],
        stop_event=handler._stop_event,
    )
    handler.client = _FakeLiveClient(session)

    task = asyncio.create_task(handler._run_live_session())
    await _wait_for(lambda: handler.output_queue.qsize() >= 3)

    handler._stop_event.set()
    await asyncio.wait_for(task, timeout=1.0)

    outputs = []
    while not handler.output_queue.empty():
        outputs.append(handler.output_queue.get_nowait())

    transcript_messages = [
        message
        for output in outputs
        if isinstance(output, AdditionalOutputs)
        for message in output.args
        if isinstance(message.get("content"), str)
    ]

    assert transcript_messages == [
        {"role": "user", "content": "How's it going, Reachy?"},
        {"role": "assistant", "content": "Doing great."},
    ]
    assert any(isinstance(output, tuple) for output in outputs), "audio output was not emitted"
    assert speaking_states_during_audio_output == [True]
    assert handler._assistant_is_speaking is False
    movement_manager.set_listening.assert_has_calls([call(True), call(False)])
    assert movement_manager.set_listening.call_args_list[-1] == call(False)


@pytest.mark.asyncio
async def test_gemini_camera_tool_sends_snapshot_and_returns_json_result() -> None:
    """Camera tool should push the snapshot via realtime video input and return a JSON-safe tool result."""
    camera_worker = MagicMock()
    camera_worker.get_latest_frame.return_value = np.zeros((8, 8, 3), dtype=np.uint8)
    deps = ToolDependencies(
        reachy_mini=MagicMock(),
        movement_manager=MagicMock(),
        camera_worker=camera_worker,
    )
    handler = GeminiLiveHandler(deps)
    session = _FakeSession([], handler._stop_event)
    handler.session = session

    await handler._handle_tool_result(
        ToolNotification(
            id="call_camera_1",
            tool_name="camera",
            is_idle_tool_call=False,
            status=ToolState.COMPLETED,
            result={"b64_im": base64.b64encode(b"jpeg-bytes").decode("ascii")},
        )
    )

    # Image pushed as a realtime video frame (not embedded in FunctionResponse)
    assert len(session.realtime_inputs) == 1
    blob = session.realtime_inputs[0]["video"]
    assert blob.data == b"jpeg-bytes"
    assert blob.mime_type == "image/jpeg"

    # Tool response is plain JSON (no binary, no parts)
    assert len(session.tool_responses) == 1
    function_response = session.tool_responses[0]["function_responses"][0]
    assert function_response.response == {"status": "image_captured"}
    assert not hasattr(function_response, "parts") or function_response.parts is None

    # Console output is JSON-safe
    outputs = []
    while not handler.output_queue.empty():
        outputs.append(handler.output_queue.get_nowait())

    tool_messages = [
        message
        for output in outputs
        if isinstance(output, AdditionalOutputs)
        for message in output.args
        if isinstance(message.get("content"), str)
    ]
    assert tool_messages == [
        {
            "role": "assistant",
            "content": '{"status": "image_captured"}',
        }
    ]


@pytest.mark.asyncio
async def test_gemini_idle_signal_starts_local_tool_without_model_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gemini idle behavior should not send an idle text turn to the model."""
    deps = ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock())
    handler = GeminiLiveHandler(deps)
    session = _FakeSession([], handler._stop_event)
    handler.session = session
    monkeypatch.setattr(
        idle_policy_mod, "choose_idle_tool_call", lambda _available: ("idle_do_nothing", {"reason": "test"})
    )
    start_tool = AsyncMock(return_value=SimpleNamespace(tool_id="idle_do_nothing-idle-1"))
    monkeypatch.setattr(type(handler.tool_manager), "start_tool", start_tool)

    await handler.send_idle_signal(16.0)

    assert session.realtime_inputs == []
    assert session.tool_responses == []
    start_tool.assert_awaited_once()
    start_kwargs = start_tool.await_args.kwargs
    assert start_kwargs["is_idle_tool_call"] is True
    assert start_kwargs["tool_call_routine"].tool_name == "idle_do_nothing"
    assert start_kwargs["tool_call_routine"].args_json_str == '{"reason": "test"}'
    outputs = []
    while not handler.output_queue.empty():
        outputs.append(handler.output_queue.get_nowait())
    tool_messages = [
        message
        for output in outputs
        if isinstance(output, AdditionalOutputs)
        for message in output.args
        if isinstance(message.get("content"), str)
    ]
    assert tool_messages == [
        {
            "role": "assistant",
            "content": (
                '🛠️ Idle tool idle_do_nothing with args {"reason": "test"}. '
                "The tool is now running. Tool ID: idle_do_nothing-idle-1"
            ),
        }
    ]


@pytest.mark.asyncio
async def test_gemini_idle_tool_result_is_not_sent_to_model() -> None:
    """Locally selected Gemini idle tool completions should not send function responses."""
    deps = ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock())
    handler = GeminiLiveHandler(deps)
    session = _FakeSession([], handler._stop_event)
    handler.session = session

    await handler._handle_tool_result(
        ToolNotification(
            id="idle-call",
            tool_name="idle_do_nothing",
            is_idle_tool_call=True,
            status=ToolState.COMPLETED,
            result={"status": "idle"},
        )
    )

    assert session.realtime_inputs == []
    assert session.tool_responses == []


@pytest.mark.asyncio
async def test_apply_personality_preserves_manual_voice_override(monkeypatch) -> None:
    """Applying a profile should keep a manually selected Gemini voice active."""
    monkeypatch.setattr(gemini_mod, "get_session_instructions", lambda _instance_path=None: "test")
    monkeypatch.setattr(gemini_mod, "get_session_voice", lambda: "Kore")
    monkeypatch.setattr("reachy_chat.config.set_custom_profile", lambda _profile: None)

    handler = GeminiLiveHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))
    handler.session = object()
    handler._voice_override = "Orus"
    restart = AsyncMock()
    monkeypatch.setattr(handler, "_restart_session", restart)

    status = await handler.apply_personality("mars_rover")

    assert status == "Applied personality and restarted Gemini session."
    assert handler.get_current_voice() == "Orus"
    restart.assert_awaited_once()


@pytest.mark.asyncio
async def test_change_voice_restart_failure_restores_previous_override_and_current_voice() -> None:
    """A failed Gemini restart must leave the previous voice selected."""
    handler = GeminiLiveHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))
    handler.client = object()
    handler._voice_override = "Orus"
    restart = AsyncMock(side_effect=RuntimeError("Gemini rejected voice"))
    handler._restart_session = restart

    status = await handler.change_voice("Zephyr")

    assert status == "Voice change failed. Will take effect on next connection."
    assert handler._voice_override == "Orus"
    assert handler.get_current_voice() == "Orus"
    restart.assert_awaited_once()


@pytest.mark.asyncio
async def test_change_voice_restart_sees_proposed_override_and_commits_it() -> None:
    """Gemini restart config must see the proposed voice before it commits."""
    handler = GeminiLiveHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))
    handler.client = object()
    handler._voice_override = "Orus"
    observed_voice: list[str] = []

    async def _restart() -> None:
        observed_voice.append(handler.get_current_voice())

    handler._restart_session = _restart

    status = await handler.change_voice("Zephyr")

    assert status == "Voice changed to Zephyr."
    assert observed_voice == ["Zephyr"]
    assert handler._voice_override == "Zephyr"
    assert handler.get_current_voice() == "Zephyr"


@pytest.mark.asyncio
async def test_change_voice_without_client_fails_and_preserves_previous_override() -> None:
    """Gemini cannot report a voice change without a client or live replacement."""
    handler = GeminiLiveHandler(
        ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()),
        startup_voice="Orus",
    )

    status = await handler.change_voice("Zephyr")

    assert status == "Voice change failed. Will take effect on next connection."
    assert handler._voice_override == "Orus"
    assert handler.get_current_voice() == "Orus"


@pytest.mark.asyncio
async def test_change_voice_production_restart_failure_restores_previous_override() -> None:
    """A startup owner failure before Gemini connects must reject the requested voice."""
    handler = GeminiLiveHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))
    handler._voice_override = "Orus"

    async def _restart(_timeout: float | None) -> None:
        raise RuntimeError("Gemini startup failed")

    handler.set_startup_owner(_restart)

    status = await handler.change_voice("Zephyr")

    assert status == "Voice change failed. Will take effect on next connection."
    assert handler._voice_override == "Orus"
    assert handler.get_current_voice() == "Orus"


@pytest.mark.asyncio
async def test_change_voice_cancellation_restores_previous_override() -> None:
    """Cancellation during an owner restart restores the exact previous override."""
    handler = GeminiLiveHandler(
        ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()),
        startup_voice="Orus",
    )

    async def _restart(_timeout: float | None) -> None:
        raise asyncio.CancelledError

    handler.set_startup_owner(_restart)

    with pytest.raises(asyncio.CancelledError):
        await handler.change_voice("Zephyr")

    assert handler._voice_override == "Orus"
    assert handler.get_current_voice() == "Orus"


@pytest.mark.asyncio
async def test_change_voice_production_restart_timeout_cancels_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A startup owner that times out must cancel its child before rejecting the voice."""
    monkeypatch.setattr(gemini_mod, "GEMINI_RESTART_TIMEOUT_SECONDS", 0.01)
    handler = GeminiLiveHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))
    handler._voice_override = "Orus"
    started = asyncio.Event()
    cancelled = asyncio.Event()
    never_connect = asyncio.Event()

    async def _start_up_without_connection() -> None:
        started.set()
        try:
            await never_connect.wait()
        finally:
            cancelled.set()

    async def _restart(timeout: float | None) -> None:
        task = asyncio.create_task(_start_up_without_connection())
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    handler.set_startup_owner(_restart)

    status = await handler.change_voice("Zephyr")

    assert started.is_set()
    assert cancelled.is_set()
    assert status == "Voice change failed. Will take effect on next connection."
    assert handler._voice_override == "Orus"
    assert handler.get_current_voice() == "Orus"


@pytest.mark.asyncio
async def test_change_voice_production_restart_success_commits_requested_voice() -> None:
    """A startup owner that connects a replacement commits the requested voice."""
    handler = GeminiLiveHandler(ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()))
    handler._voice_override = "Orus"
    replacement_stopped = asyncio.Event()
    keep_running = asyncio.Event()

    async def _start_up_and_connect() -> None:
        try:
            await keep_running.wait()
        finally:
            replacement_stopped.set()

    replacement_task: asyncio.Task[None] | None = None

    async def _restart(_timeout: float | None) -> None:
        nonlocal replacement_task
        replacement_task = asyncio.create_task(_start_up_and_connect())
        await asyncio.sleep(0)

    handler.set_startup_owner(_restart)

    status = await handler.change_voice("Zephyr")

    assert status == "Voice changed to Zephyr."
    assert handler._voice_override == "Zephyr"
    assert handler.get_current_voice() == "Zephyr"
    assert replacement_task is not None
    assert not replacement_task.done()

    replacement_task.cancel()
    await asyncio.gather(replacement_task, return_exceptions=True)
    assert replacement_stopped.is_set()


def test_handler_uses_startup_voice_at_startup() -> None:
    """Gemini handler startup should restore a persisted startup voice."""
    handler = GeminiLiveHandler(
        ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()),
        startup_voice="Orus",
    )

    assert handler.get_current_voice() == "Orus"


def test_copy_preserves_current_voice_override() -> None:
    """Copied Gemini handlers should keep the current voice override."""
    handler = GeminiLiveHandler(
        ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock()),
        startup_voice="Orus",
    )
    handler._voice_override = "Zephyr"

    copied_handler = handler.copy()

    assert copied_handler.get_current_voice() == "Zephyr"


def test_gemini_excludes_head_tracking_when_no_head_tracker(monkeypatch) -> None:
    """head_tracking tool must not appear in Gemini session config when head_tracker is not active."""
    monkeypatch.setattr(gemini_mod, "get_session_instructions", lambda _instance_path=None: "test")
    monkeypatch.setattr(gemini_mod, "get_session_voice", lambda: "Kore")

    # Mock the spec source while preserving get_active_tool_specs filtering.
    fake_tool_specs = [
        {"type": "function", "name": "head_tracking", "description": "head_tracking", "parameters": {}},
        {"type": "function", "name": "fake_tool", "description": "fake_tool", "parameters": {}},
    ]

    def fake_get_tool_specs(exclusion_list: list[str] | None = None) -> list[dict[str, object]]:
        excluded = set(exclusion_list or [])
        return [spec for spec in fake_tool_specs if spec["name"] not in excluded]

    monkeypatch.setattr(ct_mod, "get_tool_specs", fake_get_tool_specs)

    # case 1: no camera at all, --no-camera flag passed
    deps = ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock(), camera_worker=None)
    handler = GeminiLiveHandler(deps)
    live_config = handler._build_live_config()
    tool_names = [fd.name for fd in live_config.tools[0].function_declarations] if live_config.tools else []
    assert "head_tracking" not in tool_names, "case 1 failed: camera_worker=None"
    assert "fake_tool" in tool_names, "case 1 failed: a non-head-tracking tool was unexpectedly excluded"

    # case 2: camera is running but --head-tracker flag was not passed
    camera_worker = MagicMock()
    camera_worker.head_tracker = None
    deps = ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock(), camera_worker=camera_worker)
    handler = GeminiLiveHandler(deps)
    live_config = handler._build_live_config()
    tool_names = [fd.name for fd in live_config.tools[0].function_declarations] if live_config.tools else []
    assert "head_tracking" not in tool_names, "case 2 failed: camera_worker.head_tracker=None"
    assert "fake_tool" in tool_names, "case 2 failed: a non-head-tracking tool was unexpectedly excluded"
