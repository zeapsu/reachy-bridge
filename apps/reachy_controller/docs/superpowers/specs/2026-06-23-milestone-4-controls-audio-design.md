# Milestone 4 Controls Proxy and Audio Visualizer Design

## Scope

Milestone 4 adds typed controller-owned routes for speaker volume, microphone volume,
motor mode, wobbling, and sound playback. It also adds an SSE audio-level feed backed
only by the daemon's real `doa.speech_detected` signal.

## Architecture

`DaemonProxy` remains the only component that communicates with the Reachy daemon. Its
new helpers call the documented daemon endpoints, decode successful JSON responses, and
normalize HTTP and JSON decoding failures to `None`.

`routes/controls.py` validates controller request bodies and motor modes, delegates to
the proxy helpers, and maps `None` to a 503 `{"detail": "Daemon unavailable"}` response.
The controls router is registered before the existing catch-all daemon router.

`routes/audio.py` polls `DaemonProxy.get_full_state()` at 20 Hz. It maps
`doa.speech_detected == true` to level `0.7` and all other valid states to `0.0`. Each
sample is emitted as an `audio-level` SSE event with a UTC timestamp. If the daemon is
unavailable, the stream emits a `daemon-unavailable` event and continues polling. It
also emits keepalive comments every 15 seconds and suppresses normal cancellation on
client disconnect.

## Validation and failure behavior

Speaker and microphone volume inputs must be integers from 0 through 100. Motor mode
must be `enabled`, `disabled`, or `gravity_compensation`. Invalid inputs return 400.
Daemon HTTP failures, non-success status codes, and invalid JSON return 503 from control
routes and never escape to clients.

## Verification

Tests cover proxy method/path/body behavior, validation, successful and offline route
responses, SSE active/idle/offline events, and router precedence. The full existing test
suite, Ruff, daemon-down curl checks, graceful SIGINT, and forbidden-source grep complete
verification.
