# Expressions: backend serialization, cancel, return-home (#64, PR #63)

## Context

The daemon does NOT serialize `play_move` requests: its guard uses `threading.RLock.acquire(blocking=False)`,
which is per-thread reentrant, and all daemon coroutines share one event-loop thread — so two rapid plays run
two interleaved 100 Hz `set_target` loops (violent motion). Verified in sim: both uuids appear in
`GET /api/move/running`, zero "Ignoring play_move" warnings. The daemon is the upstream SDK (gitignored),
so the controller backend must enforce one-at-a-time.

Return-home must match app-stop exactly: apps call `reachy_mini.wake_up()`; the daemon equivalent is
`POST /api/move/play/wake_up` (home + head gesture + wake_up.wav cue).

## Verified daemon primitives (sim, SDK 1.8.3)

- `POST /api/move/play/recorded-move-dataset/{dataset}/{move}` → 200 `{"uuid": ...}` (returns immediately; move runs async)
- `GET /api/move/running` → `[{"uuid": ...}, ...]` (accurate; empties when moves finish)
- `POST /api/move/stop` body `{"uuid": ...}` → 200 mid-flight; **500 (bare KeyError) if the move already finished** — treat as success
- `POST /api/move/play/wake_up` → 200 `{"uuid": ...}` (the return-home move, also async)

## API contract (frozen — both tasks code against this)

- `POST /api/expressions/play/{dataset:path}/{move_name}`
  - 200 `{"status": "playing", "uuid": "...", "dataset": "...", "move": "..."}`
  - 409 `{"detail": "Robot is busy with another move."}` — an expression lifecycle is active (playing OR returning home), or the daemon reports any foreign running move
  - 503 `{"detail": "Reachy daemon unavailable"}` — daemon unreachable
  - 404 passthrough if the daemon 404s the move/dataset
- `POST /api/expressions/stop`
  - 200 `{"status": "stopping"}` when a move was playing (daemon stop fired; return-home follows automatically)
  - 200 `{"status": "returning_home"}` when already in return-home (no-op)
  - 200 `{"status": "idle"}` when nothing active (idempotent)
- `GET /api/expressions/current`
  - `{"active": false}` or `{"active": true, "phase": "playing" | "returning_home", "dataset": "...", "move": "...", "uuid": "..."}`

## Task 1 — backend lifecycle (routes/expressions.py + daemon_proxy + tests)

Files: `apps/reachy_controller/src/reachy_controller/routes/expressions.py` (new),
`apps/reachy_controller/src/reachy_controller/daemon_proxy.py` (add helpers),
`apps/reachy_controller/src/reachy_controller/main.py` (wire router + state + lifespan cleanup),
`apps/reachy_controller/tests/test_expressions.py` (new).

DaemonProxy helpers (follow existing `_post_json`/`get_json` style):
`play_recorded_move(dataset, move)`, `get_running_moves()`, `stop_move(uuid)`, `play_wake_up()`.
`stop_move` must swallow the daemon's 500-on-already-finished (return None / best-effort).

`ExpressionPlayer` (module-level class, instance on `app.state.expression_player`):
- `_session`: None or `{uuid, dataset, move, phase}`; `_lock: asyncio.Lock` held only during check-and-start (never during playback); `_watcher: asyncio.Task | None`.
- `play(dataset, move)`: under lock — if `_session` → busy; else `get_running_moves()`: on None → daemon unavailable; on non-empty → busy (foreign move). POST play; on daemon 404 propagate. Store session phase="playing", spawn watcher, return session.
- watcher (single owner of all transitions): poll `get_running_moves()` every 0.3 s until our uuid is absent → phase="returning_home" → `play_wake_up()` → poll until THAT uuid absent → clear session. Any daemon error (None response) at any point → clear session and stop (no return-home possible). Never raises.
- `stop()`: if no session → "idle"; if phase returning_home → "returning_home"; else fire `stop_move(uuid)` best-effort and return "stopping" — the watcher sees the uuid vanish and proceeds to return-home. Do NOT mutate session in stop(); the watcher owns transitions.
- `aclose()`: cancel watcher (called from the app lifespan shutdown, alongside existing cleanup).

