# Issue #46 — Shared Controller Streams Design

## Goal

Reduce duplicate continuous daemon work from visible controller browsers while preserving the existing SSE event names, payloads, reconnect behavior, daemon gating, and UI cadence. This design is linked to [Issue #46](https://github.com/zeapsu/reachy-console/issues/46).

## Architecture

`reachy_controller.shared_streams` owns two app-scoped, lazy broadcasters created by `create_app()` and stored on `app.state`:

- `StateBroadcaster` owns one `DaemonProxy.ws_stream_full_state()` task. The first subscription starts it; the last unsubscribe cancels it. Every subscriber receives a bounded queue with drop-oldest overflow. The most recent state (including the daemon-unavailable sentinel) is queued immediately for a new subscriber. A completed/failed upstream stream waits before reconnecting, preventing a busy loop while retaining offline-to-online recovery.
- `StatsBroadcaster` owns one loop that probes daemon reachability and, only when online, calls `asyncio.to_thread(collect_jetson_stats)` once every 1.5 seconds. The sample is fanned out to all stats subscribers through bounded drop-oldest queues. Offline samples are internal `None` values, so routes emit keepalives but never stats updates or host reads while offline. It is started only by the first stats subscriber and cancelled after the last.

Subscriptions are explicit async iterators with cleanup (`aclose()` and async context-manager support). Broadcasters are also closed during ASGI lifespan teardown before the daemon proxy closes.

## Route contracts

- `/telemetry/state-stream` adapts state snapshots to the unchanged `state-update` and `daemon-offline` SSE events, plus 15-second keepalive comments.
- `/api/audio/levels-stream` derives the unchanged `audio-level` payload and `daemon-unavailable` event from the shared state stream; it no longer calls `get_full_state()` per client.
- `/api/system/stats-stream` adapts shared online samples to the unchanged `stats-update` event, emits 15-second keepalives, and omits offline samples.

The one-shot REST endpoints remain unchanged. The frontend retains the authoritative 1 Hz daemon-status fallback and adds an `appsStatusInflight` guard/finally release around `pollAppsStatus()` so slow requests cannot overlap.

## Lifecycle and evidence

No broadcaster does upstream work with zero subscribers. Targeted route/broadcaster tests cover sharing, latest replay, bounded overflow, cleanup, lazy first/last lifecycle, offline recovery, reconnect backoff, one sample per interval, and offline host-read suppression. A focused static test verifies the frontend guard and success/error release. ASGI/fake-proxy smoke evidence records O(1) upstream work and teardown behavior in the issue report.
