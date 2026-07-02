# Robot Arbitration + Motor-Enable Safety + Mic Self-Heal (#65 #66 #67) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> At execution start, copy this plan to `docs/superpowers/plans/2026-07-02-arbitration-motor-safety-mic.md` in the worktree.

**Goal:** One PR closing #65 (mic self-heal via `~/.asoundrc`), #66 (apps ↔ expressions mutual blocking), and #67 (graceful return-home after motor enable — jolt-safety).

**Architecture:** All controller work rides the #64 `ExpressionPlayer` session machinery: #66 adds route-layer cross-guards between `app.state.app_supervisor` and `app.state.expression_player`; #67 adds a `return_home()` session (phase `returning_home`, no play half) kicked by the motor-mode route, plus a `needs_home` safety flag so expressions refuse while the pose is untrusted. #65 is a self-contained 2-edit fix in the Jetson daemon wrapper. No other open issue bundles cleanly (#34 is a separate owner-design feature; the rest are unrelated domains).

**Tech Stack:** FastAPI + httpx (controller backend), vanilla JS (no build step), pytest (`asyncio_mode="auto"`), uv workspace.

## Context

- **#67 (SAFETY, drives the design):** disabling motors lets the head droop; re-enabling just flips torque without re-establishing pose; the next expression commands absolute targets from the *recorded* start pose → violent jolt, hardware risk. Fix: after the controller's set-mode route flips to `enabled`, run the graceful daemon `goto_home` (MIN_JERK, 1.5 s, interpolates from the *actual* current pose) as an ExpressionPlayer session, and refuse expression plays (`needs_home` flag) until a return-home has verifiably completed.
- **#66:** an app driving `set_target` holds no daemon move and no app-lock entry an expression checks, and vice versa — the two actuating surfaces interleave 100 Hz control loops (same violent class as #64). Fix: cheap in-process cross-checks at the route layer, plus UI gating with the existing disabled/tooltip machinery.
- **#65:** PipeWire on the Jetson currently exposes only a Sink for "Reachy Mini Audio" (no Source), so the SDK falls back to `autoaudiosrc` and captures the Sink's own loopback — `reachy_chat` never hears speech. The SDK's `~/.asoundrc` ALSA-direct bypass exists but is gated behind `--wireless-version`; provision it from the Jetson wrapper.

**Key payoff of the session-reuse design:** a `return_home()` session makes expressions auto-refuse (session → busy → 409), makes the new #66 app-start guard auto-refuse (`player.active`), and the frontend's existing 750 ms expression poller renders the whole thing — near-zero new UI.

## Global Constraints

- Run backend tests as `cd apps/reachy_controller && uv run python -m pytest` — NOT `uv run pytest`.
- After creating the worktree: `uv sync --all-packages` from the worktree root (plain `uv sync` half-breaks the env).
- Frontend is vanilla JS, no framework/bundler, no JS test harness — frontend verification is manual/Playwright (see Verification).
- All `gh` commands: `--repo zeapsu/reachy-console`. PR base: `feat/reachy-chat-vision`. Branch: `feat/robot-arbitration-motor-safety`.
- Match existing error payload shapes exactly: expressions busy → internal `{"status": "busy", "detail": ...}` → route 409; app-start refusal → HTTP **200** with `{"error": ..., "name": ..., "status": "refused"}` (do NOT remap to 409 — frontend inspects the body).
- The apps↔expressions check-then-act across two independent locks is non-atomic by design (single-user UI) — mark with a `ponytail:` comment, don't add a shared lock.
- Nothing in this plan touches the real robot; hardware QA is a separate owner-coordinated pass (see Verification).

## File Structure

| File | Responsibility in this PR |
|---|---|
| `tools/reachy_mini_daemon_jetson.py` | #65: `_ensure_reachymini_asoundrc()` + `main()` call |
| `apps/reachy_controller/src/reachy_controller/app_supervisor.py` | #66: public `any_app_active()` helper (reused by `start_app`) |
| `apps/reachy_controller/src/reachy_controller/routes/expressions.py` | #66: `ExpressionPlayer.active` property + play-route guard; #67: `_goto_home_and_clear()` extraction, `return_home()`, `needs_home` |
| `apps/reachy_controller/src/reachy_controller/routes/apps.py` | #66: start-route expression guard |
| `apps/reachy_controller/src/reachy_controller/routes/controls.py` | #67: motor-mode route kicks return-home + sets `needs_home` |
| `apps/reachy_controller/src/reachy_controller/static/main.js` | #66 + #67 UI gating (existing machinery only) |
| `apps/reachy_controller/tests/test_arbitration.py` | NEW: cross-surface integration tests (full-app + MockTransport) |
| `apps/reachy_controller/tests/test_expressions.py` | #67 unit tests (MockDaemonProxy pattern) |
| `CLAUDE.md`/`AGENTS.md` (symlinked) | doc blurb update |