Route must be registered BEFORE the transparent `/api/{path:path}` daemon proxy router in create_app
(specific routers already are — match their placement).

Tests (mirror existing `httpx.MockTransport` + `create_app` patterns in tests/test_controls.py, test_daemon_routes.py;
run with `uv run python -m pytest` from apps/reachy_controller):
1. play → daemon receives POST play, 200 with uuid, `current` reports playing.
2. play while session active → 409, daemon play called exactly once.
3. play while daemon reports a foreign running move → 409, no play POST.
4. natural finish → fake daemon running-list sequence [ [uuid], [] ] → watcher POSTs wake_up → after wake_up's uuid drains, `current` → inactive. (Drive time with short poll interval — make the interval a class attr so tests set it to ~0.01.)
5. stop while playing → daemon /move/stop POSTed with the uuid; return-home still happens; final state inactive.
6. stop tolerates daemon 500 on /move/stop (move just finished): still returns "stopping", watcher completes.
7. stop when idle → {"status": "idle"}, zero daemon calls.
8. play with daemon unreachable (transport raises httpx.ConnectError) → 503.
9. daemon dies mid-watch (running-list request errors) → session cleared, no exception, no wake_up retry loop.
10. two concurrent plays via asyncio.gather → exactly one daemon play POST, other gets 409.
11. play propagates daemon 404 (unknown move) → 404, session NOT left set.

Constraints: no new dependencies; follow the file's existing comment style (explain constraints, not narration);
`uv run python -m pytest` green including all pre-existing tests.

## Task 2 — frontend lifecycle wiring (static/main.js, index.html, style.css)

- index.html: remove the `one at a time` pill from the expressions card head (keep `.head` + `.rule`).
  Add a cancel button in its place: `<button class="btn stop expr-cancel" id="expr-cancel" hidden>cancel</button>`
  (reuse existing `.btn.stop` styling; add a `.expr-cancel` size tweak in style.css only if needed — small,
  fits the card head row; no new colors, no emoji).
- main.js `playExpression`: POST `/api/expressions/play/${dataset}/${encodeURIComponent(move)}`.
  On 200: enter busy state (all chips disabled, tapped chip `.playing`, cancel button visible) and start
  polling `GET /api/expressions/current` every 750 ms until `active === false` (then reconcile chips to
  daemon-online state, hide cancel, drop `.playing`). During phase `returning_home`, hide the cancel button
  (cancelling return-home is not a thing) and keep chips disabled — reuse the existing `.playing` chip cue,
  no new spinner.
  On 409: hint "Robot is busy with another move." (`panel-hint error`). On other errors: existing behavior.
- Cancel button → POST `/api/expressions/stop`; button disables immediately; polling continues until inactive.
- Sync-on-load / multi-tab: when the daemon comes online (the existing `loadExpressions` gate at the online
  reconcile), also fetch `/api/expressions/current` once; if active, enter the same busy/polling state
  (without a `.playing` chip if the move name isn't in the strips — just disable + show cancel when phase playing).
- Keep `state.exprBusy` as the single client-side busy flag; it now tracks the whole lifecycle (playing +
  returning home), not just the POST round-trip.
- No framework, no build step; `node --check` must pass. Match existing code style and comment discipline.

## Task 3 — E2E + visual QA (orchestrator, in-session; not a subagent)

Against the live sim daemon + controller on the Mac:
- Rapid double-click two chips → exactly ONE uuid in `GET /api/move/running`; second click shows busy hint.
- Cancel mid-move → move's uuid drains, wake_up uuid appears (return-home), then all idle; chips re-enable.
- Natural finish → wake_up fires without user action.
- Reload mid-move → UI resumes busy state (sync-on-load).
- Reduced-motion / theme unaffected; screenshot the card idle + playing + hint states.
- `uv run python -m pytest` (full controller suite) green; `node --check` green.
