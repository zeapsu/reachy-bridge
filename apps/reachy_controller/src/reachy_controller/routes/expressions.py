"""Expression playback lifecycle: serialize, cancel, return-home."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

from reachy_controller.daemon_proxy import DaemonProxy

router = APIRouter(prefix="/api/expressions", tags=["expressions"])


class ExpressionPlayer:
    """Manages expression playback with one-at-a-time serialization and return-home.

    The daemon does not serialize play_move (its RLock guard is per-thread, but
    all daemon coroutines share one event-loop thread), so rapid plays run
    interleaved 100 Hz set_target loops (violent motion). This class enforces
    one-at-a-time via a lock during check-and-start, then releases it so the
    watcher task can run independently.

    The watcher is the single owner of all session-state transitions (playing →
    returning_home → cleared). stop() only fires the daemon stop request and never
    mutates the session; the watcher sees the uuid vanish and proceeds.
    """

    POLL_INTERVAL_S = 0.3  # Class attribute so tests can override

    def __init__(
        self,
        daemon_proxy: DaemonProxy,
        poll_interval_s: float | None = None,
    ) -> None:
        self.daemon_proxy = daemon_proxy
        if poll_interval_s is not None:
            self.POLL_INTERVAL_S = poll_interval_s
        self._session: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self._watcher: asyncio.Task[None] | None = None

    async def play(self, dataset: str, move: str) -> dict[str, Any]:
        """Start playing an expression. Returns session or error.

        Under lock: checks if already playing, checks daemon for foreign moves,
        POSTs play, stores session, spawns watcher, releases lock.

        Returns:
            {"status": "playing", "uuid": "...", "dataset": "...", "move": "..."}
            {"status": "busy", "detail": "..."}
            {"status": "unavailable", "detail": "..."}
            {"status": "not_found", "detail": "..."}
        """
        async with self._lock:
            # Check if already playing
            if self._session is not None:
                return {
                    "status": "busy",
                    "detail": "Robot is busy with another move.",
                }

            # Check for foreign running moves
            running_moves = await self.daemon_proxy.get_running_moves()
            if running_moves is None:
                return {
                    "status": "unavailable",
                    "detail": "Reachy daemon unavailable",
                }
            if running_moves:
                return {
                    "status": "busy",
                    "detail": "Robot is busy with another move.",
                }

            # POST play request
            try:
                result = await self.daemon_proxy.play_recorded_move(dataset, move)
                if result is None:
                    return {
                        "status": "unavailable",
                        "detail": "Reachy daemon unavailable",
                    }
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    return {"status": "not_found"}
                raise

            # Store session and spawn watcher
            uuid = result["uuid"]
            self._session = {
                "uuid": uuid,
                "dataset": dataset,
                "move": move,
                "phase": "playing",
            }
            self._watcher = asyncio.create_task(self._watch())

            return {
                "status": "playing",
                "uuid": uuid,
                "dataset": dataset,
                "move": move,
            }

    async def stop(self) -> dict[str, str]:
        """Stop the current expression playback.

        If no session: returns idle.
        If returning_home: returns returning_home (idempotent).
        If playing: fires daemon stop request (best-effort) and returns stopping.
        The watcher sees the uuid vanish and proceeds to return-home.

        Returns:
            {"status": "idle"} | {"status": "returning_home"} | {"status": "stopping"}
        """
        # Snapshot: keeps the reads coherent even if a future edit adds an
        # await before them (the watcher clears _session at its own awaits).
        session = self._session
        if session is None:
            return {"status": "idle"}

        if session["phase"] == "returning_home":
            return {"status": "returning_home"}

        # Fire stop request (best-effort, don't mutate session)
        await self.daemon_proxy.stop_move(session["uuid"])

        return {"status": "stopping"}

    async def current(self) -> dict[str, Any]:
        """Return current session state.

        Returns:
            {"active": false} or
            {"active": true, "phase": "playing" | "returning_home", "dataset": "...", "move": "...", "uuid": "..."}
        """
        session = self._session
        if session is None:
            return {"active": False}

        return {
            "active": True,
            "phase": session["phase"],
            "dataset": session["dataset"],
            "move": session["move"],
            "uuid": session["uuid"],
        }

    async def aclose(self) -> None:
        """Cancel the watcher and clean up."""
        if self._watcher is not None:
            self._watcher.cancel()
            try:
                await self._watcher
            except asyncio.CancelledError:
                pass

    async def _watch(self) -> None:
        """Poll daemon until our uuid is absent, then trigger return-home.

        Single owner of all session state transitions:
        - playing → (uuid vanishes) → returning_home
        - returning_home → (wake_up uuid vanishes) → clear session

        Any daemon error (None response) at any point clears the session and
        stops (no return-home possible). Never raises.
        """
        if self._session is None:
            return

        try:
            # Poll until our uuid is absent (move finished)
            playing_uuid = self._session["uuid"]
            while True:
                await asyncio.sleep(self.POLL_INTERVAL_S)
                running = await self.daemon_proxy.get_running_moves()
                if running is None:
                    # Daemon unavailable, clear and stop
                    self._session = None
                    return
                if not any(m["uuid"] == playing_uuid for m in running):
                    # Our move finished
                    break

            # Transition to returning_home and play wake_up
            if self._session is not None:
                self._session["phase"] = "returning_home"
            else:
                return

            wake_up_result = await self.daemon_proxy.play_wake_up()
            if wake_up_result is None:
                # Daemon unavailable, clear and stop
                self._session = None
                return

            wake_up_uuid = wake_up_result["uuid"]

            # Poll until wake_up uuid is absent
            while True:
                await asyncio.sleep(self.POLL_INTERVAL_S)
                running = await self.daemon_proxy.get_running_moves()
                if running is None:
                    # Daemon unavailable, clear and stop
                    self._session = None
                    return
                if not any(m["uuid"] == wake_up_uuid for m in running):
                    # Wake_up finished
                    break

            # Clear session
            self._session = None

        except asyncio.CancelledError:
            # Task cancelled, clean up
            pass
        except Exception:
            # Swallow any exception; watcher is fire-and-forget
            self._session = None


@router.post("/play/{dataset:path}/{move_name}")
async def play_expression(
    dataset: str,
    move_name: str,
    request: Request,
) -> dict[str, Any]:
    """POST /api/expressions/play/{dataset}/{move_name}."""
    player: ExpressionPlayer = request.app.state.expression_player
    result = await player.play(dataset, move_name)

    # Map status to HTTP status code
    if result["status"] == "busy":
        raise HTTPException(status_code=409, detail=result["detail"])
    elif result["status"] == "unavailable":
        raise HTTPException(status_code=503, detail=result["detail"])
    elif result["status"] == "not_found":
        raise HTTPException(status_code=404, detail="Unknown move or dataset")

    return result


@router.post("/stop")
async def stop_expression(request: Request) -> dict[str, str]:
    """POST /api/expressions/stop."""
    player: ExpressionPlayer = request.app.state.expression_player
    return await player.stop()


@router.get("/current")
async def get_current_expression(request: Request) -> dict[str, Any]:
    """GET /api/expressions/current."""
    player: ExpressionPlayer = request.app.state.expression_player
    return await player.current()
