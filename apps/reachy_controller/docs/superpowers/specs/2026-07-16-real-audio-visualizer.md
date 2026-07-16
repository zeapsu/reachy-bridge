# Issue #14 — real microphone spectrum visualizer

## Contract

The Jetson wrapper installs an idempotent runtime patch on the SDK's existing
`GstMediaServer._configure_audio` before the daemon entry point constructs a
media server. The original capture source and `queue_audiosrc → webrtcsink`
branch remain the only microphone capture path. The patch inserts one tee,
keeps the WebRTC branch, and adds one bounded analysis branch:

`queue → audioconvert → audioresample → F32LE mono 16 kHz caps → appsink`.

The appsink callback maps the Gst buffer as a float32 view and hands it to
`AudioSpectrumTap` on the GStreamer thread. The tap rate-caps FFT work at 20 Hz,
reuses its window, frequency masks, and work arrays for a stable buffer length,
and stores only a lock-guarded latest snapshot. It never touches asyncio.

Each snapshot has normalized overall RMS and low/mid/high levels plus clamped
`rms_db`, `sample_rate_hz`, UTC `timestamp`, and `source = "daemon mic tap"`.
The normalized mapping is linear in dBFS from −60 dBFS to −6 dBFS and clamped to
0..1. The three bands use rFFT magnitudes normalized by sample count over
80–300 Hz, 300–2000 Hz, and 2000–7500 Hz.

`GET /api/audio/levels` is a read-only latest-state route. It performs no
capture, FFT, or blocking work and returns 503 when the daemon has no media,
media is released, the tap has no sample, or the latest sample is older than
one second. A media reacquire rebuild naturally creates a fresh tap.

The controller owns one lazy `AudioLevelsBroadcaster`. It performs one daemon
GET at 10 Hz while at least one SSE client is subscribed, uses bounded
drop-oldest queues and latest replay, backs off while offline, and cancels
when the final client disconnects. The audio SSE emits `audio-level` payloads
with the complete real signal fields, `daemon-unavailable` while offline, and a
15-second keepalive. Telemetry remains the sole consumer of `StateBroadcaster`.

## Failure behavior

Missing GStreamer, daemon media, or optional analysis elements leaves the
original WebRTC audio branch intact and exposes an honest 503/no-signal state.
The controller never opens an audio device or constructs `ReachyMini`.

## Verification

Synthetic 120 Hz, 1 kHz, and 5 kHz buffers prove band dominance; amplitude,
silence, non-finite input, rate cap, and cache reuse are covered by unit tests.
Fake-GStreamer tests prove one original source, one tee, preserved WebRTC
linkage, one analysis branch, idempotent installation, and graceful missing
appsink behavior. ASGI tests cover freshness, release, no-media, reacquire, SSE
payloads, bounded sharing, idle behavior, recovery, keepalive, and cancellation.
Physical Jetson capture remains a deployment checklist item rather than a Mac
hardware claim.
