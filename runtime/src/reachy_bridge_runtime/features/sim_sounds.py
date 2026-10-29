"""Hand a sim daemon's sounds to the browser (#219).

A sim daemon runs with ``--no-media`` (its WebRTC signalling would clash
with the robot daemon's :8443), so ``Backend.play_sound`` does nothing: the
wake-up chime, expression sounds and the test sound are lost. The host's
speaker isn't the place for them anyway: in sim mode the person is usually
somewhere else, looking at the dashboard.

So in a sim daemon every ``play_sound`` and ``stop_sound`` is recorded:

- ``GET /api/bridge/sounds`` returns the latest events
  (``{"seq": n, "events": [{"seq", "kind": "play"|"stop", "token"?, "name"?}]}``).
  The controller polls it while a dashboard listens and relays the events.
- ``GET /api/bridge/sounds/<token>`` serves the file a play event named.
  The only files it serves are ones the daemon itself was asked to play,
  each under a random token, and only the most recent ones: there is no path
  parameter to probe the disk with.

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
SOUND_SUFFIXES = frozenset({".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".opus"})


class SoundLog:
    """Thread-safe: ``play_sound`` runs on the daemon's loop and in move
    threads, the routes on the FastAPI loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._events: collections.deque[dict[str, Any]] = collections.deque(maxlen=EVENTS_KEPT)
        self._files: collections.OrderedDict[str, Path] = collections.OrderedDict()

    def played(self, path: Path) -> str:
        token = secrets.token_hex(16)
        with self._lock:
            self._seq += 1
            self._files[token] = path
            while len(self._files) > FILES_KEPT:
                self._files.popitem(last=False)
            self._events.append({"seq": self._seq, "kind": "play", "token": token, "name": path.name})
        return token

    def stopped(self) -> None:
        with self._lock:
            self._seq += 1
            self._events.append({"seq": self._seq, "kind": "stop"})

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"seq": self._seq, "events": [dict(event) for event in self._events]}

    def file(self, token: str) -> Path | None:
        with self._lock:
            return self._files.get(token)


LOG = SoundLog()


def _play_sound(original: Any, backend: Any, sound_file: str, *args: Any, **kwargs: Any) -> Any:
    result = original(backend, sound_file, *args, **kwargs)
    try:
        path = upstream.sound_path(str(sound_file))
        if path is None or path.suffix.lower() not in SOUND_SUFFIXES:
            logger.info("Sim sound %r not relayed: no such sound file", sound_file)
        else:
            LOG.played(path)
    except Exception:
        # A sound is never worth breaking a move over.
        logger.debug("Sim sound %r not recorded", sound_file, exc_info=True)
    return result


def _stop_sound(original: Any, backend: Any, *args: Any, **kwargs: Any) -> Any:
    result = original(backend, *args, **kwargs)
    LOG.stopped()
    return result


def install_route(app: Any) -> None:
    @app.get("/api/bridge/sounds", include_in_schema=False)
    async def sounds() -> dict[str, Any]:
        return LOG.snapshot()

    @app.get("/api/bridge/sounds/{token}", include_in_schema=False)
    async def sound(token: str) -> FileResponse:
        path = LOG.file(token)
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="No such sound")
        return FileResponse(path)


def apply() -> None:
    upstream.wrap_play_sound(_play_sound)
    upstream.wrap_stop_sound(_stop_sound)
    upstream.on_create_app(install_route)
    logger.info("Sim sounds: relayed to the dashboard")
