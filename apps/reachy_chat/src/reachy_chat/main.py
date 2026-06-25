"""Entrypoint for the Reachy Mini conversation app."""

import os
import sys
import time
import asyncio
import argparse
import threading
from typing import Optional
from pathlib import Path
from collections.abc import Callable, Awaitable

from fastapi import FastAPI, Request, Response

from reachy_mini import ReachyMini, ReachyMiniApp
from reachy_mini.utils import create_head_pose
from reachy_chat.gstreamer_nvidia_patch import apply_nvidia_unixfd_patch
from reachy_chat.utils import (
    CameraVisionInitializationError,
    parse_args,
    setup_logger,
    initialize_camera_and_vision,
    log_connection_troubleshooting,
)

# Apply NVIDIA Jetson GStreamer plugin patch before any SDK initialization
apply_nvidia_unixfd_patch()

RETURN_HOME_DURATION_S = 2.5
SLEEP_CUE_SETTLE_S = 1.0


def _return_home(robot: ReachyMini, logger) -> None:
    try:
        robot.goto_target(
            head=create_head_pose(degrees=True),
            body_yaw=0.0,
            duration=RETURN_HOME_DURATION_S,
        )
    except (AttributeError, TypeError) as exc:
        logger.warning("chat: graceful return-home unavailable; snapping home: %s", exc)
        try:
            robot.set_target(head=create_head_pose(degrees=True), body_yaw=0.0)
        except (AttributeError, TypeError) as set_exc:
            logger.warning("chat: failed to snap home on exit: %s", set_exc)
        except Exception as set_exc:
            logger.debug("chat: failed to snap home on exit: %s", set_exc)
    except Exception as exc:
        logger.debug("chat: failed to return home on exit: %s", exc)

    try:
        robot.media.play_sound("go_sleep.wav")
        time.sleep(SLEEP_CUE_SETTLE_S)
    except AttributeError as exc:
        logger.warning("chat: sleep cue unavailable on exit: %s", exc)
    except Exception as exc:
        logger.debug("chat: sleep cue failed on exit: %s", exc)


def main() -> None:
    """Entrypoint for the Reachy Mini conversation app."""
    args, _ = parse_args()
    if args.command == "tool-spaces":
        from reachy_chat.tool_spaces import handle_tool_spaces_command

        logger = setup_logger(args.debug)
        try:
            raise SystemExit(handle_tool_spaces_command(args))
        except Exception as exc:
            logger.error("tool-spaces command failed: %s", exc)
            raise SystemExit(1) from exc
    run(args)


