"""Turn-based speech pipeline backed by local STT/TTS and OpenRouter chat."""

from __future__ import annotations
import os
import sys
import time
import asyncio
import logging
import tempfile
import subprocess
from typing import Any, Literal, cast
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from openai import AsyncOpenAI
from fastrtc import AdditionalOutputs
from numpy.typing import NDArray

from reachy_chat.config import OPENAI_BACKEND, config
from reachy_chat.prompts import get_session_instructions
from reachy_chat.tools.core_tools import ToolDependencies
from reachy_chat.conversation_handler import AudioFrame, HandlerOutput, ConversationHandler


logger = logging.getLogger(__name__)

Role = Literal["user", "assistant"]
Message = dict[str, str]


class OpenRouterChatHandler(ConversationHandler):
    """Discrete microphone -> Whisper -> OpenRouter -> local TTS pipeline."""

    BACKEND_PROVIDER = OPENAI_BACKEND
    SAMPLE_RATE = 16_000
    SPEECH_ENERGY_THRESHOLD = 0.015
    SILENCE_DURATION_S = 0.8
    MIN_SPEECH_DURATION_S = 0.25
    MAX_UTTERANCE_DURATION_S = 30.0
    MAX_HISTORY_MESSAGES = 10
    OUTPUT_CHUNK_SAMPLES = 3_200
    FALLBACK_REPLY = "Sorry, I couldn't answer that just now."
    SYSTEM_DEFAULT_VOICE = "system-default"

    def __init__(
        self,
        deps: ToolDependencies,
        instance_path: str | None = None,
        startup_voice: str | None = None,
    ) -> None:
        """Initialize lightweight state without loading models or audio engines."""
        super().__init__(
            expected_layout="mono",
            output_sample_rate=self.SAMPLE_RATE,
            input_sample_rate=self.SAMPLE_RATE,
        )
        self.deps = deps
        self.instance_path = instance_path
        self.output_queue: asyncio.Queue[AudioFrame | AdditionalOutputs] = asyncio.Queue()
        self.conversation_history: list[Message] = []
        self._system_prompt: str | None = None
        self._voice_override = startup_voice.strip() if startup_voice and startup_voice.strip() else None

        self._client: Any | None = None
        self.connection: Any | None = None
        self._whisper_model: Any | None = None
        self._tts_engine: Any | None = None
        self._default_voice_id: str | None = None
        self._blocking_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reachy-speech")

        self._stop_event = asyncio.Event()
        self._processing_task: asyncio.Task[None] | None = None
        self._processing_turn = False
        self._speech_active = False
        self._speech_chunks: list[NDArray[np.float32]] = []
        self._speech_samples = 0
        self._silence_samples = 0
        self.last_activity_time = time.monotonic()

    def copy(self) -> "OpenRouterChatHandler":
        """Create a fresh handler with the same dependencies and voice selection."""
        return OpenRouterChatHandler(
            self.deps,
            self.instance_path,
            startup_voice=self._voice_override,
        )

    async def start_up(self) -> None:
        """Run until shutdown while audio is handled by receive/emit."""
        await self.run()

    async def run(self) -> None:
        """Keep the discrete handler alive without eagerly loading STT or TTS."""
        self._stop_event.clear()
        self.connection = self._ensure_client()
        try:
            await self._stop_event.wait()
        finally:
            self.connection = None

    async def shutdown(self) -> None:
        """Stop active work and release client, model, and speech resources."""
        self._stop_event.set()
        self._set_listening(False)

        task = self._processing_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._processing_task = None

        client = self._client
        self._client = None
        self.connection = None
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    logger.debug("OpenRouter client close failed", exc_info=True)

        self._whisper_model = None
        if self._tts_engine is not None:
            try:
                await self._run_blocking(self._stop_tts_engine_blocking)
            except Exception:
                logger.debug("pyttsx3 shutdown failed", exc_info=True)
        self._blocking_executor.shutdown(wait=False, cancel_futures=True)
        self._drain_output_queue()

    async def receive(self, frame: AudioFrame) -> None:
        """Buffer SDK microphone frames and finalize a turn after trailing silence."""
        if self._stop_event.is_set() or self._processing_turn:
            return

        sample_rate, samples = frame
        audio = self._prepare_audio(samples, sample_rate)
        if audio.size == 0:
            return

        energy = float(np.sqrt(np.mean(np.square(audio, dtype=np.float32))))
        is_speech = energy >= self.SPEECH_ENERGY_THRESHOLD

        if is_speech:
            if not self._speech_active:
                self._speech_active = True
                self._set_listening(True)
                self._mark_activity("user_speech_started")
            self._speech_chunks.append(audio)
            self._speech_samples += audio.size
            self._silence_samples = 0
        elif self._speech_active:
            self._speech_chunks.append(audio)
            self._silence_samples += audio.size

        utterance_duration = sum(chunk.size for chunk in self._speech_chunks) / self.SAMPLE_RATE
        trailing_silence = self._silence_samples / self.SAMPLE_RATE
        should_finalize = self._speech_active and (
            trailing_silence >= self.SILENCE_DURATION_S or utterance_duration >= self.MAX_UTTERANCE_DURATION_S
        )
        if not should_finalize:
            return

        speech_duration = self._speech_samples / self.SAMPLE_RATE
        utterance = np.concatenate(self._speech_chunks) if self._speech_chunks else np.empty(0, dtype=np.float32)
        self._reset_input_buffer()
        self._set_listening(False)
        self._mark_activity("user_speech_stopped")
        if speech_duration < self.MIN_SPEECH_DURATION_S:
            return

        self._processing_turn = True
        self._processing_task = asyncio.create_task(self._process_turn(utterance), name="openrouter-chat-turn")

    async def emit(self) -> HandlerOutput:
        """Return the next transcript or TTS audio chunk for the SDK speaker loop."""
        return await self.output_queue.get()

    async def apply_personality(self, profile: str | None) -> str:
        """Apply a profile by replacing the system prompt for subsequent turns."""
        try:
            from reachy_chat.config import set_custom_profile

            set_custom_profile(profile)
            self._system_prompt = get_session_instructions(self.instance_path)
            return "Applied personality."
        except BaseException as exc:
            logger.error("Failed to apply personality %r: %s", profile, exc)
            return f"Failed to apply personality: {exc}"

    async def get_available_voices(self) -> list[str]:
        """Return local voice identifiers, loading pyttsx3 only when needed."""
        try:
            voices = await self._run_blocking(self._available_voices_blocking)
        except Exception as exc:
            logger.warning("Unable to enumerate local voices: %s", exc)
            return [self.SYSTEM_DEFAULT_VOICE]
        return [self.SYSTEM_DEFAULT_VOICE, *voices]

    def get_current_voice(self) -> str:
        """Return the configured local voice, or the platform default marker."""
        return self._voice_override or self.SYSTEM_DEFAULT_VOICE

    async def change_voice(self, voice: str) -> str:
        """Validate and select a local voice."""
        requested = voice.strip()
        if not requested:
            return "Voice change failed: voice cannot be empty."
        try:
            resolved = await self._run_blocking(self._select_voice_blocking, requested)
        except Exception as exc:
            logger.warning("Unable to change local voice to %r: %s", requested, exc)
            return f"Voice change failed: {exc}"
        self._voice_override = None if resolved == self.SYSTEM_DEFAULT_VOICE else resolved
        return f"Voice changed to {resolved}."

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        api_key = (config.OPENROUTER_API_KEY or os.getenv("OPENROUTER_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not configured")
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=config.OPENROUTER_BASE_URL,
        )
        return self._client

    async def _process_turn(self, audio: NDArray[np.float32]) -> None:
        """Run one complete STT, LLM, TTS turn."""
        self._processing_turn = True
        try:
            try:
                transcript = (await self._transcribe(audio)).strip()
            except Exception as exc:
                logger.exception("Speech transcription failed: %s", exc)
                await self.output_queue.put((self.SAMPLE_RATE, np.zeros(1_600, dtype=np.int16)))
                return
            if not transcript:
                logger.debug("Ignoring empty transcription")
                return

            self._record_message("user", transcript)
            await self.output_queue.put(AdditionalOutputs({"role": "user", "content": transcript}))
            self._mark_activity("user_transcription_completed")

            try:
                reply = await self._request_completion()
            except Exception as exc:
                logger.exception("OpenRouter chat completion failed: %s", exc)
                reply = self.FALLBACK_REPLY

            self._record_message("assistant", reply)
            await self.output_queue.put(AdditionalOutputs({"role": "assistant", "content": reply}))
            self._mark_activity("assistant_transcript_done")

            try:
                sample_rate, speech = await self._synthesize(reply)
            except Exception as exc:
                logger.exception("Text-to-speech synthesis failed: %s", exc)
                return

            for offset in range(0, speech.size, self.OUTPUT_CHUNK_SAMPLES):
                await self.output_queue.put((sample_rate, speech[offset : offset + self.OUTPUT_CHUNK_SAMPLES]))
            if speech.size:
                self._mark_activity("assistant_audio_delta")
                await asyncio.sleep(speech.size / sample_rate)
        finally:
            self._processing_turn = False
            self._reset_input_buffer()

    async def _request_completion(self) -> str:
        client = self._ensure_client()
        if self._system_prompt is None:
            self._system_prompt = get_session_instructions(self.instance_path)
        messages: list[Message] = [
            {"role": "system", "content": self._system_prompt},
            *self.conversation_history,
        ]
        response = await client.chat.completions.create(
            model=config.MODEL_NAME,
            messages=messages,
        )
        content = response.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("OpenRouter returned an empty assistant response")
        return content.strip()

    def _record_message(self, role: Role, content: str) -> None:
        self.conversation_history.append({"role": role, "content": content})
        del self.conversation_history[: -self.MAX_HISTORY_MESSAGES]

    async def _transcribe(self, audio: NDArray[np.float32]) -> str:
        return cast(str, await self._run_blocking(self._transcribe_blocking, audio))

    def _transcribe_blocking(self, audio: NDArray[np.float32]) -> str:
        if self._whisper_model is None:
            from faster_whisper import WhisperModel

            model_name = os.getenv("WHISPER_MODEL", "base.en")
            device = os.getenv("WHISPER_DEVICE", "cpu")
            compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
            logger.info("Loading faster-whisper model %s on %s", model_name, device)
            self._whisper_model = WhisperModel(model_name, device=device, compute_type=compute_type)
        segments, _ = self._whisper_model.transcribe(
            audio,
            beam_size=5,
            vad_filter=True,
        )
        return " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()

    async def _synthesize(self, text: str) -> tuple[int, NDArray[np.int16]]:
        return cast(
            tuple[int, NDArray[np.int16]],
            await self._run_blocking(self._synthesize_blocking, text),
        )

    def _synthesize_blocking(self, text: str) -> tuple[int, NDArray[np.int16]]:
        if sys.platform == "darwin":
            return self._synthesize_with_say_blocking(text)

        engine = self._ensure_tts_engine_blocking()
        self._apply_voice_override_blocking(engine)
        descriptor, filename = tempfile.mkstemp(prefix="reachy-chat-", suffix=".wav")
        os.close(descriptor)
        audio_path = Path(filename)
        try:
            engine.save_to_file(text, str(audio_path))
            engine.runAndWait()
            if not audio_path.exists() or audio_path.stat().st_size == 0:
                raise RuntimeError("pyttsx3 did not produce an audio file")
            return self._decode_audio_blocking(audio_path)
        finally:
            audio_path.unlink(missing_ok=True)

    def _synthesize_with_say_blocking(self, text: str) -> tuple[int, NDArray[np.int16]]:
        self._apply_voice_override_blocking()
        decode_error: Exception | None = None

        for suffix, format_options in (
            (".aiff", []),
            (".wav", ["--file-format=WAVE", "--data-format=LEI16@16000"]),
        ):
            descriptor, filename = tempfile.mkstemp(prefix="reachy-chat-", suffix=suffix)
            os.close(descriptor)
            audio_path = Path(filename)
            command = ["say"]
            if self._voice_override:
                command.extend(["-v", self._voice_override])
            command.extend(["-o", str(audio_path), *format_options, text])
            try:
                try:
                    subprocess.run(command, check=True, capture_output=True, text=True)
                except FileNotFoundError as exc:
                    raise RuntimeError(
                        "macOS 'say' command is unavailable; expected it at /usr/bin/say"
                    ) from exc
                except subprocess.CalledProcessError as exc:
                    detail = (exc.stderr or exc.stdout or "").strip()
                    message = "macOS 'say' command failed"
                    if detail:
                        message = f"{message}: {detail}"
                    else:
                        message = f"{message} with exit code {exc.returncode}"
                    raise RuntimeError(message) from exc

                if not audio_path.exists() or audio_path.stat().st_size == 0:
                    raise RuntimeError(f"macOS 'say' did not produce a non-empty {suffix} audio file")
                try:
                    return self._decode_audio_blocking(audio_path)
                except Exception as exc:
                    decode_error = exc
                    if suffix == ".aiff":
                        logger.warning("Unable to decode macOS say AIFF output; retrying as WAV: %s", exc)
            finally:
                audio_path.unlink(missing_ok=True)

        raise RuntimeError("macOS 'say' output could not be decoded as AIFF or WAV") from decode_error

    def _decode_audio_blocking(self, audio_path: Path) -> tuple[int, NDArray[np.int16]]:
        import av

        chunks: list[NDArray[np.int16]] = []
        resampler = av.AudioResampler(format="s16", layout="mono", rate=self.SAMPLE_RATE)
        with av.open(str(audio_path)) as container:
            stream = container.streams.audio[0]
            for frame in container.decode(stream):
                for converted in resampler.resample(frame):
                    chunks.append(np.asarray(converted.to_ndarray(), dtype=np.int16).reshape(-1))
            for converted in resampler.resample(None):
                chunks.append(np.asarray(converted.to_ndarray(), dtype=np.int16).reshape(-1))
        if not chunks:
            raise RuntimeError("Speech synthesizer produced no decodable audio")
        return self.SAMPLE_RATE, np.concatenate(chunks)

    def _ensure_tts_engine_blocking(self) -> Any:
        if self._tts_engine is None:
            import pyttsx3

            self._tts_engine = pyttsx3.init()
            current_voice = self._tts_engine.getProperty("voice")
            self._default_voice_id = str(current_voice) if current_voice else None
        return self._tts_engine

    def _stop_tts_engine_blocking(self) -> None:
        if self._tts_engine is not None:
            self._tts_engine.stop()
            self._tts_engine = None

    def _available_voices_blocking(self) -> list[str]:
        if sys.platform == "darwin":
            try:
                return self._say_voices_blocking()
            except FileNotFoundError:
                logger.warning("macOS 'say' command is unavailable; falling back to pyttsx3 voice enumeration")

        engine = self._ensure_tts_engine_blocking()
        return [str(voice.id) for voice in engine.getProperty("voices")]

    def _say_voices_blocking(self) -> list[str]:
        try:
            result = subprocess.run(
                ["say", "-v", "?"],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            message = "macOS 'say -v ?' command failed"
            if detail:
                message = f"{message}: {detail}"
            else:
                message = f"{message} with exit code {exc.returncode}"
            raise RuntimeError(message) from exc

        voices: list[str] = []
        for line in result.stdout.splitlines():
            metadata, separator, _description = line.partition("#")
            if not separator:
                continue
            fields = metadata.strip().rsplit(maxsplit=1)
            if len(fields) == 2 and fields[0]:
                voices.append(fields[0])
        if not voices:
            raise RuntimeError("macOS 'say -v ?' returned no voice names")
        return voices

    def _select_voice_blocking(self, requested: str) -> str:
        if sys.platform == "darwin":
            if requested == self.SYSTEM_DEFAULT_VOICE:
                return self.SYSTEM_DEFAULT_VOICE
            try:
                voices = self._say_voices_blocking()
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "macOS 'say' command is unavailable; expected it at /usr/bin/say"
                ) from exc
            for voice_name in voices:
                if requested.casefold() == voice_name.casefold():
                    return voice_name
            raise ValueError(f"Unknown voice: {requested}")

        engine = self._ensure_tts_engine_blocking()
        if requested == self.SYSTEM_DEFAULT_VOICE:
            if self._default_voice_id:
                engine.setProperty("voice", self._default_voice_id)
            return self.SYSTEM_DEFAULT_VOICE
        for voice in engine.getProperty("voices"):
            voice_id = str(voice.id)
            voice_name = str(getattr(voice, "name", ""))
            if requested.casefold() in {voice_id.casefold(), voice_name.casefold()}:
                engine.setProperty("voice", voice_id)
                return voice_id
        raise ValueError(f"Unknown voice: {requested}")

    def _apply_voice_override_blocking(self, engine: Any | None = None) -> None:
        if not self._voice_override:
            return
        try:
            resolved = self._select_voice_blocking(self._voice_override)
        except ValueError:
            logger.warning("Configured local voice %r was not found; using system default", self._voice_override)
            self._voice_override = None
            if sys.platform != "darwin" and engine is not None and self._default_voice_id:
                engine.setProperty("voice", self._default_voice_id)
        else:
            self._voice_override = resolved

    async def _run_blocking(self, function: Any, *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._blocking_executor, function, *args)

    def _prepare_audio(self, samples: NDArray[np.int16], sample_rate: int) -> NDArray[np.float32]:
        audio = np.asarray(samples)
        if audio.ndim == 2:
            if audio.shape[0] <= 8 and audio.shape[1] > audio.shape[0]:
                audio = audio.T
            audio = audio.mean(axis=1)
        elif audio.ndim != 1:
            return np.empty(0, dtype=np.float32)

        if np.issubdtype(audio.dtype, np.integer):
            maximum = float(np.iinfo(audio.dtype).max)
            normalized = audio.astype(np.float32) / maximum
        else:
            normalized = audio.astype(np.float32, copy=False)
        normalized = np.clip(normalized, -1.0, 1.0)

        if sample_rate <= 0 or sample_rate == self.SAMPLE_RATE or normalized.size == 0:
            return np.ascontiguousarray(normalized, dtype=np.float32)
        output_size = max(1, round(normalized.size * self.SAMPLE_RATE / sample_rate))
        source_positions = np.arange(normalized.size, dtype=np.float64)
        target_positions = np.linspace(0, normalized.size, output_size, endpoint=False)
        return np.ascontiguousarray(np.interp(target_positions, source_positions, normalized), dtype=np.float32)

    def _set_listening(self, listening: bool) -> None:
        try:
            self.deps.movement_manager.set_listening(listening)
        except Exception:
            logger.debug("Unable to update listening motion state", exc_info=True)

    def _mark_activity(self, reason: str) -> None:
        self.last_activity_time = time.monotonic()
        observer = self._activity_observer
        if observer is not None:
            try:
                observer(reason)
            except Exception:
                logger.debug("Activity observer raised", exc_info=True)

    def _reset_input_buffer(self) -> None:
        self._speech_active = False
        self._speech_chunks.clear()
        self._speech_samples = 0
        self._silence_samples = 0

    def _drain_output_queue(self) -> None:
        while not self.output_queue.empty():
            try:
                self.output_queue.get_nowait()
            except asyncio.QueueEmpty:
                break


__all__ = ["OpenRouterChatHandler"]
