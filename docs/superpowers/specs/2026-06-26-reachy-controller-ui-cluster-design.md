# Reachy Controller UI cluster — slider drag, power-off ramp, battery drain

**Date:** 2026-06-26
**Issues:** #38 (bug), #32, #36
**Branch:** `fix/reachy-controller-ui-38-32-36`
**Files:** all in `apps/reachy_controller/src/reachy_controller/static/` — `main.js`, `style.css`, `index.html`

## Context

Owner hardware QA of the Liquid Glass web console surfaced three frontend issues. They are
independent symptoms but cluster naturally: all three live in the controller's vanilla-JS/CSS
static assets (no build step), and two of them (#38, #36) trace back to the same periodic-update
machinery. Fixing them together keeps the review surface in one place. The controller talks to the
daemon over HTTP/WS only and never instantiates `ReachyMini()` — that boundary is preserved.

## Fix 1 — #38: volume slider drag stops after ~1 s (bug)

**Symptom:** click-drag on `#vol-spk` / `#vol-mic` stops after ~1 s; the value snaps to where the
cursor is.

**Real root cause (differs from the issue's `syncSlider` guess):** the slider `input` handler
(`main.js:515`) calls a 240 ms-debounced `save()` (`bindSliders`, `main.js:504-511`). `save()`
routes through `performControl()` (`main.js:477`), whose line 484 calls `setControlsEnabled(false)`
(`main.js:122`) — which sets `disabled=true` on the slider **currently being dragged**. The browser
cancels an in-progress range drag when the element is disabled; line 499 re-enables it ~a second
later (after the POST). `syncSlider()`'s `document.activeElement === input` guard (`main.js:432`) is
correct and is not the culprit — it's only called from `loadControls()`, not during streaming.

**Approach:** give volume saves a lightweight path that does **not** disable the slider.
- Add a `saveVolume(label, endpoint, getValue)` helper that POSTs via `apiJson` directly, with an
  in-flight coalesce guard (if a request is in flight, store the latest value and re-send once on
  completion — avoids hammering the daemon during a drag) and updates `#control-status`
  (`label` → `ok`/`error`) **without** calling `setControlsEnabled(false)`.
- Rewire `bindSliders` to debounce `saveVolume` instead of `performControl`.
- Leave `performControl` for the discrete operations it's meant for (wobble, test sound, motor
  mode). Daemon-offline disabling stays owned by `setDaemonOnline → setControlsEnabled`.

**Acceptance:** dragging either slider tracks the cursor smoothly until release; the value commits
without the slider going disabled mid-drag; nudging volume no longer disables wobble/test-sound/
motor buttons.

## Fix 2 — #32: power button needs an OFF transition ramp

**Symptom:** powering the daemon **on** pulses `.starting` (good); powering **off** jumps green→red
instantly with no transitional state.

**Why:** the start branch optimistically adds `.starting` on click (`main.js:770`); the stop branch
(`main.js:754-766`) adds no transitional class. Because `/api/daemon/stop` blocks while the daemon
is still `running:true`, a poll-driven backend `"stopping"` status is not reliably observable — so
drive the transition from the frontend, mirroring the start path.

**Approach:**
- On stop-click: set `state.daemonStopping = true`, add `.stopping` (remove `running`/`starting`),
  disable the button.
- `pollDaemonStatus` (`main.js:710`): add `stopping` to the remove-list (line 718); while
  `state.daemonStopping` is true, hold `.stopping` and suppress `.running` until the poll sees
  not-running, then clear the flag and fall through to the red (no-class) default.
- On stop error / `finally`: clear the flag and re-poll.
- CSS: `@keyframes ramp-off` (`--mint` → `--coral`) on `.vp-power.stopping button`, landing on coral
  and holding (`forwards`); the class is removed once stopped so the static coral default takes over
  with no flash. Add a `prefers-reduced-motion` static-frame guard for **both** `.starting` and
  `.stopping` (the existing `pulse` currently has none — `style.css:447-450`, keyframes `281-283`).

**Acceptance:** clicking stop shows a legible green→red ramp until the daemon is confirmed down;
reduced-motion holds a static frame; symmetric with the on-pulse.

## Fix 3 — #36: phone battery drain when console is open

**Symptom:** phone battery drains while the console tab is open, including when backgrounded.

**Why:** nothing pauses on `visibilitychange` (confirmed absent). Always-on burns: audio stream
20 Hz (`audio.py:17`, 50 ms), telemetry ~10 Hz (WS-driven), stats 0.67 Hz, plus two 1 s polls
(`pollDaemonStatus` `main.js:788`, `pollAppsStatus` `main.js:821`). EventSources are never closed
(`bindEventSource` `main.js:77-93` returns the source but callers discard it).

**Approach (JS — satisfies the acceptance):**
- Hold references: capture the 3 EventSources and 2 interval IDs in `state`.
- `stopLiveConnections()` — `.close()` the 3 EventSources, `clearInterval` both polls, cancel the
  sim rAF if running.
- `startLiveConnections()` — (re)open the streams + intervals (fire one poll immediately), restart
  sim rAF if `mode==="sim"`. Refactor the existing inline setup (`main.js:786-821`) into this.
- Wire `document.addEventListener("visibilitychange", …)`: hidden → stop; visible → start.

**Approach (CSS — small, in-scope mobile win):** extend the existing `@media (max-width:560px)`
block (`style.css:~1046-1051`, already drops SVG refraction on phones) to also pause `.blob` drift
and the `.glass::before` sheen on phones.

**Acceptance:** when the tab is backgrounded, the 3 streams close and both polls stop (verifiable via
`EventSource.readyState`/network); they resume cleanly on foreground; console fully functional when
visible. Real phone-battery improvement confirmed by the owner.

## Out of scope (YAGNI)

- No change to foreground stream cadences (the 20 Hz audio stream stays; #14 replaces the equalizer
  with a real visualizer later). We only **pause** streams when hidden, not retune them.
- No EventSource backoff/reconnect rework beyond open/close on visibility.
- No new daemon/backend endpoints; controller stays HTTP/WS-only, never `ReachyMini()`.
- #32 ON-ramp (coral→mint) is optional polish; the primary deliverable is the OFF ramp + the
  reduced-motion guard.

## Verification

Execution via `subagent-driven-development` (3 tasks, one per fix, sequential — shared files are
fine sequentially). Per-task TDD where it fits + per-task review; final whole-branch review.

**(A) Mac-side agent verification** — run the controller (`uv run python -m reachy_controller.main`)
+ a local sim daemon (`uv run python -m reachy_mini.daemon.app.main --sim --headless`); drive with
Playwright:
- #38: dispatch `input` events on a slider, assert `disabled` stays false through the debounce
  window and the value commits; assert volume save no longer toggles other controls.
- #32: toggle the `.stopping` class / drive the click path against the sim daemon; screenshot the
  green→red ramp; verify reduced-motion holds a static frame.
- #36: dispatch `visibilitychange`; assert the 3 EventSources reach `readyState===2` (closed) and
  both intervals are cleared on hide, and re-open on show.

**(B) Live-Jetson functional pass** — drive the live Jetson controller through the controller-port
tunnel (`ssh -N -L 7870:localhost:7861 <jetson>` → `:7870`); on-device the daemon power button is
manageable (`REACHY_MANAGE_DAEMON=1`). Verify the real click→`stopping`→stopped ramp and that
streams pause/resume. Coordinate timing with the owner — toggling daemon power stops/starts the
whole stack. Owner does the final phone-battery + motion-feel pass.

## PR

One branch, likely one PR (`Closes #38, #32, #36`); split at `finishing-a-development-branch` if
review prefers the bug (#38) on its own.
