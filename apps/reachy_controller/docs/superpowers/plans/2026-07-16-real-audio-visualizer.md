# Issue #14 — backend implementation plan

## Phase 1: daemon signal source

1. Add failing synthetic-signal tests for normalized RMS, dB mapping, silence and
   non-finite input, low/mid/high dominance, rate limiting, and cache reuse.
2. Implement `AudioSpectrumTap` with a plain thread lock, latest snapshot,
   monotonic freshness, reusable Hann/window/magnitude buffers, and bounded
   frequency masks.
3. Add the Jetson-only runtime patch around the SDK's original
   `_configure_audio`; call the original first, find `queue_audiosrc`, then
   insert exactly one tee and one drop-enabled analysis branch. Make the class
   marker prevent duplicate wrapping and make missing optional elements return
   with WebRTC audio preserved.
4. Add the daemon route wrapper and test 503/no-media, release, no-sample,
   stale, and media-reacquire behavior.

## Phase 2: controller transport

1. Add `DaemonProxy.get_audio_levels()` as a narrow GET helper that maps daemon
   transport/HTTP/JSON errors to `None`.
2. Add one app-scoped `AudioLevelsBroadcaster` using the existing bounded lazy
   broadcaster lifecycle. It polls only with subscribers, publishes one
   latest snapshot to every client, drops oldest queue values, backs off when
   unavailable, and cancels on the final unsubscribe.
3. Rewire `/api/audio/levels-stream` to the broadcaster and preserve the
   `audio-level`, `daemon-unavailable`, and 15-second keepalive contract.
4. Close the audio broadcaster before daemon proxy shutdown and leave telemetry's
   `StateBroadcaster` unchanged.

## Evidence gates

- Run focused daemon/spectrum and controller audio/broadcaster tests.
- Run the complete controller test suite from `apps/reachy_controller`.
- Run source compilation and ASGI synthetic route/SSE smokes.
- Record exact counters and outcomes in `/tmp/reachy-console-issue-14-report.md`.
- Do not claim Jetson hardware verification from this development host; leave
  the speak/hum/whistle, release/reacquire, and transcription checklist for
  deployment.
