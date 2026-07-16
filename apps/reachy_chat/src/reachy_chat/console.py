"""Bidirectional local audio stream with optional web settings UI.

If the selected backend is missing its required API key, a settings page is
served via the Reachy Mini Apps settings server so users can configure it.
"""

import os
import sys
import time
import asyncio
import logging
import threading
from inspect import isawaitable
from typing import Any, Awaitable, List, Optional
from pathlib import Path
from collections.abc import Callable, AsyncGenerator

import numpy as np
from fastrtc import AdditionalOutputs, audio_to_float32
from scipy.signal import resample

from reachy_mini import ReachyMini
from reachy_mini.media.media_manager import MediaBackend
from reachy_chat.config import (
    HF_BACKEND,
    GEMINI_BACKEND,
    LOCKED_PROFILE,
    OPENAI_BACKEND,
    OPENAI_REALTIME_BACKEND,
    HF_REALTIME_WS_URL_ENV,
    HF_LOCAL_CONNECTION_MODE,
    HF_DEPLOYED_CONNECTION_MODE,
    HF_REALTIME_CONNECTION_MODE_ENV,
    config,
    get_backend_choice,
    get_hf_session_url,
    get_hf_direct_ws_url,
    build_hf_direct_ws_url,
    has_hf_realtime_target,
    parse_hf_direct_target,
    get_model_name_for_backend,
    get_hf_connection_selection,
    get_default_voice_for_backend,
    refresh_runtime_config_from_env,
    get_available_voices_for_backend,
)
from reachy_chat.startup_settings import read_startup_settings, write_startup_settings
from reachy_chat.personality_routes import mount_personality_routes
from reachy_chat.audio.startup_config import apply_audio_startup_config
from reachy_chat.conversation_handler import ConversationHandler


try:
    # FastAPI is provided by the Reachy Mini Apps runtime
    from fastapi import FastAPI, Request, Response
    from pydantic import BaseModel
    from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
    from starlette.staticfiles import StaticFiles
except Exception:  # pragma: no cover - only loaded when settings_app is used
    FastAPI = object  # type: ignore
    FileResponse = object  # type: ignore
    JSONResponse = object  # type: ignore
    StreamingResponse = object  # type: ignore
    StaticFiles = object  # type: ignore
    BaseModel = object  # type: ignore
    Request = object  # type: ignore

logger = logging.getLogger(__name__)

class VoiceChangeError(RuntimeError):
    """Raised when a handler did not verify the requested voice change."""

_SSE_KEEPALIVE_INTERVAL_SECONDS = 15.0  # below typical 60s proxy idle timeout


def _detach_framework_root_routes(app: "FastAPI") -> None:
    """Strip framework-registered GET / and /static routes so ours aren't silently shadowed."""
    routes = getattr(app, "router", None)
    routes = getattr(routes, "routes", None) if routes else getattr(app, "routes", None)
    if routes is None:
        return
    survivors = []
    for route in routes:
        path = getattr(route, "path", None)
        if path in ("/", "/static"):
            logger.debug("detaching framework-provided route %r (%s)", path, type(route).__name__)
            continue
        survivors.append(route)
    routes[:] = survivors


class ConversationEventBus:
    """Thread-safe fan-out bus that delivers conversation activity events to SSE subscribers."""

    MAX_QUEUE_SIZE = 64

    def __init__(self) -> None:
        """Initialize the bus."""
        # Each entry is (loop, queue); loop captured at subscribe() time for cross-thread delivery.
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, "asyncio.Queue[str]"]] = []
        self._lock = threading.Lock()

    def subscribe(self) -> tuple["asyncio.Queue[str]", Callable[[], None]]:
        """Return a per-subscriber queue and its unsubscribe callback. Must be called from a coroutine."""
        loop = asyncio.get_running_loop()
        queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=self.MAX_QUEUE_SIZE)
        entry = (loop, queue)
        with self._lock:
            self._subscribers.append(entry)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(entry)
                except ValueError:
                    pass

        return queue, unsubscribe

    def publish(self, event: str) -> None:
        """Broadcast to all subscribers via call_soon_threadsafe. Drops events for full or closed queues."""
        with self._lock:
            snapshot = list(self._subscribers)
        for loop, queue in snapshot:
            try:
                loop.call_soon_threadsafe(self._enqueue_safely, queue, event)
            except RuntimeError:
                pass  # loop closed; subscriber will clean itself up on disconnect

    @staticmethod
    def _enqueue_safely(queue: "asyncio.Queue[str]", event: str) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.debug("conversation event dropped (subscriber queue full): %s", event)


async def _conversation_events_stream(
    bus: ConversationEventBus,
    request: "Request",
) -> "AsyncGenerator[str, None]":
    """Yield SSE lines for one subscriber; emits keep-alive comments during idle periods."""
    queue, unsubscribe = bus.subscribe()
    try:
        yield "retry: 2000\n\n"
        yield "event: ready\ndata: connected\n\n"

        while True:
            if await request.is_disconnected():
                return
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield f"event: activity\ndata: {event}\n\n"
    finally:
        unsubscribe()


LOCAL_PLAYER_BACKEND = (
    getattr(MediaBackend, "LOCAL", None)
    or getattr(MediaBackend, "GSTREAMER", None)
    or getattr(MediaBackend, "DEFAULT", None)
)

HandlerFactory = Callable[[Optional[str]], ConversationHandler]

LEGACY_STARTUP_ENV_NAMES = (
    "REACHY_MINI_CUSTOM_PROFILE",
    "REACHY_MINI_VOICE_OVERRIDE",
)
BACKEND_RETRY_DELAY_SECONDS = 5.0


class _StartupGeneration:
    """LocalStream-owned lifecycle state for one handler.start_up() child."""

    def __init__(self, handler: ConversationHandler, generation: int) -> None:
        loop = asyncio.get_running_loop()
        self.handler = handler
        self.generation = generation
        self.connection_result: asyncio.Future[None] = loop.create_future()
        self.replaced: asyncio.Future[None] = loop.create_future()
        self.task: asyncio.Task[None] | None = None


