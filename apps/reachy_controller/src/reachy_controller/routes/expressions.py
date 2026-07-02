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
        # #67 safety: set by the motor-mode route on any torque change; cleared
        # only when a goto-home verifiably completes. While True, play() refuses
        # (jolt hazard: torque on, pose unknown).
        self.needs_home = False

    @property
    def active(self) -> bool:
        """True while any session (playing or returning home) exists."""
        return self._session is not None

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

            if self.needs_home:
                return {
                    "status": "busy",
                    "detail": "Robot pose is not homed — set motor mode to enabled to return home.",
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

            # Store session and spawn watcher. Guard against a malformed 200
            # (missing uuid) — treat as unavailable rather than KeyError with
            # the robot already moving untracked.
            uuid = result.get("uuid")
            if uuid is None:
                return {
                    "status": "unavailable",
                    "detail": "Reachy daemon unavailable",
                }
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

    async def return_home(self) -> dict[str, Any]:
        """Standalone return-home session (post-motor-enable recovery, #67).

        Reuses the session machinery so play() and the app-start guard block
        for free, and the frontend expression poller renders it with zero new
        UI. No daemon I/O under the lock — the goto runs in the watcher — so
        the session exists before the caller's route returns.

        Returns:
            {"status": "returning_home"} | {"status": "busy", "detail": "..."}
        """
        async with self._lock:
            if self._session is not None:
                return {"status": "busy", "detail": "Robot is busy with another move."}
            self._session = {
                "uuid": None,
                "dataset": None,
                "move": None,
                "phase": "returning_home",
            }
            self._watcher = asyncio.create_task(self._watch_return_home())
            return {"status": "returning_home"}

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

    async def _poll_until_absent(self, target_uuid: str) -> bool:
        """Poll get_running_moves until target_uuid is gone.

        A single transient None must not cost the return-home; only two
        consecutive None polls mean the daemon is actually gone.

        Returns True once the move is absent, False if the daemon is
        confirmed gone (two consecutive None polls).
        """
        consecutive_failures = 0
        while True:
            await asyncio.sleep(self.POLL_INTERVAL_S)
            running = await self.daemon_proxy.get_running_moves()
            if running is None:
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    return False
                continue
            consecutive_failures = 0
            if not any(m["uuid"] == target_uuid for m in running):
                return True

    async def _goto_home_and_clear(self) -> None:
        """Second half of every lifecycle: goto home (one retry), poll the
        goto uuid out, clear the session."""
        home_result = await self.daemon_proxy.goto_home()
        if home_result is None:
            # A single transient failure must not cost the return-home;
            # retry once before giving up.
            await asyncio.sleep(self.POLL_INTERVAL_S)
            home_result = await self.daemon_proxy.goto_home()
            if home_result is None:
                self._session = None
                return

        # Poll until the goto-home uuid is absent
        if not await self._poll_until_absent(home_result["uuid"]):
            self._session = None
            return

        self.needs_home = False  # robot is verifiably at home
        self._session = None

    async def _watch(self) -> None:
        """Poll daemon until our uuid is absent, then trigger return-home.

        Single owner of all session state transitions:
        - playing → (uuid vanishes) → returning_home
        - returning_home → (goto-home uuid vanishes) → clear session

        Two consecutive daemon errors (None response) at any point clear the
        session and stop (no return-home possible). Never raises.
        """
        if self._session is None:
            return

        try:
            # Poll until our uuid is absent (move finished)
            playing_uuid = self._session["uuid"]
            if not await self._poll_until_absent(playing_uuid):
                self._session = None
                return

            # Transition to returning_home and goto the home pose (silent —
            # the wake_up emote's wav/gesture felt unnatural after expressions)
            if self._session is not None:
                self._session["phase"] = "returning_home"
            else:
                return

            await self._goto_home_and_clear()

        except asyncio.CancelledError:
            # Task cancelled, clean up
            pass
        except Exception:
            # Swallow any exception; watcher is fire-and-forget
            self._session = None

    async def _watch_return_home(self) -> None:
        """Watcher for a standalone return-home session. Never raises."""
        try:
            await self._goto_home_and_clear()
        except asyncio.CancelledError:
            pass
        except Exception:
            self._session = None


@router.post("/play/{dataset:path}/{move_name}")
async def play_expression(
    dataset: str,
    move_name: str,
    request: Request,
) -> dict[str, Any]:
    """POST /api/expressions/play/{dataset}/{move_name}."""
    # ponytail: check-then-act across app/expression state is non-atomic; single-user UI
    if request.app.state.app_supervisor.any_app_active():
        raise HTTPException(
            status_code=409, detail="An app is controlling the robot — stop it first."
        )
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
