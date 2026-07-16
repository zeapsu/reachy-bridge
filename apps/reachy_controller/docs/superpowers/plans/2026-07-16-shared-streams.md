# Issue #46 — Shared Streams Implementation Plan

Linked issue: [#46](https://github.com/zeapsu/reachy-console/issues/46)

1. **Red tests:** Add behavior tests for state fan-out/replay/drop-oldest/lazy lifecycle/cleanup/reconnect and stats fan-out/interval/offline gating/idle behavior. Update route tests to consume broadcasters while asserting the existing SSE strings.
2. **Broadcaster core:** Implement `StateBroadcaster` and `StatsBroadcaster` with app-scoped lazy tasks, bounded per-subscriber queues, latest replay, first/last lifecycle, cancellation cleanup, and reconnect delay.
3. **Route wiring:** Instantiate broadcasters in `main.create_app`, expose them via `app.state`, close them during lifespan teardown, and adapt telemetry/audio/stats generators without changing endpoint paths or SSE events.
4. **Frontend guard:** Add `state.appsStatusInflight`, return early while a status request is active, and clear it in `finally`; retain the 1 Hz timer and daemon-status fallback.
5. **Verification:** Run focused controller tests and the full controller suite with the prescribed `UV_PROJECT_ENVIRONMENT`; smoke the real ASGI app with a fake proxy and concurrent consumers, recording upstream counters and teardown in `/tmp/reachy-console-issue-46-report.md`.
