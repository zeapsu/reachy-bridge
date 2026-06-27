# Reachy Controller UI cluster — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three owner-QA frontend issues in the Liquid Glass controller console — the volume-slider drag bug (#38), the missing power-OFF transition (#32), and battery drain when the tab is open/backgrounded (#36).

**Architecture:** All changes are in the controller's vanilla-JS / CSS static assets (no build step). No Python/backend changes; the controller stays HTTP/WS-only and never instantiates `ReachyMini()`. Three independent fixes, sequential under subagent-driven-development (shared files, so order matters but there's no parallel contention).

**Tech Stack:** Vanilla ES (IIFE module in `main.js`), CSS, served by FastAPI/uvicorn (`reachy_controller.main`). Verification via Playwright MCP against a locally-served controller + sim daemon.

## Global Constraints

- **No build step / no framework / no bundler.** Vanilla JS + CSS only. (CLAUDE.md)
- **Controller never instantiates `ReachyMini()`** and never grabs the robot/mic; HTTP/WS to the daemon only. (CLAUDE.md, #14)
- **Never put `filter: url(#…)` on `.vp-stage`.** Refraction lives on panel backdrops only. (CLAUDE.md) — not touched here, but do not regress it.
- Honor existing a11y media queries: `prefers-reduced-motion`, `prefers-reduced-transparency`.
- Single file for JS: `apps/reachy_controller/src/reachy_controller/static/main.js`. CSS: `.../static/style.css`. Markup: `.../static/index.html`.
- Run/serve note: the controller binds `127.0.0.1:7861`. Static assets have **no Cache-Control** → hard-refresh the browser after edits (memory `reachy-controller-serve-refresh-trap`).

---

## Task 1: #38 — volume slider drag no longer disables the slider

**Files:**
- Modify: `apps/reachy_controller/src/reachy_controller/static/main.js` — `bindSliders` (~503-520); add a `saveVolume` helper + its small state above it.

**Interfaces:**
- Consumes: existing `apiJson(url, opts)` (~46-67), `debounce(fn, delay)` (~469-475), `state.daemonOnline`, `$(id)`, `updateSliderFill(id)`.
- Produces: `saveVolume(id, label, endpoint)` (async, fire-and-forget via `void`) and a module-level `volumeSave` coalesce-state object. Replaces the per-slider `performControl(...)` save path. `performControl` is unchanged and still used by wobble/test-sound/motor.

**Root cause:** the debounced save routed through `performControl()` → `setControlsEnabled(false)` (main.js:484), which sets `disabled=true` on the slider being dragged; the browser cancels the in-progress range drag. Fix: a lightweight save path that does not disable controls.

- [ ] **Step 1: Add the `saveVolume` helper + coalesce state.** In `main.js`, immediately above `function bindSliders() {` (~503), insert:

```javascript
  // Lightweight volume save path (#38): POST directly without disabling the slider
  // being dragged. setControlsEnabled() is reserved for daemon-offline + discrete ops.
  const volumeSave = {
    "vol-spk": { inflight: false, dirty: false },
    "vol-mic": { inflight: false, dirty: false },
  };

  async function saveVolume(id, label, endpoint) {
    if (!state.daemonOnline) return;
    const slot = volumeSave[id];
    if (slot.inflight) {
      // A request is already in flight; remember that the value moved again and
      // re-send the freshest value once it returns (coalesce, don't queue per-event).
      slot.dirty = true;
      return;
    }
    slot.inflight = true;
    $("control-status").textContent = label;
    try {
      const value = Number($(id).value);
      await apiJson(endpoint, { method: "POST", body: JSON.stringify({ volume: value }) });
      $("control-status").textContent = "ok";
      $("control-hint").textContent = "";
      $("control-hint").className = "panel-hint";
    } catch (error) {
      $("control-status").textContent = "error";
      $("control-hint").textContent = error.message || "Volume request failed";
      $("control-hint").className = "panel-hint error";
    } finally {
      slot.inflight = false;
      if (slot.dirty) {
        slot.dirty = false;
        void saveVolume(id, label, endpoint);
      }
    }
  }
```

- [ ] **Step 2: Rewire `bindSliders` to use `saveVolume`.** Replace the body of `bindSliders` (~504-519) with:

```javascript
  function bindSliders() {
    const speakerSave = debounce(() => void saveVolume("vol-spk", "speaker", "/api/volume/set"), 240);
    const micSave = debounce(() => void saveVolume("vol-mic", "microphone", "/api/volume/microphone/set"), 240);

    [["vol-spk", speakerSave], ["vol-mic", micSave]].forEach(([id, save]) => {
      updateSliderFill(id);
      $(id).addEventListener("input", () => {
        updateSliderFill(id);
        save();
      });
    });
  }
```

- [ ] **Step 3: Self-check — confirm the disable path is gone.** Grep that no slider-save path calls `performControl`/`setControlsEnabled`:

```bash
grep -n "performControl\|setControlsEnabled" apps/reachy_controller/src/reachy_controller/static/main.js
```
Expected: `performControl` appears only for wobble (`#wobble`), test sound (`#testsound`), and motor-mode handlers — **not** in `bindSliders`/`saveVolume`. `setControlsEnabled` appears only in `setDaemonOnline`, `loadControls`, and `performControl`.

- [ ] **Step 4: Playwright verification (see "Verification A").** With controller + sim daemon up: dispatch a burst of `input` events on `#vol-spk` and sample `#vol-spk.disabled` + `#testsound.disabled` for ~1.2 s — both must stay `false` throughout. Confirm `#control-status` shows the value committing (`speaker` → `ok`).

- [ ] **Step 5: Commit.**

```bash
git add apps/reachy_controller/src/reachy_controller/static/main.js
git commit -m "fix(reachy_controller): volume save no longer disables the dragged slider (#38)"
```

---

## Task 2: #32 — power button OFF transition ramp (+ reduced-motion guard)

**Files:**
- Modify: `main.js` — `state` literal (add `daemonStopping`), `pollDaemonStatus` (~710-745), the `#daemon-power` stop branch (~754-766).
- Modify: `style.css` — add `.vp-power.stopping` rule + `ramp-off` keyframes; add a `prefers-reduced-motion` guard for `.starting`/`.stopping`.

**Interfaces:**
- Consumes: `state`, `$("vp-power")`, `$("daemon-power")`, `apiJson`, `pollDaemonStatus`, CSS vars `--mint` (#5ee6c3 green), `--coral` (#ff6b6b red), `--ease`.
- Produces: a `.vp-power.stopping` visual state driven by `state.daemonStopping`. The existing `.vp-power.starting` pulse is preserved.

**Why:** the start branch already adds `.starting` optimistically (main.js:770); the stop branch adds no transitional class, and `/api/daemon/stop` blocks while the daemon is still `running:true`, so a poll-driven backend `"stopping"` isn't reliably observable. Drive the OFF transition from the frontend.

- [ ] **Step 1: Add the state flag.** In the `state` object literal near the top of the IIFE (~11-21), add a field:

```javascript
    daemonStopping: false,
```

- [ ] **Step 2: Optimistically enter the stopping state on click.** In the `#daemon-power` click handler, replace the `if (isRunning) { ... }` block's head (the `state.controlBusy = true; $("control-status").textContent = "stopping";` lines, ~755-756) so the branch reads:

```javascript
    if (isRunning) {
      state.controlBusy = true;
      state.daemonStopping = true;
      vpPower.classList.remove("running", "starting");
      vpPower.classList.add("stopping");
      button.disabled = true;
      button.title = "Daemon stopping…";
      button.setAttribute("aria-label", "Daemon stopping…");
      $("control-status").textContent = "stopping";
      try {
        await apiJson("/api/daemon/stop", { method: "POST" });
      } catch (error) {
        state.daemonStopping = false;
        vpPower.classList.remove("stopping");
        $("control-status").textContent = "error";
        $("control-hint").textContent = error.message || "Failed to stop daemon";
        $("control-hint").className = "panel-hint error";
      } finally {
        state.controlBusy = false;
        void pollDaemonStatus();
      }
    } else {
```
(The `else { ... }` start branch below is unchanged.)

- [ ] **Step 3: Make `pollDaemonStatus` hold the stopping state.** In `pollDaemonStatus` (~710-745): (a) add `"stopping"` to the remove-list at line 718, and (b) insert the stopping-hold block right after it, before the `if (!status.manageable)` check:

```javascript
      vpPower.classList.remove("running", "starting", "stopping", "disabled");

      if (state.daemonStopping) {
        if (status.running) {
          // Still shutting down — hold the OFF ramp and keep the button inert.
          vpPower.classList.add("stopping");
          button.title = "Daemon stopping…";
          button.setAttribute("aria-label", "Daemon stopping…");
          button.disabled = true;
          return;
        }
        // Daemon is down — clear the transitional state, fall through to red default.
        state.daemonStopping = false;
      }

      if (!status.manageable) {
```

- [ ] **Step 4: CSS — add the OFF ramp + keyframes.** In `style.css`, after the `.vp-power.starting button` rule (~447-450), add:

```css
.vp-power.stopping button {
  animation: ramp-off 1.6s var(--ease) forwards;
}
```
And add the keyframes next to `@keyframes pulse` (~281-283):

```css
@keyframes ramp-off {
  from { color: var(--mint); }
  to { color: var(--coral); }
}
```

- [ ] **Step 5: CSS — reduced-motion static frame.** Inside the existing `@media (prefers-reduced-motion: reduce)` block (~1054-1073), add:

```css
  .vp-power.starting button,
  .vp-power.stopping button {
    animation: none;
  }
```
(With animation off, `.starting` holds static green from its own `color: var(--mint)` rule and `.stopping` falls back to the default coral — both legible static frames.)

- [ ] **Step 6: Self-check.** Grep confirms the new hooks exist and the start path is untouched:

```bash
grep -n "daemonStopping\|stopping\|ramp-off" apps/reachy_controller/src/reachy_controller/static/main.js apps/reachy_controller/src/reachy_controller/static/style.css
```
Expected: `daemonStopping` set in the click handler + checked/cleared in `pollDaemonStatus`; `.vp-power.stopping` + `ramp-off` in CSS.

- [ ] **Step 7: Playwright verification (see "Verification A").** On the Mac the button is non-manageable (`.disabled`), so verify the ramp **visually**: `browser_evaluate` → `document.getElementById('vp-power').classList.remove('disabled','running');document.getElementById('vp-power').classList.add('stopping')`, screenshot the green→red ramp; then emulate `prefers-reduced-motion: reduce` and confirm no animation (static red). The live click→stopping→stopped path is covered by Verification B.

- [ ] **Step 8: Commit.**

```bash
git add apps/reachy_controller/src/reachy_controller/static/main.js apps/reachy_controller/src/reachy_controller/static/style.css
git commit -m "feat(reachy_controller): power-OFF green→red transition ramp + reduced-motion guard (#32)"
```

---

## Task 3: #36 — pause streams/polls when the tab is hidden (+ mobile CSS)

**Files:**
- Modify: `main.js` — refactor the inline boot setup (~786-821) into `startLiveConnections()` / `stopLiveConnections()`; add `live` to `state`; add a `visibilitychange` listener.
- Modify: `style.css` — pause `.blob` drift + `.glass::before` sheen on phones inside the existing `@media (max-width: 560px)` block (~1046-1051).

**Interfaces:**
- Consumes: `bindEventSource(url, handlers)` (~77-93, already returns the `EventSource` or `null`), `pollDaemonStatus`, `pollAppsStatus`, `setDaemonOnline`, `applyRobotState`, `updateStats`, `renderEqualizer`, `readEventPayload`.
- Produces: `startLiveConnections()`, `stopLiveConnections()`, and `state.live = { streams: [], timers: [], active: false }`.

**Why:** nothing pauses on `visibilitychange`; the 20 Hz audio stream + ~10 Hz telemetry + two 1 s polls keep running when the tab is hidden, and the EventSources are never closed. (Sim-canvas rAF and CSS animations are already auto-paused by the browser when hidden — no handling needed there; the streams + intervals are the leak.)

- [ ] **Step 1: Add `live` to state.** In the `state` object literal (~11-21), add:

```javascript
    live: { streams: [], timers: [], active: false },
```

- [ ] **Step 2: Add start/stop helpers.** In `main.js`, just above the boot section (above `void pollDaemonStatus();` ~786), add:

```javascript
  function startLiveConnections() {
    if (state.live.active) return;
    state.live.active = true;

    void pollDaemonStatus();
    void pollAppsStatus();
    state.live.timers.push(
      setInterval(() => void pollDaemonStatus(), 1000),
      setInterval(() => void pollAppsStatus(), 1000),
    );

    const telemetry = bindEventSource("/telemetry/state-stream", {
      "state-update": (event) => {
        const payload = readEventPayload(event);
        setDaemonOnline(true);
        applyRobotState(payload);
      },
      "daemon-offline": () => setDaemonOnline(false),
      error: () => setDaemonOnline(false),
    });
    const stats = bindEventSource("/api/system/stats-stream", {
      "stats-update": (event) => updateStats(readEventPayload(event)),
      error: () => {
        $("v-host").textContent = "stats reconnecting";
      },
    });
    const audio = bindEventSource("/api/audio/levels-stream", {
      "audio-level": (event) => {
        const payload = readEventPayload(event);
        const speechDetected = payload && payload.speech_detected === true;
        renderEqualizer(speechDetected);
      },
      "daemon-unavailable": () => renderEqualizer(false, "no audio signal"),
      error: () => renderEqualizer(false, "audio stream reconnecting"),
    });
    state.live.streams.push(telemetry, stats, audio);
  }

  function stopLiveConnections() {
    if (!state.live.active) return;
    state.live.active = false;
    state.live.timers.forEach((id) => clearInterval(id));
    state.live.timers = [];
    state.live.streams.forEach((source) => {
      if (source) source.close();
    });
    state.live.streams = [];
  }
```

- [ ] **Step 3: Replace the inline boot setup with the helpers + visibility wiring.** Replace the boot block (the two `setInterval` calls + three `bindEventSource` calls + the trailing `renderApps()`/`renderEqualizer()`/`setDaemonOnline(false)`/`pollAppsStatus` lines, ~786-821) with:

```javascript
  renderApps();
  renderEqualizer(false, "no audio signal");
  setDaemonOnline(false);
  startLiveConnections();

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopLiveConnections();
    else startLiveConnections();
  });
```
(Note: `pollDaemonStatus`/`pollAppsStatus` are now called inside `startLiveConnections`, so the duplicate eager calls are removed.)

- [ ] **Step 4: CSS — pause blob drift + sheen on phones.** Inside the existing `@media (max-width: 560px)` block (~1046-1051, where `.glass` backdrop-filter is already simplified), add:

```css
  .blob { animation: none; }
  .glass::before { animation: none; }
```

- [ ] **Step 5: Self-check.** Confirm streams are closeable and wired:

```bash
grep -n "startLiveConnections\|stopLiveConnections\|visibilitychange\|\.close()" apps/reachy_controller/src/reachy_controller/static/main.js
```
Expected: both helpers defined + called; `visibilitychange` listener present; `source.close()` in `stopLiveConnections`. Ensure no remaining bare `setInterval(`/`bindEventSource(` calls in the boot section outside `startLiveConnections`.

- [ ] **Step 6: Playwright verification (see "Verification A").** With controller + sim daemon up and panels online: `browser_evaluate` to read live state before/after a synthetic visibility change. Dispatch hide → assert the 3 EventSources are closed (`readyState===2`) and timers cleared; dispatch show → assert they re-open and telemetry resumes.

- [ ] **Step 7: Commit.**

```bash
git add apps/reachy_controller/src/reachy_controller/static/main.js apps/reachy_controller/src/reachy_controller/static/style.css
git commit -m "perf(reachy_controller): pause SSE streams + polls when tab hidden; quiet mobile animations (#36)"
```

---

## Verification

### A — Mac-side agent verification (Playwright MCP)

Bring up the stack on the Mac (two background processes from the repo root):

```bash
# sim daemon (serves :8000)
uv run python -m reachy_mini.daemon.app.main --sim --headless
# controller (serves 127.0.0.1:7861)
cd apps/reachy_controller && uv run python -m reachy_controller.main
```
Then drive `http://localhost:7861` with the Playwright MCP. Hard-refresh after each rebuild (no Cache-Control on static assets).

- **#38:** focus `#vol-spk`; in a loop over ~1.2 s dispatch `input` events while sampling `#vol-spk.disabled` and `#testsound.disabled` every ~16 ms — assert both remain `false` for the whole window (old code flips them `true` for the POST duration). Confirm `#control-status` transitions `speaker → ok`. (Tactile drag-feel is finalized in B / owner pass.)
- **#32:** force-show the button (`classList` remove `disabled`/`running`, add `stopping`) and screenshot the green→red ramp; re-run under emulated `prefers-reduced-motion: reduce` and confirm static (no animation).
- **#36:** read `EventSource` count/`readyState` via `browser_evaluate`; dispatch a `visibilitychange` to hidden → assert streams closed + timers cleared; to visible → assert re-opened and telemetry numbers update again.

Regression sanity (no backend change expected, run once at the end):
```bash
cd apps/reachy_controller && uv run pytest -q
```
Expected: all green.

### B — Live-Jetson functional pass (tunnel)

Per memory `agent-driven-functional-qa`: tunnel the controller port and drive the on-device controller (where the daemon power button is manageable):
```bash
ssh -N -L 7870:localhost:7861 <jetson>   # then drive http://localhost:7870
```
- **#32:** click the live power button to stop the daemon; confirm the real `.stopping` green→red ramp shows for the stop duration and settles red once `:8000` is unreachable; restart and confirm the start pulse still works. **Coordinate timing with the owner — this stops/starts the whole stack.**
- **#36:** confirm streams pause on backgrounding the tab and resume on foreground (DevTools network / telemetry liveness).
- **Owner final pass:** real phone-battery drop when backgrounded; slider drag feel on the touchscreen; reduced-motion device check.

## PR

One branch `fix/reachy-controller-ui-38-32-36`; likely one PR `Closes #38, #32, #36`. Split the bug (#38) into its own PR at `finishing-a-development-branch` if review prefers. Base = `feat/reachy-chat-vision`. Use `--repo zeapsu/reachy-console` for all gh ops (memory `gh-cli-wrong-repo-in-reachy`).