def run(
    args: argparse.Namespace,
    robot: ReachyMini = None,
    app_stop_event: Optional[threading.Event] = None,
    settings_app: Optional[FastAPI] = None,
    instance_path: Optional[str] = None,
) -> None:
    """Run the Reachy Mini conversation app."""
    # Putting these dependencies here makes the dashboard faster to load when the conversation app is installed
    from reachy_chat.moves import MovementManager
    from reachy_chat.config import (
        HF_BACKEND,
        OPENAI_REALTIME_BACKEND,
        HF_LOCAL_CONNECTION_MODE,
        config,
        is_gemini_model,
        get_backend_label,
        set_instance_path,
        get_hf_connection_selection,
        refresh_runtime_config_from_env,
    )
    from reachy_chat.startup_settings import (
        StartupSettings,
        load_startup_settings_into_runtime,
    )

    logger = setup_logger(args.debug)
    logger.info("Starting Reachy Mini Conversation App")
    set_instance_path(instance_path)
    startup_settings = StartupSettings()

    if instance_path is not None:
        try:
            from dotenv import load_dotenv

            env_path = Path(instance_path) / ".env"
            if env_path.exists():
                load_dotenv(dotenv_path=str(env_path), override=True)
                refresh_runtime_config_from_env()
                logger.info("Loaded instance configuration from %s", env_path)
        except Exception as e:
            logger.warning("Failed to load instance configuration: %s", e)

        try:
            startup_settings = load_startup_settings_into_runtime(instance_path)
        except Exception as e:
            logger.warning("Failed to load startup settings: %s", e)

    if config.BACKEND_PROVIDER == HF_BACKEND:
        logger.info(
            "Configured backend provider: %s (%s), connection mode: %s",
            config.BACKEND_PROVIDER,
            get_backend_label(config.BACKEND_PROVIDER),
            get_hf_connection_selection().mode,
        )
    else:
        logger.info(
            "Configured backend provider: %s (%s), model: %s",
            config.BACKEND_PROVIDER,
            get_backend_label(config.BACKEND_PROVIDER),
            config.MODEL_NAME,
        )

    from reachy_chat.console import LocalStream
    from reachy_chat.tools.core_tools import ToolDependencies, initialize_tools
    from reachy_chat.conversation_handler import ConversationHandler

    try:
        initialize_tools(instance_path=instance_path)
    except Exception as e:
        logger.error("Failed to initialize tools: %s", e)
        sys.exit(1)

    if args.no_camera and args.head_tracker is not None:
        logger.warning("Head tracking disabled: --no-camera flag is set. Remove --no-camera to enable head tracking.")

    if robot is None:
        try:
            robot_kwargs = {}
            if args.robot_name is not None:
                robot_kwargs["robot_name"] = args.robot_name

            logger.info("Initializing ReachyMini (SDK will auto-detect appropriate backend)")
            robot = ReachyMini(**robot_kwargs)

        except TimeoutError as e:
            logger.error(f"Connection timeout: Failed to connect to Reachy Mini daemon. Details: {e}")
            log_connection_troubleshooting(logger, args.robot_name)
            sys.exit(1)

        except ConnectionError as e:
            logger.error(f"Connection failed: Unable to establish connection to Reachy Mini. Details: {e}")
            log_connection_troubleshooting(logger, args.robot_name)
            sys.exit(1)

        except Exception as e:
            logger.error(f"Unexpected error during robot initialization: {type(e).__name__}: {e}")
            logger.error("Please check your configuration and try again.")
            sys.exit(1)

    try:
        camera_worker, vision_processor = initialize_camera_and_vision(args, robot)
    except CameraVisionInitializationError as e:
        logger.error("Failed to initialize camera/vision: %s", e)
        sys.exit(1)

    movement_manager = MovementManager(
        current_robot=robot,
        camera_worker=camera_worker,
    )

    deps = ToolDependencies(
        reachy_mini=robot,
        movement_manager=movement_manager,
        instance_path=instance_path,
        camera_worker=camera_worker,
        vision_processor=vision_processor,
    )

    def build_handler(startup_voice: Optional[str] = None) -> ConversationHandler:
        """Build a conversation handler for the current runtime backend config."""
        if is_gemini_model():
            from reachy_chat.gemini_live import GeminiLiveHandler

            logger.info(
                "Using %s via GeminiLiveHandler",
                get_backend_label(config.BACKEND_PROVIDER),
            )
            return GeminiLiveHandler(
                deps,
                instance_path=instance_path,
                startup_voice=startup_voice,
            )
        if config.BACKEND_PROVIDER == HF_BACKEND:
            from reachy_chat.huggingface_realtime import HuggingFaceRealtimeHandler

            hf_connection_selection = get_hf_connection_selection()
            transport_label = (
                "Hugging Face direct websocket"
                if hf_connection_selection.mode == HF_LOCAL_CONNECTION_MODE and hf_connection_selection.has_target
                else "Hugging Face session proxy"
            )
            logger.info(
                "Using %s via Hugging Face realtime handler (%s)",
                get_backend_label(config.BACKEND_PROVIDER),
                transport_label,
            )
            return HuggingFaceRealtimeHandler(
                deps,
                instance_path=instance_path,
                startup_voice=startup_voice,
            )
        if config.BACKEND_PROVIDER == OPENAI_REALTIME_BACKEND:
            from reachy_chat.openai_realtime import OpenaiRealtimeHandler

            logger.info(
                "Using %s via OpenaiRealtimeHandler",
                get_backend_label(config.BACKEND_PROVIDER),
            )
            return OpenaiRealtimeHandler(
                deps,
                instance_path=instance_path,
                startup_voice=startup_voice,
            )

        from reachy_chat.openrouter_chat import OpenRouterChatHandler

        logger.info(
            "Using %s via local STT -> OpenRouter chat -> local TTS handler",
            get_backend_label(config.BACKEND_PROVIDER),
        )
        return OpenRouterChatHandler(
            deps,
            instance_path=instance_path,
            startup_voice=startup_voice,
        )

    handler = build_handler(startup_settings.voice)

    stream_manager: LocalStream | None = None
    own_ui_server = None

    effective_settings_app = settings_app
    if args.ui and settings_app is None:
        effective_settings_app = FastAPI()

        @effective_settings_app.middleware("http")
        async def _no_cache(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
            """Serve everything no-store so browsers don't keep stale UI modules."""
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            return response

    stream_manager = LocalStream(
        handler,
        robot,
        settings_app=effective_settings_app,
        instance_path=instance_path,
        handler_factory=build_handler,
        startup_voice=startup_settings.voice,
    )

    if args.ui and settings_app is None and effective_settings_app is not None:
        import uvicorn

        # Routes must exist before uvicorn starts serving, launch() repeats this as a no-op.
        stream_manager._init_settings_ui_if_needed()

        own_ui_server = uvicorn.Server(
            uvicorn.Config(effective_settings_app, host="0.0.0.0", port=7860, log_level="warning")
        )
        threading.Thread(target=own_ui_server.run, daemon=True, name="ui-server").start()
        logger.info("Web UI available at http://localhost:7860")

    # Each async service → its own thread/loop
    movement_manager.start()
    # Audio-reactive head motion is driven by the daemon's wobbler, which
    # taps the media pipeline at push_audio_sample. The console stream pushes
    # assistant audio through that pipeline directly.
    robot.enable_wobbling()
    if camera_worker:
        camera_worker.start()

    def poll_stop_event() -> None:
        """Poll the stop event to allow graceful shutdown."""
        if app_stop_event is not None:
            app_stop_event.wait()

        logger.info("App stop event detected, shutting down...")
        try:
            stream_manager.close()
        except Exception as e:
            logger.error(f"Error while closing stream manager: {e}")

    if app_stop_event:
        threading.Thread(target=poll_stop_event, daemon=True).start()

    try:
        stream_manager.launch()
    except KeyboardInterrupt:
        logger.info("Keyboard interruption in main thread... closing server.")
    finally:
        if own_ui_server is not None:
            own_ui_server.should_exit = True

        try:
            movement_manager.stop()
        except Exception as e:
            logger.debug(f"Error stopping movement manager during shutdown: {e}")
        try:
            robot.disable_wobbling()
        except Exception as e:
            logger.debug(f"Error disabling wobbling during shutdown: {e}")
        _return_home(robot, logger)
        if camera_worker:
            camera_worker.stop()

        # Ensure media is explicitly closed before disconnecting
        try:
            robot.media.close()
        except Exception as e:
            logger.debug(f"Error closing media during shutdown: {e}")

        # prevent connection to keep alive some threads
        robot.client.disconnect()
        time.sleep(1)
        logger.info("Shutdown complete.")


class ReachyChat(ReachyMiniApp):  # type: ignore[misc]
    """Reachy Mini Apps entry point for the conversation app."""

    # Default to LOCAL media backend; Lite daemon doesn't expose WebRTC signalling (:8443),
    # so auto-detect's WebRTC fallback would crash. Override via REACHY_MINI_MEDIA_BACKEND env var
    # to use WebRTC for wireless robot setups.
    request_media_backend = os.getenv("REACHY_MINI_MEDIA_BACKEND", "local")

    custom_app_url = "http://0.0.0.0:7860/"
    dont_start_webserver = False

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        """Run the Reachy Mini conversation app."""
        asyncio.set_event_loop(asyncio.new_event_loop())

        args, _ = parse_args()

        instance_path = self._get_instance_path().parent
        run(
            args,
            robot=reachy_mini,
            app_stop_event=stop_event,
            settings_app=self.settings_app,
            instance_path=instance_path,
        )


if __name__ == "__main__":
    app = ReachyChat()
    try:
        app.wrapped_run()
    except KeyboardInterrupt:
        app.stop()
