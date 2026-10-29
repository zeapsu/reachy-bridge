"""The seam between the bridge and Pollen's daemon (``reachy-mini``).

This is the only runtime module that imports ``reachy_mini`` or reads its
private attributes. The features (``features/``) and platform adapters
(``platforms/``) call the functions here and never reach into the daemon
themselves. When an upstream release renames or reshapes something, the fix
belongs in this file, which is the surface the SDK update agent may change
(#155). If a fix needs a different hook rather than a renamed one, it isn't a
seam fix: flag it.

Every upstream symbol used here is listed in ``REQUIREMENTS`` with the
feature that needs it. ``check()`` verifies them without starting anything;
``python -m reachy_bridge_runtime --check`` runs it in CI and fails on any
mismatch. At service startup a feature whose requirements fail is skipped
with an error instead of half-applying a patch to code it doesn't match.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# --- Requirements ------------------------------------------------------------


@dataclass(frozen=True)
class Requirement:
    feature: str
    description: str
    check: Callable[[], None]


class UpstreamMismatch(RuntimeError):
    """An upstream symbol a feature relies on is missing or has changed."""


def _attr(module: str, path: str) -> Any:
    try:
        target: Any = importlib.import_module(module)
    except ImportError as exc:
        raise UpstreamMismatch(f"cannot import {module}: {exc}") from exc
    for name in path.split(".") if path else ():
        if not hasattr(target, name):
            raise UpstreamMismatch(f"{module}.{path}: no attribute {name!r}")
        target = getattr(target, name)
    return target


def _coroutine(module: str, path: str, *params: str) -> Callable[[], None]:
    def check() -> None:
        function = _attr(module, path)
        if not inspect.iscoroutinefunction(function):
            raise UpstreamMismatch(f"{module}.{path} is no longer a coroutine function")
        _has_params(function, f"{module}.{path}", params)

    return check


def _function(module: str, path: str, *params: str) -> Callable[[], None]:
    def check() -> None:
        function = _attr(module, path)
        if not callable(function):
            raise UpstreamMismatch(f"{module}.{path} is not callable")
        _has_params(function, f"{module}.{path}", params)

    return check


def _has_params(function: Any, name: str, params: tuple[str, ...]) -> None:
    signature = inspect.signature(function)
    missing = [p for p in params if p not in signature.parameters]
    if missing:
        raise UpstreamMismatch(f"{name}{signature} lacks parameter(s) {', '.join(missing)}")


def _source_mentions(module: str, path: str, *names: str) -> Callable[[], None]:
    """For private attributes that only exist on instances."""

    def check() -> None:
        source = inspect.getsource(_attr(module, path))
        missing = [n for n in names if n not in source]
        if missing:
            raise UpstreamMismatch(f"{module}.{path} no longer uses {', '.join(missing)}")

    return check


KINEMATICS_ENGINES = ("AnalyticalKinematics", "Placo")
DEFAULT_KINEMATICS = KINEMATICS_ENGINES[0]
KINEMATICS_FLAG = "--kinematics-engine"

_MANAGER = "reachy_mini.apps.manager"
_DAEMON = "reachy_mini.daemon.daemon"
_APP_MAIN = "reachy_mini.daemon.app.main"
_LOCK = "reachy_mini.daemon.robot_app_lock"
_BACKEND = "reachy_mini.daemon.backend.abstract"
_MEDIA_SERVER = "reachy_mini.media.media_server"
_APPS_VENV = "reachy_mini.apps.sources.local_common_venv"
_ROBOT_BACKEND = "reachy_mini.daemon.backend.robot.backend"
# Backend.__init__ keeps uploaded move audio in <tempdir>/<this>/audio.
_UPLOADS_DIR = "reachy-mini-uploads"

REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement("daemon", "daemon entry point", _function(_APP_MAIN, "main")),
    Requirement(
        "daemon",
        "the command-line flags the service unit passes",
        _source_mentions(
            _APP_MAIN,
            "",
            "--desktop-app-daemon",
            "--headless",
            "--no-autostart",
            "--no-wake-up-on-start",
            "--fastapi-host",
            "--fastapi-port",
            KINEMATICS_FLAG,
        ),
    ),
    Requirement("routes", "create_app builds the FastAPI app", _function(_APP_MAIN, "create_app", "args")),
    Requirement(
        "routes",
        "app.state holds the daemon and its args",
        _source_mentions(_APP_MAIN, "create_app", "app.state.daemon", "app.state.args"),
    ),
    Requirement(
        "keep_awake",
        "AppManager.stop_current_app",
        _coroutine(_MANAGER, "AppManager.stop_current_app"),
    ),
    Requirement(
        "keep_awake",
        "the robot slot's became-free hook",
        _function(_LOCK, "RobotAppLock.set_on_became_free_handler"),
    ),
    Requirement(
        "keep_awake",
        "RobotAppLock stores the hook in _on_became_free",
        _source_mentions(_LOCK, "RobotAppLock", "_on_became_free"),
    ),
    Requirement(
        "keep_awake",
        "Daemon owns robot_app_lock and backend",
        _source_mentions(_DAEMON, "Daemon", "self.robot_app_lock", "self.backend"),
    ),
    Requirement(
        "keep_awake",
        "Backend.is_shutting_down",
        _source_mentions(_BACKEND, "Backend", "is_shutting_down"),
    ),
    Requirement(
        "keep_awake",
        "RobotBackend tracks torque in _torque_enabled",
        _source_mentions(_ROBOT_BACKEND, "RobotBackend", "self._torque_enabled ="),
    ),
    Requirement(
        "keep_awake",
        "Backend.get_motor_control_mode() and set_motor_control_mode(mode)",
        lambda: (
            _function(_BACKEND, "Backend.get_motor_control_mode")(),
            _function(_BACKEND, "Backend.set_motor_control_mode", "mode")(),
            _attr(_BACKEND, "MotorControlMode.Disabled"),
        ),
    ),
    Requirement(
        "app_bind",
        "AppManager.start_app(app_name)",
        _coroutine(_MANAGER, "AppManager.start_app", "app_name"),
    ),
    Requirement(
        "app_bind",
        "apps run get_app_python's interpreter with a copy of the daemon's environment",
        lambda: (
            _function(_APPS_VENV, "get_app_python", "app_name", "wireless_version", "desktop_app_daemon")(),
            _source_mentions(
                _MANAGER,
                "AppManager.start_app",
                "get_app_python",
                "self.wireless_version",
                "self.desktop_app_daemon",
                "os.environ.copy()",
            )(),
        ),
    ),
    Requirement(
        "app_bind",
        "GstMediaServer._configure_webrtc(pipeline) returns the webrtcsink running the signalling server",
        lambda: (
            _function(_MEDIA_SERVER, "GstMediaServer._configure_webrtc", "pipeline")(),
            _source_mentions(_MEDIA_SERVER, "GstMediaServer._configure_webrtc", "run-signalling-server", "return webrtcsink")(),
        ),
    ),
    Requirement(
        "sim_apps",
        "every apps_venv path comes from local_common_venv._get_venv_parent_dir()",
        lambda: (
            _function(_APPS_VENV, "_get_venv_parent_dir")(),
            _source_mentions(_APPS_VENV, "_get_app_venv_path", "_get_venv_parent_dir()")(),
            _function(_APPS_VENV, "get_app_python", "app_name")(),
        ),
    ),
    Requirement(
        "sim_apps",
        "AppManager.start_app(app_name) runs get_app_python's interpreter",
        lambda: (
            _coroutine(_MANAGER, "AppManager.start_app", "app_name")(),
            _source_mentions(_MANAGER, "AppManager.start_app", "get_app_python")(),
        ),
    ),
    Requirement(
        "kinematics",
        "Daemon.start takes kinematics_engine",
        _coroutine(_DAEMON, "Daemon.start", "kinematics_engine"),
    ),
    Requirement(
        "kinematics",
        "the backend records its kinematics engine",
        _source_mentions(_BACKEND, "Backend", "kinematics_engine"),
    ),
    Requirement(
        "kinematics",
        "daemon/start reads the engine from app.state.args",
        _source_mentions("reachy_mini.daemon.app.routers.daemon", "", "args.kinematics_engine"),
    ),
    Requirement(
        "audio_levels",
        "GstMediaServer._configure_audio(pipeline, webrtcsink)",
        _function(_MEDIA_SERVER, "GstMediaServer._configure_audio", "pipeline", "webrtcsink"),
    ),
    Requirement(
        "audio_levels",
        f"the mic branch ends in a queue named {'queue_audiosrc'!r}",
        _source_mentions(_MEDIA_SERVER, "GstMediaServer", "queue_audiosrc"),
    ),
    Requirement(
        "audio_levels",
        "Daemon keeps the media server in _media_server and has media_released",
        _source_mentions(_DAEMON, "Daemon", "_media_server", "media_released"),
    ),
    Requirement(
        "camera",
        "GStreamerCamera reads the daemon's shared camera feed",
        _function("reachy_mini.media.camera_gstreamer", "GStreamerCamera", "log_level"),
    ),
    Requirement(
        "camera",
        "is_local_camera_available",
        _function("reachy_mini.daemon.utils", "is_local_camera_available"),
    ),
    Requirement(
        "asoundrc",
        "the Reachy Mini ~/.asoundrc helpers",
        lambda: (
            _function("reachy_mini.media.audio_utils", "check_reachymini_asoundrc")(),
            _function("reachy_mini.media.audio_utils", "write_asoundrc_to_home")(),
        ),
    ),
    Requirement(
        "sim_sounds",
        "Backend.play_sound(sound_file) and Backend.stop_sound()",
        lambda: (
            _function(_BACKEND, "Backend.play_sound", "sound_file")(),
            _function(_BACKEND, "Backend.stop_sound")(),
        ),
    ),
    Requirement(
        "sim_sounds",
        "ASSETS_ROOT_PATH, where play_sound looks up a bare file name",
        lambda: _attr("reachy_mini.utils.constants", "ASSETS_ROOT_PATH"),
    ),
    Requirement(
        "sim_sounds",
        "where the daemon keeps uploaded sounds and move audio",
        lambda: (
            _attr("reachy_mini.daemon.app.routers.media", "SOUNDS_TMP_DIR"),
            _source_mentions(_BACKEND, "Backend", f'"{_UPLOADS_DIR}"', '"audio"')(),
            _attr("huggingface_hub.constants", "HF_HUB_CACHE"),
        ),
    ),
)


def check(features: set[str] | None = None) -> dict[str, list[str]]:
    """Problems per feature (an empty dict when everything matches)."""
    problems: dict[str, list[str]] = {}
    for requirement in REQUIREMENTS:
        if features is not None and requirement.feature not in features:
            continue
        try:
            requirement.check()
        except Exception as exc:  # UpstreamMismatch, or anything the import raised
            problems.setdefault(requirement.feature, []).append(f"{requirement.description}: {exc}")
    return problems


def installed_version() -> str:
    from importlib.metadata import version

    return version("reachy-mini")


# --- Daemon ------------------------------------------------------------------


def run_daemon() -> int:
    """Run the stock daemon entry point with the current ``sys.argv``."""
    from reachy_mini.daemon.app.main import main

    return main()


def wrap_daemon_start(wrapper: Callable[..., Awaitable[Any]]) -> None:
    """Replace ``Daemon.start`` with ``wrapper(original, daemon, *args, **kwargs)``.

    Like every ``wrap_*`` here, wrapping twice with the same function (by
    qualified name) is a no-op.
    """
    from reachy_mini.daemon.daemon import Daemon

    _wrap(Daemon, "start", wrapper)


def start_kinematics(kwargs: dict[str, Any]) -> str | None:
    """The kinematics engine an explicit ``Daemon.start`` call names."""
    return kwargs.get("kinematics_engine")


def with_kinematics(kwargs: dict[str, Any], engine: str) -> dict[str, Any]:
    return {**kwargs, "kinematics_engine": engine}


def _wrap(owner: Any, name: str, wrapper: Callable[..., Any]) -> None:
    original = getattr(owner, name)
    key = f"{wrapper.__module__}.{wrapper.__qualname__}"
    if getattr(original, "_bridge_wrapper", None) == key:
        return

    if inspect.iscoroutinefunction(original):

        async def patched(self: Any, *args: Any, **kwargs: Any) -> Any:
            return await wrapper(original, self, *args, **kwargs)

    else:

        def patched(self: Any, *args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
            return wrapper(original, self, *args, **kwargs)

    patched._bridge_wrapper = key  # type: ignore[attr-defined]
    patched.__wrapped__ = original  # type: ignore[attr-defined]
    setattr(owner, name, patched)


# --- Apps and the robot slot -------------------------------------------------


def wrap_stop_current_app(wrapper: Callable[..., Awaitable[Any]]) -> None:
    """Replace ``AppManager.stop_current_app`` with ``wrapper(original, manager, ...)``."""
    from reachy_mini.apps.manager import AppManager

    _wrap(AppManager, "stop_current_app", wrapper)


def wrap_start_app(wrapper: Callable[..., Awaitable[Any]]) -> None:
    """Replace ``AppManager.start_app`` with ``wrapper(original, manager, app_name, ...)``."""
    from reachy_mini.apps.manager import AppManager

    _wrap(AppManager, "start_app", wrapper)


def default_apps_root() -> Path:
    """Where the daemon keeps ``apps_venv`` unless told otherwise: next to its own venv."""
    from reachy_mini.apps.sources import local_common_venv

    function = getattr(local_common_venv._get_venv_parent_dir, "__wrapped__", local_common_venv._get_venv_parent_dir)
    return Path(function())


def set_apps_root(root: Path) -> None:
    """Keep ``apps_venv`` under ``root`` (a sim daemon's apps, #218)."""
    from reachy_mini.apps.sources import local_common_venv

    original = local_common_venv._get_venv_parent_dir

    def apps_root() -> Path:
        return Path(root)

    apps_root.__wrapped__ = getattr(original, "__wrapped__", original)  # type: ignore[attr-defined]
    local_common_venv._get_venv_parent_dir = apps_root


def app_python(manager: Any, app_name: str) -> Path:
    """The interpreter ``start_app`` runs ``app_name`` with."""
    from reachy_mini.apps.sources.local_common_venv import get_app_python

    return Path(get_app_python(app_name, manager.wireless_version, manager.desktop_app_daemon))


def manager_daemon(manager: Any) -> Any | None:
    return getattr(manager, "daemon", None)


def slot_free_handler(daemon: Any) -> Callable[..., Any] | None:
    """The hook the robot slot calls when it becomes free (the idle reset)."""
    lock = getattr(daemon, "robot_app_lock", None)
    return getattr(lock, "_on_became_free", None)


def set_slot_free_handler(daemon: Any, handler: Callable[..., Any] | None) -> bool:
    lock = getattr(daemon, "robot_app_lock", None)
    if lock is None:
        return False
    lock.set_on_became_free_handler(handler)
    return True


def daemon_backend(daemon: Any) -> Any | None:
    return getattr(daemon, "backend", None)


def backend_shutting_down(backend: Any) -> bool:
    return bool(getattr(backend, "is_shutting_down", False))


def sync_torque_mode(backend: Any) -> bool:
    """Set the motor mode to ``enabled`` when torque is on but the mode says
    ``disabled``; True if it did.

    The app manager's return home after a stop turns torque on with
    ``enable_motors()``, which leaves ``motor_control_mode`` as it was. Only the
    real robot backend tracks torque; the simulators report a mode that is
    always right.
    """
    from reachy_mini.daemon.backend.abstract import MotorControlMode

    if getattr(backend, "_torque_enabled", None) is not True:
        return False
    if backend.get_motor_control_mode() != MotorControlMode.Disabled:
        return False
    backend.set_motor_control_mode(MotorControlMode.Enabled)
    return True


# --- The daemon's FastAPI app --------------------------------------------------

_app_installers: list[Callable[[Any], None]] = []


def on_create_app(installer: Callable[[Any], None]) -> None:
    """Run ``installer(app)`` on every daemon app ``create_app`` builds.

    Installers run in registration order; registering the same one twice is a
    no-op.
    """
    from reachy_mini.daemon.app import main as app_main

    if installer not in _app_installers:
        _app_installers.append(installer)
    if getattr(app_main.create_app, "_bridge_installers", None) is _app_installers:
        return
    original = app_main.create_app

    def create_app(*args: Any, **kwargs: Any) -> Any:
        app = original(*args, **kwargs)
        for install in _app_installers:
            install(app)
        return app

    create_app._bridge_installers = _app_installers  # type: ignore[attr-defined]
    app_main.create_app = create_app


def app_daemon(app: Any) -> Any | None:
    return getattr(app.state, "daemon", None)


def app_kinematics(app: Any) -> str:
    """The engine ``daemon/start`` will use for the next backend."""
    return app.state.args.kinematics_engine


def set_app_kinematics(app: Any, engine: str) -> None:
    app.state.args.kinematics_engine = engine


def backend_kinematics(backend: Any) -> str | None:
    return getattr(backend, "kinematics_engine", None)


# --- Media -------------------------------------------------------------------

AUDIO_QUEUE_ELEMENT = "queue_audiosrc"


def wrap_configure_audio(wrapper: Callable[..., None]) -> None:
    """Replace ``GstMediaServer._configure_audio`` with
    ``wrapper(original, media_server, pipeline, webrtcsink)``."""
    from reachy_mini.media.media_server import GstMediaServer

    _wrap(GstMediaServer, "_configure_audio", wrapper)


def wrap_configure_webrtc(wrapper: Callable[..., Any]) -> None:
    """Replace ``GstMediaServer._configure_webrtc`` with
    ``wrapper(original, media_server, pipeline)``; it returns the webrtcsink."""
    from reachy_mini.media.media_server import GstMediaServer

    _wrap(GstMediaServer, "_configure_webrtc", wrapper)


def media_server(daemon: Any) -> Any | None:
    return getattr(daemon, "_media_server", None)


def media_released(daemon: Any) -> bool:
    released = getattr(daemon, "media_released", False)
    return bool(released() if callable(released) else released)


def local_camera_available() -> bool:
    from reachy_mini.daemon.utils import is_local_camera_available

    return bool(is_local_camera_available())


def open_camera() -> Any:
    """A reader of the daemon's shared camera feed (never the device itself)."""
    from reachy_mini.media.camera_gstreamer import GStreamerCamera

    return GStreamerCamera(log_level="WARNING")


def ensure_reachymini_asoundrc() -> bool:
    """Write upstream's ``~/.asoundrc`` for Reachy Mini Audio; False if already set."""
    from reachy_mini.media.audio_utils import check_reachymini_asoundrc, write_asoundrc_to_home

    if check_reachymini_asoundrc():
        return False
    write_asoundrc_to_home()
    return True


def wrap_play_sound(wrapper: Callable[..., None]) -> None:
    """Replace ``Backend.play_sound`` with ``wrapper(original, backend, sound_file)``."""
    from reachy_mini.daemon.backend.abstract import Backend

    _wrap(Backend, "play_sound", wrapper)


def wrap_stop_sound(wrapper: Callable[..., None]) -> None:
    """Replace ``Backend.stop_sound`` with ``wrapper(original, backend)``."""
    from reachy_mini.daemon.backend.abstract import Backend

    _wrap(Backend, "stop_sound", wrapper)


def sound_path(sound_file: str) -> Path | None:
    """The file ``play_sound(sound_file)`` plays, resolved as the media server
    does (the path as given, else the name under the assets folder); None if
    neither exists."""
    from reachy_mini.utils.constants import ASSETS_ROOT_PATH

    for candidate in (Path(sound_file), Path(ASSETS_ROOT_PATH) / sound_file):
        if candidate.is_file():
            return candidate.resolve()
    return None


def sound_roots() -> tuple[Path, ...]:
    """The folders the daemon's own sounds come from: its assets, uploaded
    sounds, uploaded move audio, and the Hugging Face cache that move
    datasets (and their sounds) download into."""
    import tempfile

    from huggingface_hub.constants import HF_HUB_CACHE
    from reachy_mini.daemon.app.routers.media import SOUNDS_TMP_DIR
    from reachy_mini.utils.constants import ASSETS_ROOT_PATH

    roots = (ASSETS_ROOT_PATH, SOUNDS_TMP_DIR, Path(tempfile.gettempdir()) / _UPLOADS_DIR / "audio", HF_HUB_CACHE)
    # The upload folders sit in the shared temp dir: one another user made
    # first (or a link there) would widen what counts as a sound folder.
    return tuple(Path(root).resolve() for root in roots if _own_folder(Path(root)))


def _own_folder(path: Path) -> bool:
    import os

    try:
        return not path.is_symlink() and path.stat().st_uid == os.getuid()
    except OSError:
        return False