Existing utilities to reuse (do not re-implement): `DaemonProxy.goto_home()` (`daemon_proxy.py:200`, POST `move/goto`, 1.5 s, one-retry pattern already in the watcher), `_poll_until_absent()` (`routes/expressions.py:167`), the `MockDaemonProxy` test class (`tests/test_expressions.py:17-28`), the full-app MockTransport pattern (`tests/test_expressions.py:395-454`).

---

### Task 1: #65 — asoundrc self-heal in the Jetson daemon wrapper

**Files:**
- Modify: `tools/reachy_mini_daemon_jetson.py` (add function after `_set_reachy_audio_output_volume`, ~line 267; add call in `main()`, line 274)

**Interfaces:**
- Produces: `_ensure_reachymini_asoundrc() -> None` (module-private, called only from `main()`)

No automated test: `tools/` has no harness, both branches sit behind `_is_jetson()` + an SDK import that isn't a controller dependency — a test would mock 100% of what it touches. Verification is the hardware checklist in the Verification section (from issue #65).

- [ ] **Step 1: Add the function** (issue #65's code + a resilience wrap matching `_set_reachy_audio_output_volume`; a broken mic bypass must not cost the whole daemon):

```python
def _ensure_reachymini_asoundrc() -> None:
    """Provision ~/.asoundrc so audio bypasses PipeWire's flaky USB-audio
    profile picker (PipeWire sometimes only exposes a Sink for the "Reachy
    Mini Audio" card, silently starving mic capture — #65). Upstream only
    does this for --wireless-version; Lite hardware needs it here too.
    """
    if not _is_jetson():
        return
    try:
        from reachy_mini.media.audio_utils import (
            check_reachymini_asoundrc,
            write_asoundrc_to_home,
        )

        if check_reachymini_asoundrc():
            logger.info("~/.asoundrc already correctly configured for Reachy Mini Audio.")
            return
        write_asoundrc_to_home()
        logger.info("Wrote ~/.asoundrc for Reachy Mini Audio (ALSA-direct, bypassing PipeWire).")
    except Exception as e:
        logger.warning("Could not provision ~/.asoundrc for Reachy Mini Audio: %s", e)
```

- [ ] **Step 2: Call it in `main()`** — insert after `_set_reachy_audio_output_volume()`:

```python
def main() -> int:
    logging.basicConfig(level=logging.INFO)
    _apply_nvidia_unixfd_patch()
    _apply_webrtcsink_compat_patch()
    _apply_camera_ipc_patch()
    _set_reachy_audio_output_volume()
    _ensure_reachymini_asoundrc()
    from reachy_mini.daemon.app.main import main as daemon_main

    return daemon_main()
```

- [ ] **Step 3: Syntax check**

Run: `uv run python -c "import ast; ast.parse(open('tools/reachy_mini_daemon_jetson.py').read())"`
Expected: no output (exit 0)

- [ ] **Step 4: Commit**

```bash
git add tools/reachy_mini_daemon_jetson.py
git commit -m "fix(jetson): self-heal ~/.asoundrc so mic capture bypasses PipeWire's Sink-only profile (#65)"
```

---

### Task 2: #66 primitives — `any_app_active()` + `ExpressionPlayer.active`

**Files:**
- Modify: `apps/reachy_controller/src/reachy_controller/app_supervisor.py` (new method after `camera_holder`, ~line 143; reuse at `start_app` ~line 209)
- Modify: `apps/reachy_controller/src/reachy_controller/routes/expressions.py` (property after `__init__`, ~line 42)
- Test: `apps/reachy_controller/tests/test_arbitration.py` (NEW)

**Interfaces:**
- Produces: `AppSupervisor.any_app_active() -> bool`; `ExpressionPlayer.active -> bool` (property). Tasks 3, 4, and 8 consume these.

- [ ] **Step 1: Write the failing tests** — create `apps/reachy_controller/tests/test_arbitration.py`:

```python
"""Cross-surface arbitration: apps <-> expressions (#66) + motor-enable return-home (#67)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from reachy_controller.app_supervisor import AppSupervisor
from reachy_controller.daemon_proxy import DaemonProxy
from reachy_controller.main import create_app
from reachy_controller.routes.expressions import ExpressionPlayer


def _make_app(handler):
    proxy = DaemonProxy("http://daemon.local:8000", transport=httpx.MockTransport(handler))
    return create_app(proxy)


def test_any_app_active_states() -> None:
    supervisor = AppSupervisor()
    assert supervisor.any_app_active() is False
    for status, expected in (
        ("running", True),
        ("loading", True),
        ("stopped", False),
        ("stopping", False),
        ("error", False),
    ):
        supervisor._apps["reachy_chat"].status = status
        assert supervisor.any_app_active() is expected, status


def test_expression_player_active_property() -> None:
    player = ExpressionPlayer(AsyncMock())
    assert player.active is False
    player._session = {"uuid": None, "dataset": None, "move": None, "phase": "returning_home"}
    assert player.active is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/reachy_controller && uv run python -m pytest tests/test_arbitration.py -v`
Expected: FAIL with `AttributeError: 'AppSupervisor' object has no attribute 'any_app_active'` / `'ExpressionPlayer' object has no attribute 'active'`

- [ ] **Step 3: Implement** — in `app_supervisor.py`, after `camera_holder()`:

```python
    def any_app_active(self) -> bool:
        """True when any managed app is running or loading (robot actuating)."""
        return any(a.status in ("running", "loading") for a in self._apps.values())
```

and replace the inline check in `start_app()` (currently `any_running = any(a.status in ("running", "loading") for a in self._apps.values())` + `if any_running:`) with:

```python
            # Refuse if any app is already running or loading
            if self.any_app_active():
                return {
                    "error": "Another app is already running",
                    "name": name,
                    "status": "refused",
                }
```

In `routes/expressions.py`, after `__init__`:

```python
    @property
    def active(self) -> bool:
        """True while any session (playing or returning home) exists."""
        return self._session is not None
```

(Deliberately excludes `"stopping"` from `any_app_active` — preserves current `start_app` semantics; flag in the PR body.)

- [ ] **Step 4: Run tests to verify they pass, plus regressions**

Run: `cd apps/reachy_controller && uv run python -m pytest tests/test_arbitration.py tests/test_app_supervisor.py -v`
Expected: PASS (incl. existing `test_single_app_lock`)

- [ ] **Step 5: Commit**

```bash
git add apps/reachy_controller/src/reachy_controller/app_supervisor.py apps/reachy_controller/src/reachy_controller/routes/expressions.py apps/reachy_controller/tests/test_arbitration.py
git commit -m "feat(reachy_controller): arbitration primitives — any_app_active() + ExpressionPlayer.active (#66)"
```

---

### Task 3: #66 guard A — expression play 409s while an app is active

**Files:**
- Modify: `apps/reachy_controller/src/reachy_controller/routes/expressions.py` (route `play_expression`, ~line 244)
- Test: `apps/reachy_controller/tests/test_arbitration.py`

**Interfaces:**
- Consumes: `AppSupervisor.any_app_active()` (Task 2)

- [ ] **Step 1: Write the failing tests** — append to `test_arbitration.py`:

```python
@pytest.mark.asyncio
async def test_play_expression_409_while_app_running() -> None:
    play_posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/move/play/"):
            play_posts.append(request.url.path)
            return httpx.Response(200, json={"uuid": "u1"})
        if request.url.path == "/api/move/running":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    app = _make_app(handler)
    async with app.router.lifespan_context(app):
        app.state.app_supervisor._apps["reachy_chat"].status = "running"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/expressions/play/dataset1/move1")
            assert response.status_code == 409
            assert "app" in response.json()["detail"].lower()
    assert play_posts == []  # never reached the daemon


@pytest.mark.asyncio
async def test_play_expression_409_while_app_loading() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    app = _make_app(handler)
    async with app.router.lifespan_context(app):
        app.state.app_supervisor._apps["reachy_dance"].status = "loading"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/expressions/play/dataset1/move1")
            assert response.status_code == 409
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/reachy_controller && uv run python -m pytest tests/test_arbitration.py -v -k "409_while_app"`
Expected: FAIL — response is 200 (or 404 in the second test), not 409

- [ ] **Step 3: Implement** — in `play_expression`, before calling the player:

```python
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
    # ... (existing status mapping unchanged)
```

- [ ] **Step 4: Run the expressions suites**

Run: `cd apps/reachy_controller && uv run python -m pytest tests/test_arbitration.py tests/test_expressions.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/reachy_controller/src/reachy_controller/routes/expressions.py apps/reachy_controller/tests/test_arbitration.py
git commit -m "feat(reachy_controller): expression play 409s while an app is active (#66)"
```

---

### Task 4: #66 guard B — app start refused while an expression session is active

**Files:**
- Modify: `apps/reachy_controller/src/reachy_controller/routes/apps.py` (route `start_app`, ~line 19)
- Test: `apps/reachy_controller/tests/test_arbitration.py`

**Interfaces:**
- Consumes: `ExpressionPlayer.active` (Task 2)

- [ ] **Step 1: Write the failing tests** — append to `test_arbitration.py`:

```python
def _expr_session(phase: str = "playing") -> dict:
    return {"uuid": "u1" if phase == "playing" else None,
            "dataset": None, "move": None, "phase": phase}


@pytest.mark.asyncio
async def test_app_start_refused_while_expression_playing() -> None:
    app = _make_app(lambda request: httpx.Response(404))
    async with app.router.lifespan_context(app):
        app.state.expression_player._session = _expr_session("playing")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            with patch.object(AppSupervisor, "start_app", new=AsyncMock()) as start:
                response = await client.post("/api/apps/start/reachy_chat")
            assert response.status_code == 200  # existing refusal shape, not 409
            body = response.json()
            assert body["status"] == "refused"
            assert body["name"] == "reachy_chat"
            assert "expression" in body["error"].lower()
            start.assert_not_awaited()  # refused before delegation


@pytest.mark.asyncio
async def test_app_start_refused_while_returning_home() -> None:
    """Pins #67 auto-blocking: a return-home session also refuses app starts."""
    app = _make_app(lambda request: httpx.Response(404))
    async with app.router.lifespan_context(app):
        app.state.expression_player._session = _expr_session("returning_home")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/apps/start/reachy_chat")
            assert response.json()["status"] == "refused"


@pytest.mark.asyncio
async def test_app_start_allowed_when_expression_idle() -> None:
    """The guard must not over-block: no session -> delegates to the supervisor."""
    app = _make_app(lambda request: httpx.Response(404))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            with patch.object(
                AppSupervisor,
                "start_app",
                new=AsyncMock(return_value={"name": "reachy_dance", "status": "loading", "pid": 1}),
            ) as start:
                response = await client.post("/api/apps/start/reachy_dance")
            assert response.json()["status"] == "loading"
            start.assert_awaited_once_with("reachy_dance")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/reachy_controller && uv run python -m pytest tests/test_arbitration.py -v -k "app_start"`
Expected: the two `refused` tests FAIL (start_app is reached / status not refused); the `allowed` test may already pass

- [ ] **Step 3: Implement** — in `routes/apps.py`:

```python
@router.post("/apps/start/{name}")
async def start_app(name: str, request: Request) -> dict[str, Any]:
    """Start an app. Returns status dict with error if refused."""
    # ponytail: check-then-act across app/expression state is non-atomic; single-user UI
    if request.app.state.expression_player.active:
        return {
            "error": "Robot is playing an expression — wait for it to finish.",
            "name": name,
            "status": "refused",
        }
    supervisor = request.app.state.app_supervisor
    result = await supervisor.start_app(name)
    # ... (existing 404 mapping unchanged)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/reachy_controller && uv run python -m pytest tests/test_arbitration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/reachy_controller/src/reachy_controller/routes/apps.py apps/reachy_controller/tests/test_arbitration.py
git commit -m "feat(reachy_controller): app start refused while an expression session is active (#66)"
```

---

### Task 5: #67 prep — extract `_goto_home_and_clear()` from `_watch()` (behavior-preserving)

**Files:**
- Modify: `apps/reachy_controller/src/reachy_controller/routes/expressions.py` (`_watch`, lines 189-241)

**Interfaces:**
- Produces: `ExpressionPlayer._goto_home_and_clear() -> None` (async; Tasks 6-7 consume it)

Pure refactor — NO new tests; the existing suite (which pins the goto-retry and two-None-give-up sequences) is the safety net. Run it before and after.

- [ ] **Step 1: Confirm green baseline**

Run: `cd apps/reachy_controller && uv run python -m pytest tests/test_expressions.py -v`
Expected: PASS (all)

- [ ] **Step 2: Extract.** New method (below `_poll_until_absent`); `_watch` keeps its try/except and phase flip:

```python
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

        self._session = None
```

`_watch()` becomes:

```python
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
```

- [ ] **Step 3: Verify still green**

Run: `cd apps/reachy_controller && uv run python -m pytest tests/test_expressions.py tests/test_arbitration.py -v`
Expected: PASS (identical set)

- [ ] **Step 4: Commit**

```bash
git add apps/reachy_controller/src/reachy_controller/routes/expressions.py
git commit -m "refactor(reachy_controller): extract _goto_home_and_clear from the expression watcher (#67 prep)"
```

---

### Task 6: #67 — `return_home()` standalone session

**Files:**
- Modify: `apps/reachy_controller/src/reachy_controller/routes/expressions.py`
- Test: `apps/reachy_controller/tests/test_expressions.py`

**Interfaces:**
- Consumes: `_goto_home_and_clear()` (Task 5)
- Produces: `ExpressionPlayer.return_home() -> dict[str, Any]` — returns `{"status": "returning_home"}` or `{"status": "busy", "detail": "Robot is busy with another move."}`. Task 8's route consumes it. Session shape keeps all four keys with `None` values (`{"uuid": None, "dataset": None, "move": None, "phase": "returning_home"}`) so `stop()`/`current()` never KeyError and the frontend sync-on-load chip selector safely matches nothing.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_expressions.py`:

```python
@pytest.mark.asyncio
async def test_return_home_creates_returning_home_session() -> None:
    """return_home() runs a session with no play half: goto fires, session
    clears when the goto uuid drains. Pins the null-field session shape the
    frontend sync-on-load path depends on (selector built from `move`)."""
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy, poll_interval_s=0.01)
    proxy.goto_home.return_value = {"uuid": "home-uuid"}
    proxy.get_running_moves.side_effect = [
        [{"uuid": "home-uuid"}],  # goto still running
        [],  # done
    ]

    result = await player.return_home()
    assert result == {"status": "returning_home"}

    current = await player.current()
    assert current["active"] is True
    assert current["phase"] == "returning_home"
    assert current["dataset"] is None
    assert current["move"] is None
    assert current["uuid"] is None

    await player._watcher
    assert (await player.current())["active"] is False
    proxy.goto_home.assert_called_once()
    await player.aclose()


@pytest.mark.asyncio
async def test_return_home_busy_when_session_active() -> None:
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy)
    proxy.play_recorded_move.return_value = {"uuid": "u1"}
    await player.play("dataset1", "move1")
    proxy.get_running_moves.return_value = [{"uuid": "u1"}]  # hold phase=playing

    result = await player.return_home()
    assert result["status"] == "busy"
    proxy.goto_home.assert_not_called()
    await player.aclose()


@pytest.mark.asyncio
async def test_stop_during_return_home_session_is_noop() -> None:
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy, poll_interval_s=0.01)
    proxy.goto_home.return_value = {"uuid": "home-uuid"}
    proxy.get_running_moves.return_value = [{"uuid": "home-uuid"}]  # goto in flight

    await player.return_home()
    result = await player.stop()
    assert result == {"status": "returning_home"}
    proxy.stop_move.assert_not_called()

    proxy.get_running_moves.return_value = []  # let it finish
    await player._watcher
    assert player.active is False
    await player.aclose()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/reachy_controller && uv run python -m pytest tests/test_expressions.py -v -k "return_home"`
Expected: FAIL with `AttributeError: 'ExpressionPlayer' object has no attribute 'return_home'`

- [ ] **Step 3: Implement** — in `ExpressionPlayer`, after `play()`:

```python
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

    async def _watch_return_home(self) -> None:
        """Watcher for a standalone return-home session. Never raises."""
        try:
            await self._goto_home_and_clear()
        except asyncio.CancelledError:
            pass
        except Exception:
            self._session = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/reachy_controller && uv run python -m pytest tests/test_expressions.py -v`
Expected: PASS (new + all existing)

- [ ] **Step 5: Commit**

```bash
git add apps/reachy_controller/src/reachy_controller/routes/expressions.py apps/reachy_controller/tests/test_expressions.py
git commit -m "feat(reachy_controller): standalone return_home() session on the expression player (#67)"
```

---

### Task 7: #67 — `needs_home` safety flag

**Files:**
- Modify: `apps/reachy_controller/src/reachy_controller/routes/expressions.py`
- Test: `apps/reachy_controller/tests/test_expressions.py`

**Interfaces:**
- Produces: `ExpressionPlayer.needs_home: bool` (public attr, default `False`). Task 8's route sets it; `_goto_home_and_clear()` clears it only on verified completion; `play()` refuses while it is set.

Safety rationale (keep, don't YAGNI away): the exact #67 hazard is torque on + pose unknown. Without the flag, a failed goto dispatch after enable clears the session and expressions unblock straight into the jolt. Side effect worth flagging in the PR: play-while-motors-disabled now 409s with an actionable message instead of silently "playing" with torque off.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_expressions.py`:

```python
@pytest.mark.asyncio
async def test_needs_home_blocks_play_with_actionable_detail() -> None:
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy)
    player.needs_home = True

    result = await player.play("dataset1", "move1")
    assert result["status"] == "busy"
    assert "enabled" in result["detail"]  # detail names the recovery action
    proxy.play_recorded_move.assert_not_called()
    await player.aclose()


@pytest.mark.asyncio
async def test_needs_home_persists_when_goto_dispatch_fails() -> None:
    """SAFETY: goto dispatch fails twice -> session clears but the flag stays
    set, so expressions keep refusing (safe direction)."""
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy, poll_interval_s=0.01)
    player.needs_home = True
    proxy.goto_home.return_value = None  # both dispatch attempts fail

    await player.return_home()
    await player._watcher

    assert player.active is False
    assert player.needs_home is True
    result = await player.play("dataset1", "move1")
    assert result["status"] == "busy"
    await player.aclose()


@pytest.mark.asyncio
async def test_home_completion_clears_needs_home() -> None:
    proxy = MockDaemonProxy()
    player = ExpressionPlayer(proxy, poll_interval_s=0.01)
    player.needs_home = True
    proxy.goto_home.return_value = {"uuid": "home-uuid"}
    proxy.get_running_moves.side_effect = [
        [{"uuid": "home-uuid"}],
        [],
    ]

    await player.return_home()
    await player._watcher
    assert player.needs_home is False

    proxy.get_running_moves.side_effect = None
    proxy.get_running_moves.return_value = []
    result = await player.play("dataset1", "move1")
    assert result["status"] == "playing"
    await player.aclose()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/reachy_controller && uv run python -m pytest tests/test_expressions.py -v -k "needs_home"`
Expected: FAIL — `play` returns `"playing"` despite `needs_home` (first test), flag semantics missing

- [ ] **Step 3: Implement** — three edits in `ExpressionPlayer`:

In `__init__` (after `self._watcher = ...`):

```python
        # #67 safety: set by the motor-mode route on any torque change; cleared
        # only when a goto-home verifiably completes. While True, play() refuses
        # (jolt hazard: torque on, pose unknown).
        self.needs_home = False
```

In `play()`, directly after the `if self._session is not None:` busy return:

```python
            if self.needs_home:
                return {
                    "status": "busy",
                    "detail": "Robot pose is not homed — set motor mode to enabled to return home.",
                }
```

In `_goto_home_and_clear()`, replace the final `self._session = None` (the success path only) with:

```python
        self.needs_home = False  # robot is verifiably at home
        self._session = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/reachy_controller && uv run python -m pytest tests/test_expressions.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/reachy_controller/src/reachy_controller/routes/expressions.py apps/reachy_controller/tests/test_expressions.py
git commit -m "feat(reachy_controller): needs_home flag refuses expressions until the pose is re-homed (#67)"
```

---

### Task 8: #67 — motor-mode route kicks the return-home

**Files:**
- Modify: `apps/reachy_controller/src/reachy_controller/routes/controls.py` (route `set_motor_mode`, lines 70-75)
- Test: `apps/reachy_controller/tests/test_arbitration.py`

**Interfaces:**
- Consumes: `ExpressionPlayer.return_home()`, `.needs_home` (Tasks 6-7)

- [ ] **Step 1: Write the failing tests** — append to `test_arbitration.py`:

```python
@pytest.mark.asyncio
async def test_motor_enable_route_kicks_return_home() -> None:
    goto_posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/motors/set_mode/enabled":
            return httpx.Response(200, json={"mode": "enabled"})
        if path == "/api/move/goto":
            goto_posts.append(path)
            return httpx.Response(200, json={"uuid": "home-uuid"})
        if path == "/api/move/running":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    app = _make_app(handler)
    async with app.router.lifespan_context(app):
        app.state.expression_player.POLL_INTERVAL_S = 0.01
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/motors/set_mode/enabled")
            assert response.status_code == 200
            assert response.json() == {"mode": "enabled"}  # proxy body unmodified

            # Session is live synchronously with the route return.
            current = await client.get("/api/expressions/current")
            assert current.json()["active"] is True
            assert current.json()["phase"] == "returning_home"

            await app.state.expression_player._watcher
            assert goto_posts  # daemon saw POST /api/move/goto


@pytest.mark.asyncio
async def test_motor_disable_route_sets_needs_home() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/motors/set_mode/disabled":
            return httpx.Response(200, json={"mode": "disabled"})
        if request.url.path == "/api/move/running":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    app = _make_app(handler)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/api/motors/set_mode/disabled")
            response = await client.post("/api/expressions/play/dataset1/move1")
            assert response.status_code == 409
            assert "enabled" in response.json()["detail"]


@pytest.mark.asyncio
async def test_play_blocked_during_post_enable_return_home() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/motors/set_mode/enabled":
            return httpx.Response(200, json={"mode": "enabled"})
        if path == "/api/move/goto":
            return httpx.Response(200, json={"uuid": "home-uuid"})
        if path == "/api/move/running":
            return httpx.Response(200, json=[{"uuid": "home-uuid"}])  # goto held running
        return httpx.Response(404)

    app = _make_app(handler)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/api/motors/set_mode/enabled")
            response = await client.post("/api/expressions/play/dataset1/move1")
            assert response.status_code == 409
    # lifespan aclose() cancels the still-polling watcher — no leak
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/reachy_controller && uv run python -m pytest tests/test_arbitration.py -v -k "motor or post_enable"`
Expected: FAIL — no session after enable, play returns 200

- [ ] **Step 3: Implement** — replace `set_motor_mode` in `routes/controls.py`:

```python
@router.post("/motors/set_mode/{mode}")
async def set_motor_mode(mode: str, request: Request) -> dict[str, Any]:
    if mode not in VALID_MOTOR_MODES:
        raise HTTPException(status_code=400, detail="Invalid motor mode")
    proxy: DaemonProxy = request.app.state.daemon_proxy
    result = _result_or_unavailable(await proxy.set_motor_mode(mode))
    # #67: any torque change invalidates the trusted pose (a limp head droops).
    # Enabling kicks the graceful return-home session; expressions refuse until
    # a goto-home verifiably completes. If a session is already active,
    # return_home() no-ops and that session's own goto-home clears the flag.
    player = request.app.state.expression_player
    player.needs_home = True
    if mode == "enabled":
        await player.return_home()
    return result
```

- [ ] **Step 4: Run the full backend suite**

Run: `cd apps/reachy_controller && uv run python -m pytest`
Expected: PASS (all files — pins that `test_controls.py`'s existing motor-route tests still hold; if `test_control_routes_delegate_and_forward_request_bodies` drives `set_mode` through a mocked app lacking `expression_player`, adapt that test's app construction, not the route)

- [ ] **Step 5: Commit**

```bash
git add apps/reachy_controller/src/reachy_controller/routes/controls.py apps/reachy_controller/tests/test_arbitration.py
git commit -m "feat(reachy_controller): motor enable triggers graceful return-home before expressions unblock (#67)"
```

---

### Task 9: Frontend #66 — mutual gating in the UI

**Files:**
- Modify: `apps/reachy_controller/src/reachy_controller/static/main.js`

**Interfaces:**
- Consumes: backend guards (Tasks 3-4) — the UI mirrors them; the backend stays authoritative.
- Produces: `state.appActive` (boolean, set by `renderApps`), consumed by Task 10's reconcile paths.

All edits reuse existing machinery (disabled/title/`opacity:.3`, the 1 Hz apps poll signature guard, the ~10 Hz `setControlsEnabled` reconcile). No new components.

- [ ] **Step 1: Add `appActive` to the state init** (the `state` object near the top of the IIFE, ~line 19-24) — add the key:

```js
    appActive: false,
```

- [ ] **Step 2: Chips gate on app activity** — in `setControlsEnabled` (~line 173), replace the chip branch:

```js
    // Expression chips gate on the daemon too — but never fight an in-progress
    // move (exprBusy disables the whole strip until the POST resolves). They
    // also pause while an app owns the robot (#66).
    if (!state.exprBusy) {
      const chipDisabled = disabled || state.appActive;
      document.querySelectorAll(".chip").forEach((chip) => {
        if (chip.disabled !== chipDisabled) chip.disabled = chipDisabled;
      });
    }
```

- [ ] **Step 3: Track the flip + hint in `renderApps`** — after `const active = statuses.find(...)` (~line 348):

```js
    // Expressions pause while an app owns the robot (#66). setControlsEnabled
    // reconciles chip disabled-ness at ~10 Hz off this flag; the hint is only
    // touched on the flip.
    const appActive = Boolean(active);
    if (appActive !== state.appActive) {
      state.appActive = appActive;
      if (!state.exprBusy) {
        $("expr-hint").hidden = !appActive;
        if (appActive) {
          $("expr-hint").textContent = "Expressions pause while an app is running.";
          $("expr-hint").className = "panel-hint";
        }
      }
    }
```

- [ ] **Step 4: Start button gates on `exprBusy`** — replace the Start-button line (~line 373):

```js
        button = `<button class="btn start" data-app="${name}" data-action="start" ${(active || state.appBusy || offline || state.exprBusy) ? "disabled" : ""}${offline ? ' title="Apps start when the daemon is online."' : state.exprBusy ? ' title="Apps start when the robot finishes its expression."' : ""}>Start</button>`;
```

(`exprBusy` is now part of the rendered markup, so the 1 Hz poll's signature guard re-renders on the flip for free; ≤1 s latency is fine — the backend guard is authoritative.)

- [ ] **Step 5: Include `appActive` in the other chip-disabled computations** (avoids a one-tick enabled flash):
  - `loadExpressions` initial render (~line 474): `const disabled = state.daemonOnline && !state.appActive ? "" : "disabled";`
  - `playExpression` error path (~line 545): `const disabled = !state.daemonOnline || state.appActive;`
  - `stopExpressionPoller` reconcile (~line 598): `const disabled = !state.daemonOnline || state.appActive;`

- [ ] **Step 6: Sanity-check by serving the app**

Run: `cd apps/reachy_controller && uv run python -m reachy_controller.main` and load `http://127.0.0.1:7861` — no console errors; daemon-offline panels are the expected state on the Mac. (Full manual QA in Verification.)

- [ ] **Step 7: Commit**

```bash
git add apps/reachy_controller/src/reachy_controller/static/main.js
git commit -m "feat(reachy_controller): UI mutual gating — chips pause while an app runs, Start waits for expressions (#66)"
```

---

### Task 10: Frontend #67 — "Returning home…" after motor enable

**Files:**
- Modify: `apps/reachy_controller/src/reachy_controller/static/main.js` (motor-seg click handler, ~lines 1099-1109)

**Interfaces:**
- Consumes: backend return-home session (Task 8), the existing 750 ms expression poller (`startExpressionPoller`/`stopExpressionPoller`), `state.exprBusy`.

- [ ] **Step 1: Extend the motor handler** — replace the handler body:

```js
  $("motor-seg").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-mode]");
    if (!button) return;
    const mode = button.dataset.mode;
    if (mode === "disabled" && !window.confirm("Disable motors? The robot will go limp. Use Wake to recover.")) return;
    void performControl("motor", async () => {
      const result = await apiJson(`/api/motors/set_mode/${encodeURIComponent(mode)}`, { method: "POST" });
      selectMotorMode(mode);
      if (mode === "enabled") {
        // The backend kicked a return-home session (#67). Mirror an in-flight
        // expression *inside* the operation so performControl's finally
        // (setControlsEnabled) skips the chip branch — no enabled-flash.
        state.exprBusy = true;
        document.querySelectorAll(".chip").forEach((c) => { c.disabled = true; });
        $("expr-cancel").hidden = true;
        $("expr-hint").hidden = false;
        $("expr-hint").textContent = "Returning home…";
        $("expr-hint").className = "panel-hint";
        startExpressionPoller();
      }
      return result;
    });
  });
```

Everything downstream is existing machinery: the poller sees `phase: "returning_home"` (cancel stays hidden), and on `active === false` calls `stopExpressionPoller(true)` which reconciles chips/`exprBusy`/hint. If an expression was already active when enable was tapped, `exprBusy` is already true and `startExpressionPoller()` no-ops on its guard — harmless.

- [ ] **Step 2: Sanity-check by serving the app** — reload `http://127.0.0.1:7861`, no console errors.

- [ ] **Step 3: Commit**

```bash
git add apps/reachy_controller/src/reachy_controller/static/main.js
git commit -m "feat(reachy_controller): show returning-home state after motor enable via the expression poller (#67)"
```

---

### Task 11: Docs — AGENTS.md controller blurb

**Files:**
- Modify: `AGENTS.md` (the `reachy_controller` bullet under "Apps (`apps/`)"; `CLAUDE.md` is a symlink to it — edit `AGENTS.md` only)

- [ ] **Step 1: Extend the expressions/lifecycle sentence.** In the `reachy_controller` bullet, after the sentence ending "…violent motion, #64).", insert:

```
Apps and expressions also **block each other** (#66: expression play 409s while an app is
running/loading; app Start returns 200-refused while any expression session exists — both
route-layer in-process checks, mirrored in the UI). Setting motor mode to **enabled** kicks a
silent graceful return-home session and expressions refuse (`needs_home`, actionable 409) until
a goto-home verifiably completes — re-enabling torque on a drooped head otherwise makes the next
expression jolt violently (#67).
```

- [ ] **Step 2: Update the Jetson gotchas** — in the "Hard-won gotchas (Jetson / Ubuntu 24.04)" section, extend the daemon-wrapper bullet (the one starting "**Launch the daemon via the Jetson wrapper**") with one sentence:

```
The wrapper also self-heals `~/.asoundrc` (#65: PipeWire sometimes exposes only a Sink for the
Reachy Mini Audio card — no Source — so SDK capture silently falls back to the Sink's own
loopback; the ALSA-direct `.asoundrc` bypass fixes mic capture for every app on the box).
```

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs(AGENTS): arbitration + motor-enable return-home + asoundrc self-heal (#65 #66 #67)"
```

---

## Verification

**Backend (automated, on the Mac):**
```bash
cd apps/reachy_controller && uv run python -m pytest        # full suite green
```

**Frontend (manual/Playwright on the Mac, daemon-offline is expected; full pass needs the Jetson tunnel per AGENTS.md):**
1. Start an app → chips dim + "Expressions pause while an app is running."; stop it → chips re-enable ≤1 s.
2. Play an expression → every app Start disabled with the tooltip; on finish → re-enabled ≤1 s.
3. Motors → disabled (confirm dialog) → enabled: chips disable, "Returning home…" hint, cancel button never appears, UI reconciles when the session clears.
4. During return-home: `curl -X POST localhost:7861/api/apps/start/reachy_chat` → `{"status":"refused",...}`.
5. Reload the page mid return-home → sync-on-load enters busy state, no chip highlighted (null move), reconciles on completion.
6. With motors disabled, tap a chip → hint shows the actionable 409 detail ("…set motor mode to enabled…").
7. Kill the daemon mid return-home → poller reconciles within ~2 polls, UI not wedged; expressions stay refused (`needs_home`) until motors are re-enabled.

**Hardware (#65 + #67 feel, on the Jetson — owner-coordinated, one app at a time, agent must not drive while the owner is hands-on):**
- `git pull && uv sync --all-packages`, then `reachy-down && reachy-up`.
- #65: `cat ~/.asoundrc` has `pcm.reachymini_audio_sink`/`_src`; daemon log shows `Using .asoundrc audio source: reachymini_audio_src` (not "No specific audio card found"); `uv run python mic_test.py` shows real RMS (~0.2 speaking); start `reachy_chat` and talk to it — recognized turns in `.reachy-logs/reachy_chat.log`; `curl "localhost:8000/api/state/full?with_doa=true"` still 200; speaker still audible.
- #67: disable motors, let the head droop, enable → robot glides home over ~1.5 s (no jolt); immediately tapping an expression during the glide → busy hint; after home, expressions play normally. **Do not test motor-disable→expression on hardware before this fix is deployed** (that's the hazard itself).

**PR:** single PR from `feat/robot-arbitration-motor-safety` → `feat/reachy-chat-vision`, body `Closes #65, Closes #66, Closes #67`, flagging: (a) `any_app_active` excludes `"stopping"`; (b) play-while-motors-disabled now 409s (behavior change, arguably a second bug fixed); (c) the accepted non-atomic cross-check race; (d) `~/.asoundrc` sets ALSA's global `pcm.!default` to the Reachy card (fine on a robot-dedicated Jetson); (e) tapping *enabled* while already enabled re-runs a harmless goto-home (doubles as a manual "home" action); (f) a post-enable goto could momentarily overlap an app's set_target loop if an app is running when enable is tapped (same exposure as today's expression-end goto).

## Execution

Worktree via superpowers:using-git-worktrees (branch `feat/robot-arbitration-motor-safety` off local `feat/reachy-chat-vision`), then superpowers:subagent-driven-development (per-task implementer + reviewer; reviewers ≥ sonnet; final whole-branch review on the most capable model), finishing via superpowers:finishing-a-development-branch. Tasks 1-8 are mechanical-with-tests (codex-loop or cheap Claude workers fine); Tasks 9-10 touch the craft-sensitive frontend — keep diffs surgical.