class LocalStream:
    """LocalStream using Reachy Mini's recorder/player."""

    def __init__(
        self,
        handler: ConversationHandler,
        robot: ReachyMini,
        *,
        settings_app: Optional[FastAPI] = None,
        instance_path: Optional[str] = None,
        handler_factory: HandlerFactory | None = None,
        startup_voice: Optional[str] = None,
    ):
        """Initialize the stream with a realtime handler and pipelines.

        - ``settings_app``: the Reachy Mini Apps FastAPI to attach settings endpoints.
        - ``instance_path``: directory where per-instance ``.env`` should be stored.
        - ``handler_factory``: builds a fresh handler for the currently selected backend.
        """
        self._robot = robot
        self._stop_event = asyncio.Event()
        self._restart_requested = asyncio.Event()
        self._tasks: List[asyncio.Task[None]] = []
        self._handler_factory = handler_factory
        self._voice_override = startup_voice
        self._settings_app: Optional[FastAPI] = settings_app
        self._instance_path: Optional[str] = instance_path
        self._settings_initialized = False
        self._personality_routes_initialized = False
        self._asyncio_loop = None
        self._mic_muted = False  # mic starts live; the UI toggles it via /mic
        self._active_backend_name = get_backend_choice()
        self._backend_connection_state = "not_started"
        self._backend_error: str | None = None
        self._backend_retry_delay = BACKEND_RETRY_DELAY_SECONDS
        self._startup_generation: _StartupGeneration | None = None
        self._startup_generation_counter = 0
        self._startup_transition_active = False
        self._startup_transition_done = asyncio.Event()
        self._startup_transition_done.set()
        self._startup_transition_lock = asyncio.Lock()
        self._startup_retry_pending = False

        # One bus for the stream's lifetime: the /conversation_events route
        # closes over it, so handler rebuilds must keep publishing into it.
        self._event_bus = ConversationEventBus()
        self._install_handler(handler)

    def _install_handler(self, handler: ConversationHandler) -> None:
        """Set the active handler and wire LocalStream-owned helpers into it."""
        previous = getattr(self, "handler", None)
        if previous is not None and previous is not handler:
            setter = getattr(previous, "set_startup_owner", None)
            if callable(setter):
                setter(None)
            callback_setter = getattr(previous, "set_startup_connection_callback", None)
            if callable(callback_setter):
                callback_setter(None)
        self.handler = handler
        self.handler._clear_queue = self.clear_audio_queue
        setter = getattr(handler, "set_startup_owner", None)
        if callable(setter):
            setter(lambda timeout=None: self._restart_handler_generation(handler, timeout))
        self._attach_event_bus_to_handler()

    def _set_generation_connection_callback(
        self,
        generation: _StartupGeneration,
    ) -> None:
        """Give a handler a generation-bound connection callback, when supported."""
        setter = getattr(generation.handler, "set_startup_connection_callback", None)
        if callable(setter):
            setter(lambda: self._mark_generation_connected(generation))

    def _mark_generation_connected(self, generation: _StartupGeneration) -> None:
        """Resolve only the currently owned generation's connection result."""
        if self._startup_generation is not generation or generation.connection_result.done():
            return
        generation.connection_result.set_result(None)
        self._set_backend_connection_state("connected")

    def _mark_generation_failed(self, generation: _StartupGeneration, error: BaseException) -> None:
        """Resolve a generation's readiness result with its own startup failure."""
        if self._startup_generation is generation:
            self._set_backend_connection_state("disconnected", error)
        if not generation.connection_result.done():
            generation.connection_result.set_exception(error)

    async def _run_startup_generation(self, generation: _StartupGeneration) -> None:
        """Run exactly one handler.start_up() invocation for a generation."""
        try:
            await generation.handler.start_up()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._mark_generation_failed(generation, error)
            raise
        else:
            if not generation.connection_result.done():
                self._mark_generation_failed(
                    generation,
                    RuntimeError("handler startup ended before connecting"),
                )

    def _create_startup_generation(self) -> _StartupGeneration:
        """Create and schedule one LocalStream-owned startup child."""
        self._startup_generation_counter += 1
        generation = _StartupGeneration(self.handler, self._startup_generation_counter)
        self._startup_generation = generation
        self._set_generation_connection_callback(generation)
        generation.task = asyncio.create_task(
            self._run_startup_generation(generation),
            name=f"handler-startup-{generation.generation}",
        )
        return generation

    def _infer_generation_connected(self, generation: _StartupGeneration) -> None:
        """Support existing non-Gemini handlers that expose a live connection directly."""
        if self._backend_connected():
            self._mark_generation_connected(generation)

    async def _wait_generation_connected(self, generation: _StartupGeneration) -> None:
        """Wait for this generation's readiness, never a shared handler event."""
        while True:
            if self._startup_generation is not generation:
                raise RuntimeError("handler startup generation was replaced")
            self._infer_generation_connected(generation)
            if generation.connection_result.done():
                return await generation.connection_result
            task = generation.task
            if task is not None and task.done():
                try:
                    await task
                except asyncio.CancelledError as error:
                    raise RuntimeError("handler startup was cancelled before connecting") from error
                except Exception as error:
                    raise error
                raise RuntimeError("handler startup ended before connecting")
            await asyncio.sleep(0.01)

    async def _stop_startup_generation(
        self,
        generation: _StartupGeneration,
        *,
        shutdown_handler: bool = True,
        timeout: float | None = None,
    ) -> None:
        """Stop, cancel, and await one exact startup child."""
        owner_task = asyncio.current_task()
        cancellation_count = owner_task.cancelling() if owner_task is not None else 0
        cancelled_error: asyncio.CancelledError | None = None
        shutdown_error: TimeoutError | None = None
        shutdown_task: asyncio.Task[Any] | None = None
        if shutdown_handler:
            try:
                shutdown_result = generation.handler.shutdown()
                if isawaitable(shutdown_result):
                    shutdown_task = asyncio.create_task(shutdown_result, name="handler-shutdown")
                    if timeout is None:
                        await shutdown_task
                    else:
                        try:
                            await asyncio.wait_for(asyncio.shield(shutdown_task), timeout=timeout)
                        except asyncio.TimeoutError:
                            shutdown_task.cancel()
                            await asyncio.gather(shutdown_task, return_exceptions=True)
                            shutdown_error = TimeoutError("handler shutdown timed out")
            except asyncio.CancelledError as error:
                cancelled_error = error
                if shutdown_task is not None and not shutdown_task.done():
                    shutdown_task.cancel()
                    await asyncio.gather(shutdown_task, return_exceptions=True)
            except Exception as error:
                logger.debug("Handler shutdown ignored during startup replacement: %s", error)

        task = generation.task
        if task is not None and task is not owner_task:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError as error:
                if owner_task is not None and owner_task.cancelling() > cancellation_count:
                    cancelled_error = error
            except Exception as error:
                logger.debug("Startup child stopped with an error: %s", error)
        if generation.connection_result.done():
            try:
                generation.connection_result.exception()
            except BaseException:
                pass
        callback_setter = getattr(generation.handler, "set_startup_connection_callback", None)
        if callable(callback_setter):
            callback_setter(None)
        if cancelled_error is not None:
            raise cancelled_error
        if shutdown_error is not None:
            raise shutdown_error

    async def _restart_handler_generation(
        self,
        handler: ConversationHandler,
        timeout: float | None = None,
    ) -> None:
        """Replace a handler child under LocalStream's sole lifecycle ownership."""
        if self.handler is not handler:
            raise RuntimeError("handler is no longer active")
        timeout = 5.0 if timeout is None else timeout
        async with self._startup_transition_lock:
            generation = self._startup_generation
            if generation is None or generation.handler is not handler or generation.task is None:
                raise RuntimeError("handler startup is not owned by LocalStream")
            if generation.task.done():
                raise RuntimeError("handler startup ended before replacement")

            self._startup_transition_active = True
            self._startup_transition_done.clear()
            if not generation.replaced.done():
                generation.replaced.set_result(None)
            try:
                try:
                    await self._stop_startup_generation(generation, timeout=timeout)
                except BaseException:
                    if self._startup_generation is generation:
                        self._startup_generation = None
                        self._startup_retry_pending = True
                    raise

                replacement = self._create_startup_generation()
                await asyncio.sleep(0)
                self._infer_generation_connected(replacement)
                try:
                    await asyncio.wait_for(
                        self._wait_generation_connected(replacement),
                        timeout=timeout,
                    )
                except BaseException:
                    self._startup_retry_pending = True
                    try:
                        await self._stop_startup_generation(replacement, timeout=timeout)
                    finally:
                        if self._startup_generation is replacement:
                            self._startup_generation = None
                    raise
            finally:
                self._startup_transition_active = False
                self._startup_transition_done.set()
    async def _shutdown_active_handler(self) -> None:
        """Stop the active handler and its LocalStream-owned startup child."""
        generation = self._startup_generation
        if generation is not None:
            await self._stop_startup_generation(generation, timeout=5.0)
            return
        try:
            result = self.handler.shutdown()
            if isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.debug("Active handler shutdown ignored during restart: %s", error)

    def _attach_event_bus_to_handler(self) -> None:
        """Wire the event bus as the handler's activity observer, if supported."""
        setter = getattr(self.handler, "set_activity_observer", None)
        if callable(setter):
            setter(self._event_bus.publish)

    def _read_env_lines(self, env_path: Path) -> list[str]:
        """Load env file contents or a template as a list of lines."""
        inst = env_path.parent
        try:
            if env_path.exists():
                try:
                    return env_path.read_text(encoding="utf-8").splitlines()
                except Exception:
                    return []
            template_text = None
            ex = inst / ".env.example"
            if ex.exists():
                try:
                    template_text = ex.read_text(encoding="utf-8")
                except Exception:
                    template_text = None
            if template_text is None:
                try:
                    cwd_example = Path.cwd() / ".env.example"
                    if cwd_example.exists():
                        template_text = cwd_example.read_text(encoding="utf-8")
                except Exception:
                    template_text = None
            if template_text is None:
                packaged = Path(__file__).parent / ".env.example"
                if packaged.exists():
                    try:
                        template_text = packaged.read_text(encoding="utf-8")
                    except Exception:
                        template_text = None
            return template_text.splitlines() if template_text else []
        except Exception:
            return []

    def _active_backend(self) -> str:
        """Return the backend family of the currently running handler."""
        return self._active_backend_name

    def _backend_connected(self) -> bool:
        """Return whether the active handler currently has a realtime connection."""
        try:
            handler_state = vars(self.handler)
        except TypeError:
            handler_state = {}
        return any(handler_state.get(attr) is not None for attr in ("connection", "session"))

    def _can_rebuild_handler(self) -> bool:
        """Return whether LocalStream can construct handlers for backend changes."""
        return self._handler_factory is not None

    def _build_handler_for_current_backend(self) -> ConversationHandler:
        """Create and install a fresh handler for the current runtime backend config."""
        if self._handler_factory is None:
            return self.handler
        handler = self._handler_factory(self._voice_override)
        self._install_handler(handler)
        self._active_backend_name = get_backend_choice()
        return handler


    def _mark_restart_requested(self, reason: str) -> None:
        """Request a backend restart from a synchronous route handler."""
        logger.info("Backend restart requested: %s", reason)
        self._set_backend_connection_state("connecting")
        loop = self._asyncio_loop
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(self.request_backend_restart(reason), loop)
            return
        self._restart_requested.set()

    async def request_backend_restart(self, reason: str) -> None:
        """Ask the startup loop to rebuild the backend and stop the current handler."""
        self._set_backend_connection_state("connecting")
        self._restart_requested.set()
        await self._shutdown_active_handler()

    async def _sleep_or_restart_requested(self, delay: float) -> None:
        """Sleep for a retry interval, waking early if a restart is requested."""
        if self._restart_requested.is_set():
            return
        try:
            await asyncio.wait_for(self._restart_requested.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    @staticmethod
    def _format_backend_error(error: BaseException | str) -> str:
        """Return a compact user-facing backend error string."""
        if isinstance(error, str):
            return error
        message = str(error).strip()
        if message:
            return f"{type(error).__name__}: {message}"
        return type(error).__name__

    def _set_backend_connection_state(self, state: str, error: BaseException | str | None = None) -> None:
        """Update backend connection status exposed through the settings UI."""
        self._backend_connection_state = state
        if error is not None:
            self._backend_error = self._format_backend_error(error)
        elif state != "disconnected":
            self._backend_error = None

    def _backend_connection_status(self) -> dict[str, object]:
        """Return the backend connection state exposed in /status."""
        connected = self._backend_connected()
        state = "connected" if connected else self._backend_connection_state
        return {
            "backend_connected": connected,
            "backend_connection_state": state,
            "backend_error": None if connected else self._backend_error,
        }

    @staticmethod
    def _has_key(value: Optional[str]) -> bool:
        """Return whether a runtime credential value is present."""
        return bool(value and str(value).strip())

    def _has_required_key(self, backend: str) -> bool:
        """Return whether the requested backend has its required credential."""
        if backend == GEMINI_BACKEND:
            return self._has_key(config.GEMINI_API_KEY)
        if backend == HF_BACKEND:
            return has_hf_realtime_target()
        if backend == OPENAI_REALTIME_BACKEND:
            return self._has_key(config.OPENAI_API_KEY)
        return self._has_key(config.OPENROUTER_API_KEY)

    @staticmethod
    def _requirement_name(backend: str) -> str:
        """Return the env var users need for a backend, if any."""
        if backend == GEMINI_BACKEND:
            return "GEMINI_API_KEY"
        if backend == HF_BACKEND:
            return HF_REALTIME_WS_URL_ENV
        if backend == OPENAI_REALTIME_BACKEND:
            return "OPENAI_API_KEY"
        return "OPENROUTER_API_KEY"

    def _persist_env_value(self, env_name: str, value: str) -> None:
        """Persist a non-empty environment value in memory and in the instance `.env`."""
        self._persist_env_values({env_name: value})

    def _persist_env_values(self, updates: dict[str, str]) -> None:
        """Persist non-empty environment values in memory and in the instance `.env`."""
        normalized_updates = {name: (value or "").strip() for name, value in updates.items()}
        normalized_updates = {name: value for name, value in normalized_updates.items() if value}
        if not normalized_updates:
            return

        for env_name, value in normalized_updates.items():
            try:
                os.environ[env_name] = value
            except Exception:
                pass
        refresh_runtime_config_from_env()

        if not self._instance_path:
            return
        try:
            inst = Path(self._instance_path)
            env_path = inst / ".env"
            lines = self._read_env_lines(env_path)
            for env_name, value in normalized_updates.items():
                replaced = False
                for i, ln in enumerate(lines):
                    if ln.strip().startswith(f"{env_name}="):
                        lines[i] = f"{env_name}={value}"
                        replaced = True
                        break
                if not replaced:
                    lines.append(f"{env_name}={value}")
            final_text = "\n".join(lines) + "\n"
            env_path.write_text(final_text, encoding="utf-8")
            logger.info("Persisted %s to %s", ", ".join(sorted(normalized_updates)), env_path)

            try:
                from dotenv import load_dotenv

                load_dotenv(dotenv_path=str(env_path))
            except Exception:
                pass
            refresh_runtime_config_from_env()
        except Exception as e:
            logger.warning("Failed to persist %s: %s", ", ".join(sorted(normalized_updates)), e)

    def _remove_persisted_env_values(self, env_names: tuple[str, ...]) -> None:
        """Remove keys from the instance `.env` without mutating the current runtime."""
        normalized_names = tuple(sorted({name.strip() for name in env_names if name and name.strip()}))
        if not normalized_names or not self._instance_path:
            return

        env_path = Path(self._instance_path) / ".env"
        if not env_path.exists():
            return

        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
            filtered_lines = [
                line
                for line in lines
                if not any(line.strip().startswith(f"{env_name}=") for env_name in normalized_names)
            ]
            if filtered_lines == lines:
                return

            final_text = "\n".join(filtered_lines)
            if final_text:
                final_text += "\n"
            env_path.write_text(final_text, encoding="utf-8")
            logger.info("Removed %s from %s", ", ".join(normalized_names), env_path)
        except Exception as e:
            logger.warning("Failed to remove %s: %s", ", ".join(normalized_names), e)

    def _persist_hf_direct_connection(self, host: str, port: int) -> None:
        """Persist a direct Hugging Face websocket target."""
        self._persist_env_values(
            {
                HF_REALTIME_CONNECTION_MODE_ENV: HF_LOCAL_CONNECTION_MODE,
                HF_REALTIME_WS_URL_ENV: build_hf_direct_ws_url(host, port),
            }
        )

    def _persist_hf_allocator_connection(self) -> None:
        """Persist the deployed Hugging Face allocator mode."""
        self._persist_env_value(HF_REALTIME_CONNECTION_MODE_ENV, HF_DEPLOYED_CONNECTION_MODE)
        self._remove_persisted_env_values(("HF_REALTIME_SESSION_URL",))

    def _persist_api_key(self, key: str, backend: str | None = None) -> None:
        """Persist API key for the appropriate backend to environment and instance `.env`."""
        if backend == OPENAI_REALTIME_BACKEND:
            self._persist_env_value("OPENAI_API_KEY", key)
        else:
            self._persist_env_value("OPENROUTER_API_KEY", key)

    def _persist_gemini_api_key(self, key: str) -> None:
        """Persist GEMINI_API_KEY to environment and instance `.env`."""
        self._persist_env_value("GEMINI_API_KEY", key)

    def _persist_backend_choice(self, backend: str) -> None:
        """Persist the selected backend without clobbering explicit model overrides."""
        current_backend = get_backend_choice()
        current_model_name = (os.getenv("MODEL_NAME") or "").strip()
        updates = {"BACKEND_PROVIDER": backend}
        if backend == HF_BACKEND:
            self._persist_env_values(updates)
            try:
                os.environ.pop("MODEL_NAME", None)
            except Exception:
                pass
            self._remove_persisted_env_values(("MODEL_NAME",))
            refresh_runtime_config_from_env()
            return

        # Preserve an explicit model override only when re-persisting the same
        # backend; switching backends resets to the new backend's default model
        # (models are backend-specific and don't carry across).
        if (
            backend == current_backend
            and current_model_name
            and current_model_name != get_model_name_for_backend(current_backend)
        ):
            updates["MODEL_NAME"] = current_model_name
        else:
            updates["MODEL_NAME"] = get_model_name_for_backend(backend)
        self._persist_env_values(updates)

    def _persist_personality(self, profile: Optional[str], voice_override: Optional[str] = None) -> None:
        """Persist startup profile and voice in instance-local UI settings."""
        if LOCKED_PROFILE is not None:
            return
        selection = (profile or "").strip() or None
        normalized_voice_override = (voice_override or "").strip() or None
        try:
            from reachy_chat.config import set_custom_profile

            set_custom_profile(selection)
        except Exception:
            pass

        if not self._instance_path:
            return
        try:
            write_startup_settings(
                self._instance_path,
                profile=selection,
                voice=normalized_voice_override,
            )
            self._remove_persisted_env_values(LEGACY_STARTUP_ENV_NAMES)
            logger.info("Persisted startup personality settings to %s", Path(self._instance_path))
        except Exception as e:
            logger.warning("Failed to persist startup personality settings: %s", e)

    def _read_persisted_personality(self) -> Optional[str]:
        """Read the saved startup personality from instance-local UI settings."""
        return read_startup_settings(self._instance_path).profile

    async def apply_personality(self, profile: Optional[str]) -> str:
        """Apply a personality by updating config and restarting the active backend."""
        try:
            from reachy_chat.config import set_custom_profile
            from reachy_chat.prompts import get_session_voice, get_session_instructions

            previous_profile = getattr(config, "REACHY_MINI_CUSTOM_PROFILE", None)
            set_custom_profile(profile)
            try:
                get_session_instructions()
                get_session_voice(default=get_default_voice_for_backend(get_backend_choice()))
            except BaseException:
                set_custom_profile(previous_profile)
                raise
        except Exception as e:
            logger.error("Error applying personality '%s': %s", profile, e)
            return f"Failed to apply personality: {e}"
        except BaseException as e:
            logger.error("Failed to resolve personality content: %s", e)
            return f"Failed to apply personality: {e}"
        await self.request_backend_restart("personality_changed")
        return "Applied personality and restarting backend."

    async def get_available_voices(self) -> list[str]:
        """Return voices from the active handler, falling back only if unavailable."""
        handler = getattr(self, "handler", None)
        callback = getattr(handler, "get_available_voices", None)
        if not callable(callback):
            return get_available_voices_for_backend(get_backend_choice())
        try:
            voices = callback()
            if isawaitable(voices):
                voices = await voices
            if not isinstance(voices, list) or not all(isinstance(voice, str) for voice in voices):
                raise TypeError("handler returned an invalid voice list")
            return voices
        except Exception as exc:
            logger.warning("Unable to enumerate active handler voices: %s", exc)
            return get_available_voices_for_backend(get_backend_choice())

    def get_current_voice(self) -> str | Awaitable[str]:
        """Return the active handler voice, falling back only if lookup fails."""
        fallback = lambda: get_default_voice_for_backend(get_backend_choice())
        handler = getattr(self, "handler", None)
        callback = getattr(handler, "get_current_voice", None)
        if not callable(callback):
            return fallback()
        try:
            current = callback()
        except Exception as exc:
            logger.warning("Unable to read active handler voice: %s", exc)
            return fallback()
        if isawaitable(current):
            async def _resolve_async() -> str:
                try:
                    value = await current
                    return value.strip() if isinstance(value, str) and value.strip() else fallback()
                except Exception as exc:
                    logger.warning("Unable to read active handler voice: %s", exc)
                    return fallback()

            return _resolve_async()
        return current.strip() if isinstance(current, str) and current.strip() else fallback()

    async def change_voice(self, voice: str) -> str:
        """Apply, verify, and persist a voice through the active handler."""
        requested = voice.strip()
        if not requested:
            raise VoiceChangeError("voice cannot be empty")
        callback = getattr(getattr(self, "handler", None), "change_voice", None)
        if not callable(callback):
            raise VoiceChangeError("active handler has no voice API")
        try:
            status = callback(requested)
            if isawaitable(status):
                status = await status
        except asyncio.CancelledError:
            raise
        except Exception:
            raise

        if (
            status is None
            or status is False
            or (
                isinstance(status, str)
                and status.strip().lower().startswith(("voice change failed", "failed to change voice"))
            )
        ):
            raise VoiceChangeError(str(status or "voice change failed"))

        current_callback = getattr(getattr(self, "handler", None), "get_current_voice", None)
        if not callable(current_callback):
            raise VoiceChangeError("active handler cannot verify voice change")
        try:
            current = current_callback()
            if isawaitable(current):
                current = await current
        except asyncio.CancelledError:
            raise
        except Exception:
            raise
        if not isinstance(current, str) or current.strip() != requested:
            raise VoiceChangeError(
                f"voice change was not applied: requested {requested!r}, current {current!r}"
            )

        self._voice_override = current.strip()
        self._persist_voice_override(self._voice_override)
        return str(status)

    def _persist_voice_override(self, voice: str) -> None:
        """Persist the chosen voice as the startup voice, keeping the startup profile."""
        if not self._instance_path:
            return
        try:
            existing = read_startup_settings(self._instance_path)
            write_startup_settings(self._instance_path, profile=existing.profile, voice=voice)
        except Exception as e:
            logger.warning("Failed to persist startup voice: %s", e)

    def _init_settings_ui_if_needed(self) -> None:
        """Attach minimal settings UI to the settings app.

        Always mounts the UI when a settings_app is provided so that users
        see a confirmation message even if the API key is already configured.
        """
        if self._settings_initialized:
            return
        if self._settings_app is None:
            return

        static_dir = Path(__file__).parent / "static"
        index_file = static_dir / "index.html"
        logger.info("Serving settings UI from %s", static_dir)

        # Framework pre-registers GET / and /static; strip them so our routes aren't shadowed.
        _detach_framework_root_routes(self._settings_app)

        if hasattr(self._settings_app, "mount"):
            try:
                self._settings_app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
            except Exception:
                pass

        class BackendPayload(BaseModel):
            backend: str
            api_key: Optional[str] = None
            hf_mode: Optional[str] = None
            hf_host: Optional[str] = None
            hf_port: Optional[int] = None

        def _status_payload() -> dict[str, object]:
            backend_provider = get_backend_choice()
            active_backend = self._active_backend()
            has_openai_key = self._has_required_key(OPENAI_BACKEND)
            has_gemini_key = self._has_required_key(GEMINI_BACKEND)
            hf_session_url = get_hf_session_url()
            hf_ws_url = get_hf_direct_ws_url()
            hf_direct_host, hf_direct_port = parse_hf_direct_target(hf_ws_url)
            has_hf_session_url = bool(hf_session_url)
            has_hf_ws_url = bool(hf_ws_url)
            hf_connection_selection = get_hf_connection_selection()
            hf_connection_mode = hf_connection_selection.mode
            has_hf_connection = hf_connection_selection.has_target
            can_proceed_with_openai = has_openai_key
            can_proceed_with_gemini = has_gemini_key
            can_proceed_with_hf = has_hf_connection
            readiness_backend = backend_provider if self._can_rebuild_handler() else active_backend
            can_proceed = self._has_required_key(readiness_backend)
            requires_restart = backend_provider != active_backend and not self._can_rebuild_handler()
            backend_connection = self._backend_connection_status()
            return {
                "active_backend": active_backend,
                "backend_provider": backend_provider,
                "has_key": can_proceed,
                "has_openai_key": has_openai_key,
                "has_gemini_key": has_gemini_key,
                "has_hf_session_url": has_hf_session_url,
                "has_hf_ws_url": has_hf_ws_url,
                "has_hf_connection": has_hf_connection,
                "hf_connection_mode": hf_connection_mode,
                "hf_direct_host": hf_direct_host,
                "hf_direct_port": hf_direct_port,
                "can_proceed": can_proceed,
                "can_proceed_with_openai": can_proceed_with_openai,
                "can_proceed_with_gemini": can_proceed_with_gemini,
                "can_proceed_with_hf": can_proceed_with_hf,
                "requires_restart": requires_restart,
                **backend_connection,
            }

        # GET / -> index.html
        @self._settings_app.get("/")
        def _root() -> FileResponse:
            return FileResponse(str(index_file))

        # GET /favicon.ico -> optional, avoid noisy 404s on some browsers
        @self._settings_app.get("/favicon.ico")
        def _favicon() -> Response:
            return Response(status_code=204)

        # GET /status -> whether key is set
        @self._settings_app.get("/status")
        def _status() -> JSONResponse:
            return JSONResponse(_status_payload())

        # GET /ready -> whether backend finished loading tools
        @self._settings_app.get("/ready")
        def _ready() -> JSONResponse:
            try:
                mod = sys.modules.get("reachy_chat.tools.core_tools")
                ready = bool(getattr(mod, "_TOOLS_INITIALIZED", False)) if mod else False
            except Exception:
                ready = False
            return JSONResponse({"ready": ready})

        event_bus = self._event_bus

        @self._settings_app.get("/conversation_events")
        async def _conversation_events(request: Request) -> StreamingResponse:
            return StreamingResponse(
                _conversation_events_stream(event_bus, request),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",  # disable proxy buffering (e.g. nginx)
                    "Connection": "keep-alive",
                },
            )

        class MicPayload(BaseModel):
            muted: bool

        # GET /mic -> current mic mute state
        @self._settings_app.get("/mic")
        def _mic_state() -> JSONResponse:
            return JSONResponse({"muted": self._mic_muted})

        # POST /mic -> mute/unmute the user's microphone (Reachy Mini keeps speaking)
        @self._settings_app.post("/mic")
        def _set_mic(payload: MicPayload) -> JSONResponse:
            self._mic_muted = bool(payload.muted)
            logger.info("Microphone %s via web UI", "muted" if self._mic_muted else "unmuted")
            return JSONResponse({"muted": self._mic_muted})

        @self._settings_app.post("/backend_config")
        def _set_backend(payload: BackendPayload) -> JSONResponse:
            backend = payload.backend.strip().lower()
            if backend not in {OPENAI_BACKEND, OPENAI_REALTIME_BACKEND, GEMINI_BACKEND, HF_BACKEND}:
                return JSONResponse({"ok": False, "error": "invalid_backend"}, status_code=400)

            api_key = (payload.api_key or "").strip()
            if backend == GEMINI_BACKEND and not api_key and not self._has_required_key(GEMINI_BACKEND):
                return JSONResponse({"ok": False, "error": "empty_key"}, status_code=400)

            if backend == OPENAI_BACKEND and api_key:
                self._persist_api_key(api_key, backend=OPENAI_BACKEND)
            if backend == OPENAI_REALTIME_BACKEND and api_key:
                self._persist_api_key(api_key, backend=OPENAI_REALTIME_BACKEND)
            if backend == GEMINI_BACKEND and api_key:
                self._persist_gemini_api_key(api_key)
            if backend == HF_BACKEND:
                hf_selection = get_hf_connection_selection()
                hf_mode = (payload.hf_mode or hf_selection.mode).strip().lower()
                if hf_mode == HF_LOCAL_CONNECTION_MODE:
                    existing_host, existing_port = parse_hf_direct_target(hf_selection.direct_ws_url)
                    host = (payload.hf_host or "").strip() or existing_host or ""
                    if not host:
                        return JSONResponse({"ok": False, "error": "empty_hf_host"}, status_code=400)
                    if "://" in host or "/" in host or "?" in host or "#" in host:
                        return JSONResponse({"ok": False, "error": "invalid_hf_host"}, status_code=400)

                    port = payload.hf_port if payload.hf_port is not None else existing_port or 8765
                    if port < 1 or port > 65535:
                        return JSONResponse({"ok": False, "error": "invalid_hf_port"}, status_code=400)

                    self._persist_hf_direct_connection(host, port)
                elif hf_mode == HF_DEPLOYED_CONNECTION_MODE:
                    if not bool(get_hf_session_url()):
                        return JSONResponse({"ok": False, "error": "missing_hf_session_url"}, status_code=400)
                    self._persist_hf_allocator_connection()
                else:
                    return JSONResponse({"ok": False, "error": "invalid_hf_mode"}, status_code=400)

            self._persist_backend_choice(backend)
            if self._can_rebuild_handler():
                self._mark_restart_requested("backend_config_changed")
            payload_data = _status_payload()
            message = "Backend saved."
            if payload_data["requires_restart"]:
                message = "Backend saved. Restart Reachy Mini Conversation from the desktop app to apply it."
            elif self._can_rebuild_handler():
                message = "Backend saved. Reconnecting backend."
            return JSONResponse(
                {
                    "ok": True,
                    "message": message,
                    **payload_data,
                }
            )

        if not self._personality_routes_initialized:
            mount_personality_routes(
                self._settings_app,
                self.handler,
                lambda: self._asyncio_loop,
                persist_personality=self._persist_personality,
                get_persisted_personality=self._read_persisted_personality,
                apply_personality=self.apply_personality,
                get_available_voices=self.get_available_voices,
                get_current_voice=self.get_current_voice,
                change_voice=self.change_voice,
            )
            self._personality_routes_initialized = True

        self._settings_initialized = True

    async def _run_handler_startup_loop(self) -> None:
        """Own one explicit start_up child per handler startup generation."""
        try:
            while not self._stop_event.is_set():
                if self._startup_transition_active:
                    await self._startup_transition_done.wait()
                    continue

                selected_backend = get_backend_choice()
                if selected_backend != self._active_backend() or self._restart_requested.is_set():
                    await self._shutdown_active_handler()
                    if self._stop_event.is_set():
                        return
                    if not self._can_rebuild_handler():
                        self._restart_requested.clear()
                        self._set_backend_connection_state("restart_required")
                        await self._sleep_or_restart_requested(0.5)
                        continue
                    self._restart_requested.clear()
                    try:
                        self._build_handler_for_current_backend()
                        self._startup_generation = None
                    except Exception as error:
                        self._set_backend_connection_state("disconnected", error)
                        logger.warning(
                            "%s backend handler failed to initialize: %s. Retrying in %.1f seconds.",
                            selected_backend,
                            error,
                            self._backend_retry_delay,
                            exc_info=logger.isEnabledFor(logging.DEBUG),
                        )
                        await self._sleep_or_restart_requested(self._backend_retry_delay)
                    continue

                active_backend = self._active_backend()
                if not self._has_required_key(active_backend):
                    requirement_name = self._requirement_name(active_backend)
                    self._set_backend_connection_state(
                        "waiting_for_config",
                        f"{requirement_name} is not configured.",
                    )
                    await self._sleep_or_restart_requested(0.5)
                    continue

                generation = self._startup_generation
                if generation is None:
                    if self._startup_retry_pending:
                        self._startup_retry_pending = False
                        await self._sleep_or_restart_requested(self._backend_retry_delay)
                        continue
                    self._set_backend_connection_state("connecting")
                    generation = self._create_startup_generation()
                    await asyncio.sleep(0)
                    self._infer_generation_connected(generation)

                task = generation.task
                if task is None:
                    self._startup_generation = None
                    continue
                restart_wait = asyncio.create_task(
                    self._restart_requested.wait(),
                    name="handler-restart-request",
                )
                stop_wait = asyncio.create_task(
                    self._stop_event.wait(),
                    name="handler-stop-request",
                )
                try:
                    done, _ = await asyncio.wait(
                        {task, generation.replaced, restart_wait, stop_wait},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for waiter in (restart_wait, stop_wait):
                        if not waiter.done():
                            waiter.cancel()
                    await asyncio.gather(restart_wait, stop_wait, return_exceptions=True)

                if self._stop_event.is_set():
                    return
                if self._startup_transition_active or generation.replaced.done():
                    await self._startup_transition_done.wait()
                    continue
                if self._startup_generation is not generation:
                    continue
                if restart_wait in done:
                    continue
                if task not in done:
                    continue

                try:
                    await task
                except asyncio.CancelledError:
                    if self._stop_event.is_set() or self._restart_requested.is_set():
                        pass
                    else:
                        raise
                except Exception as error:
                    self._set_backend_connection_state("disconnected", error)
                    logger.warning(
                        "%s backend failed to start: %s. Settings UI remains available; retrying in %.1f seconds.",
                        active_backend,
                        error,
                        self._backend_retry_delay,
                        exc_info=logger.isEnabledFor(logging.DEBUG),
                    )
                else:
                    self._set_backend_connection_state("disconnected")
                    logger.info(
                        "%s backend session ended. Settings UI remains available; retrying in %.1f seconds.",
                        active_backend,
                        self._backend_retry_delay,
                    )
                if generation.connection_result.done():
                    try:
                        generation.connection_result.exception()
                    except BaseException:
                        pass
                if self._startup_generation is generation:
                    self._startup_generation = None
                if self._restart_requested.is_set():
                    continue
                await self._sleep_or_restart_requested(self._backend_retry_delay)
        finally:
            generation = self._startup_generation
            if generation is not None:
                await self._stop_startup_generation(generation, timeout=5.0)

    def launch(self) -> None:
        """Start the recorder/player and run the async processing loops.

        If the selected backend is missing its required key, expose a tiny
        settings UI via the Reachy Mini settings server to collect it before
        starting streams.
        """
        self._stop_event.clear()

        # Try to load an existing instance .env first (covers subsequent runs)
        if self._instance_path:
            try:
                from dotenv import load_dotenv

                env_path = Path(self._instance_path) / ".env"
                if env_path.exists():
                    load_dotenv(dotenv_path=str(env_path), override=True)
                    refresh_runtime_config_from_env()
            except Exception:
                pass  # Instance .env loading is optional; continue with defaults

        active_backend = self._active_backend()

        # Always expose settings UI if a settings app is available
        # (do this AFTER loading the instance .env so status endpoint sees the right value)
        self._init_settings_ui_if_needed()

        # If key is still missing -> wait until provided via the settings UI
        if not self._has_required_key(active_backend):
            requirement_name = self._requirement_name(active_backend)
            self._set_backend_connection_state("waiting_for_config", f"{requirement_name} is not configured.")
            if active_backend == HF_BACKEND and self._settings_app is None:
                logger.error(
                    "%s not found. Set it in the app .env before starting the Hugging Face backend.", requirement_name
                )
                return
            logger.warning("%s not found. Open the app settings page to configure it.", requirement_name)
            # Poll until the key becomes available (set via the settings UI)
            try:
                while not self._stop_event.is_set() and not self._has_required_key(active_backend):
                    selected_backend = get_backend_choice()
                    if selected_backend != active_backend:
                        if self._can_rebuild_handler():
                            active_backend = selected_backend
                            self._active_backend_name = selected_backend
                            self._restart_requested.set()
                            self._set_backend_connection_state("waiting_for_config")
                        else:
                            self._set_backend_connection_state("restart_required")
                    time.sleep(0.2)
            except KeyboardInterrupt:
                logger.info("Interrupted while waiting for API key.")
                return
            if self._stop_event.is_set():
                return
            self._set_backend_connection_state("not_started")

        # Start media after key is set/available
        self._robot.media.start_recording()
        self._robot.media.start_playing()
        time.sleep(1)  # give some time to the pipelines to start
        # XVF3800 firmware (v0.2.1.2 on this unit) has an unreliable control-channel
        # read-back: parameter WRITES succeed but verification READS return retry/unknown
        # status, so verify=True wrongly reports every tuned param as failed. Writes are
        # applied either way; skip the broken read-back. (Confirmed via direct device probe.)
        apply_audio_startup_config(self._robot, logger=logger, verify=False)

        async def runner() -> None:
            # Capture loop for cross-thread personality actions
            loop = asyncio.get_running_loop()
            self._asyncio_loop = loop  # type: ignore[assignment]
            self._tasks = [
                asyncio.create_task(self._run_handler_startup_loop(), name="realtime-handler"),
                asyncio.create_task(self.record_loop(), name="stream-record-loop"),
                asyncio.create_task(self.play_loop(), name="stream-play-loop"),
            ]
            try:
                await asyncio.gather(*self._tasks)
            except asyncio.CancelledError:
                logger.info("Tasks cancelled during shutdown")
            finally:
                # Ensure handler connection is closed
                await self._shutdown_active_handler()

        asyncio.run(runner())

    def close(self) -> None:
        """Stop the stream and underlying media pipelines.

        This method:
        - Stops audio recording and playback first
        - Sets the stop event to signal async loops to terminate
        - Cancels all pending async tasks (openai-handler, record-loop, play-loop)
        """
        logger.info("Stopping LocalStream...")

        # Stop media pipelines FIRST before cancelling async tasks
        # This ensures clean shutdown before PortAudio cleanup
        try:
            self._robot.media.stop_recording()
        except Exception as e:
            logger.debug(f"Error stopping recording (may already be stopped): {e}")

        try:
            self._robot.media.stop_playing()
        except Exception as e:
            logger.debug(f"Error stopping playback (may already be stopped): {e}")

        # Now signal async loops to stop
        self._stop_event.set()

        # Cancel all running tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()

    def clear_audio_queue(self) -> None:
        """Flush queued playback audio immediately on user barge-in.

        Calls the SDK's ``clear_player()`` — now a first-class flush on both
        the local GStreamer and WebRTC backends (the WebRTC one also tells the
        daemon to drop audio already queued for the speaker). Falls back to the
        deprecated ``clear_output_buffer()`` only for older SDKs.
        """
        logger.info("User intervention: flushing player queue")
        audio = getattr(self._robot.media, "audio", None)
        if audio is not None:
            if hasattr(audio, "clear_player") and callable(audio.clear_player):
                audio.clear_player()
            elif hasattr(audio, "clear_output_buffer") and callable(audio.clear_output_buffer):
                # Older SDK without clear_player(); best-effort.
                audio.clear_output_buffer()
        # Drain the handler's pending output in place — do NOT replace the
        # queue object, since emit() may be awaiting it (wait_for_item).
        self._drain_output_queue()

    def _drain_output_queue(self) -> None:
        """Empty the handler's output queue in place without replacing it."""
        queue = getattr(self.handler, "output_queue", None)
        if queue is None:
            return
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def record_loop(self) -> None:
        """Read mic frames from the recorder and forward them to the handler."""
        input_sample_rate = self._robot.media.get_input_audio_samplerate()
        logger.debug(f"Audio recording started at {input_sample_rate} Hz")

        while not self._stop_event.is_set():
            audio_frame = self._robot.media.get_audio_sample()
            if audio_frame is not None and not self._mic_muted:
                await self.handler.receive((input_sample_rate, audio_frame))
            await asyncio.sleep(0)  # avoid busy loop

    async def play_loop(self) -> None:
        """Fetch outputs from the handler: log text and play audio frames."""
        while not self._stop_event.is_set():
            handler = self.handler
            try:
                handler_output = await asyncio.wait_for(handler.emit(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            if isinstance(handler_output, AdditionalOutputs):
                for msg in handler_output.args:
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        logger.info(
                            "role=%s content=%s",
                            msg.get("role"),
                            content if len(content) < 500 else content[:500] + "…",
                        )

            elif isinstance(handler_output, tuple):
                handler_output_sample_rate, audio_data = handler_output
                device_output_sample_rate = self._robot.media.get_output_audio_samplerate()

                # Skip empty audio frames
                if audio_data.size == 0:
                    continue

                # Flatten to 1D mono if needed
                if audio_data.ndim == 2:
                    # Handler sends (1, N) shape; flatten to (N,)
                    if audio_data.shape[1] > audio_data.shape[0]:
                        audio_data = audio_data.T  # (1, N) -> (N, 1)
                    # Take first channel if multi-channel
                    if audio_data.shape[1] > 1:
                        audio_data = audio_data[:, 0]
                    else:
                        audio_data = audio_data.ravel()  # (N, 1) -> (N,)
                elif audio_data.ndim > 2:
                    # Flatten anything higher-dimensional to 1D
                    audio_data = audio_data.ravel()

                # Cast to float32
                audio_frame = audio_to_float32(audio_data)

                # Resample if needed: convert from handler output rate to device output rate
                if handler_output_sample_rate != device_output_sample_rate:
                    num_samples = int(len(audio_frame) * device_output_sample_rate / handler_output_sample_rate)
                    if num_samples == 0:
                        continue
                    audio_frame = resample(
                        audio_frame,
                        num_samples,
                    )

                # Ensure 1D mono array, then convert to stereo for the 2-channel appsrc
                if audio_frame.ndim > 1:
                    audio_frame = audio_frame.ravel()

                # Convert mono to stereo by duplicating the mono channel
                # The appsrc caps specify CHANNELS=2 (stereo)
                if len(audio_frame.shape) == 1:
                    # Duplicate mono to stereo: (N,) -> (N, 2)
                    audio_frame = np.column_stack((audio_frame, audio_frame))

                self._robot.media.push_audio_sample(audio_frame)

            else:
                logger.debug("Ignoring output type=%s", type(handler_output).__name__)

            await asyncio.sleep(0)  # yield to event loop
