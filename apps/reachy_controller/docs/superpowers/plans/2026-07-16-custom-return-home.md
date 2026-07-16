# Custom return-home implementation plan — Issue #34

Issue: [#34](https://github.com/zeapsu/reachy-console/issues/34)

## Steps

1. Add focused headless tests for each app before implementation. Use fake
   robots to observe `goto_target`, `set_target`, and media calls, and assert
   the identity pose, SDK antenna offsets, body yaw, 2.4-second duration,
   explicit minimum-jerk method, motion/audio order, fallback behavior, and
   audio-error containment. Add one minimal Simon module.
2. Generate one deterministic short mathematical PCM chime per app and verify
   each resource through `importlib.resources` and Python WAV header decoding.
3. Replace the four borrowed `wake_up` shutdown paths with explicit, local
   return-home choreography. Keep motion fallback and existing teardown intact;
   play audio only after successful motion and catch audio failures separately.
4. Update only package-data declarations needed for packaged assets (chat's
   explicit declaration; the other apps already include all package data).
5. Run focused return-home tests, then the existing chat/lookat/dance suites and
   the Simon tests with the assigned shared environment. Run fake shutdown
   smoke calls and capture ordering without hardware.
6. Self-review for independent packaging, no borrowed `wake_up` shutdown path,
   no repository-relative audio path, and disconnect safety; commit with
   `#34` in commit messages.

## Verification contract

No hardware is actuated. Evidence consists of pytest output, WAV resource/header
checks, and fake call-order traces for normal motion, motion failure, and audio
failure paths.
