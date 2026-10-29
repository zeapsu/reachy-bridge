"""Hand a sim daemon's sounds to the browser (#219).

A sim daemon runs with ``--no-media`` (its WebRTC signalling would clash
with the robot daemon's :8443), so ``Backend.play_sound`` does nothing: the
wake-up chime, expression sounds and the test sound are lost. The host's
speaker isn't the place for them anyway: in sim mode the person is usually
somewhere else, looking at the dashboard.

So in a sim daemon every ``play_sound`` and ``stop_sound`` is recorded:

- ``GET /api/bridge/sounds`` returns the latest events
  (``{"boot": id, "seq": n, "events": [{"seq", "kind": "play"|"stop", "token"?, "name"?}]}``;
  ``boot`` changes when the daemon restarts, so a dashboard knows ``seq``
  started over).
  The controller polls it while a dashboard listens and relays the events.
- ``GET /api/bridge/sounds/<token>`` serves the file a play event named.
  The only files it serves are ones the daemon itself was asked to play,
  each under a random token, and only the most recent ones: there is no path
  parameter to probe the disk with. ``play_sound`` can be handed any path,
  so only files under the daemon's own sound folders are recorded, and
  checked again when served (``upstream.sound_roots``): its assets, uploads,
  and the Hugging Face cache move datasets live in.

Daemon access goes through ``reachy_bridge_runtime.upstream``.
"""

from __future__ import annotations

import collections
import logging
import secrets
import threading
from pathlib import Path
from typing import Any

# Module level: FastAPI resolves the (string) annotations from here.
from fastapi import HTTPException
from fastapi.responses import FileResponse

from reachy_bridge_runtime import upstream

logger = logging.getLogger(__name__)

FEATURE = "sim_sounds"

# Events the route returns: a burst between two controller polls fits.
EVENTS_KEPT = 16
# Files that stay servable; an old token 404s, which a late tab survives.
FILES_KEPT = 32
# Explicit types: the host's MIME table may not know them, and the
# controller passes only audio/* on.
SOUND_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
}


class SoundLog:
    """Thread-safe: ``play_sound`` runs on the daemon's loop and in move
    threads, the routes on the FastAPI loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._boot = secrets.token_hex(8)
        self._seq = 0
        self._events: collections.deque[dict[str, Any]] = collections.deque(maxlen=EVENTS_KEPT)
        # token -> (resolved file, its audio type)
        self._files: collections.OrderedDict[str, tuple[Path, str]] = collections.OrderedDict()

    def played(self, path: Path, name: str, media_type: str) -> str:
        token = secrets.token_hex(16)
        with self._lock:
            self._seq += 1
            self._files[token] = (path, media_type)
            while len(self._files) > FILES_KEPT:
                self._files.popitem(last=False)
            self._events.append({"seq": self._seq, "kind": "play", "token": token, "name": name})
        return token

    def stopped(self) -> None:
        with self._lock:
            self._seq += 1
            self._events.append({"seq": self._seq, "kind": "stop"})

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"boot": self._boot, "seq": self._seq, "events": [dict(event) for event in self._events]}

    def file(self, token: str) -> tuple[Path, str] | None:
        with self._lock:
            return self._files.get(token)


LOG = SoundLog()


def _in_sound_folders(path: Path) -> bool:
    return any(path.is_relative_to(root) for root in upstream.sound_roots())


def _play_sound(original: Any, backend: Any, sound_file: str, *args: Any, **kwargs: Any) -> Any:
    result = original(backend, sound_file, *args, **kwargs)
    try:
        # The type comes from the name the daemon was given: a move sound in
        # the Hugging Face cache is a symlink to a blob with no extension.
        name = Path(str(sound_file)).name
        media_type = SOUND_TYPES.get(Path(name).suffix.lower())
        path = upstream.sound_path(str(sound_file))
        if path is None or media_type is None:
            logger.info("Sim sound %r not relayed: no such sound file", sound_file)
        elif not _in_sound_folders(path):
            logger.warning("Sim sound %r not relayed: not one of the daemon's sound folders", sound_file)
        else:
            LOG.played(path, name, media_type)
    except Exception:
        # A sound is never worth breaking a move over.
        logger.debug("Sim sound %r not recorded", sound_file, exc_info=True)
    return result


def _stop_sound(original: Any, backend: Any, *args: Any, **kwargs: Any) -> Any:
    result = original(backend, *args, **kwargs)
    try:
        LOG.stopped()
    except Exception:
        logger.debug("Sim sound stop not recorded", exc_info=True)
    return result


def install_route(app: Any) -> None:
    @app.get("/api/bridge/sounds", include_in_schema=False)
    async def sounds() -> dict[str, Any]:
        return LOG.snapshot()

    @app.get("/api/bridge/sounds/{token}", include_in_schema=False)
    async def sound(token: str) -> FileResponse:
        recorded = LOG.file(token)
        if recorded is None:
            raise HTTPException(status_code=404, detail="No such sound")
        path, media_type = recorded
        # Checked again: the file may have been replaced since it played.
        if not path.is_file() or not _in_sound_folders(path.resolve()):
            raise HTTPException(status_code=404, detail="No such sound")
        return FileResponse(path, media_type=media_type, headers={"X-Content-Type-Options": "nosniff"})


def apply() -> None:
    upstream.wrap_play_sound(_play_sound)
    upstream.wrap_stop_sound(_stop_sound)
    upstream.on_create_app(install_route)
    logger.info("Sim sounds: relayed to the dashboard")
