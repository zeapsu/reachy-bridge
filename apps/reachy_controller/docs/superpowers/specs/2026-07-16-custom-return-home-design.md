# Custom return-home design — Issue #34

Issue: [#34](https://github.com/zeapsu/reachy-console/issues/34)

## Contract

The four independently packageable apps own their shutdown return-home behavior:

- `reachy_chat/src/reachy_chat/main.py::_return_home`
- `reachy_lookat/reachy_lookat/main.py::ReachyLookat._return_to_neutral`
- `reachy_simon/reachy_simon/main.py::ReachySimon._return_home`
- `reachy_dance/reachy_dance/main.py::ReachyDance._return_home`

Each function sends one smooth `goto_target` to the identity head pose with SDK
home antenna offsets `[-0.1745, 0.1745]`, body yaw `0`, duration `2.4`, and
explicit `InterpolationTechnique.MIN_JERK`. No cross-app runtime abstraction is
introduced.

After a successful `goto_target` return, the app plays its own short original
PCM WAV (`assets/return_home.wav`) through `reachy_mini.media.play_sound`. Sound
resources are resolved with `importlib.resources`, so installed packages do not
depend on repository-relative paths. Each package includes its own asset.

If motion fails, the app logs at WARNING level and best-effort calls `set_target`
with the same identity/head/antenna/body target. Audio errors are logged
separately and are never allowed to prevent the caller's existing teardown and
disconnect sequence. Existing `finally` teardown remains unchanged.

## Observable acceptance

Focused tests cover target pose and parameters, motion-before-audio ordering,
packaged WAV lookup/header decoding, motion fallback, and audio failures that do
not escape. A fake shutdown smoke records calls without actuating hardware.
