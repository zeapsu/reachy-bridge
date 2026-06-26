# Reachy Controller — Foreground Battery/Heat Drain Fix Plan

> **For agentic workers:** continuation of the #36 work in PR #42. The `visibilitychange`
> teardown (commit 982afe0) fixed the *backgrounded* case; this plan fixes the remaining
> *focused/foreground* drain. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Stop the controller PWA from generating sustained CPU/GPU load (battery drain + heat)
while its tab is focused on an iPhone.

**Architecture:** Two surgical changes to the static assets — no build step, no backend, controller
still HTTP/WS-only. (1) The 20 Hz audio-levels stream calls `renderEqualizer`, which unconditionally
rewrites 36 `bar.style.height` values *every tick, forever* (the easing + `toFixed(1)` never goes
quiet even in silence); those bars sit inside a `backdrop-filter: url(#lg-refract)` `.glass` panel
with `will-change: height`, so iOS Safari re-rasterizes the whole filtered panel 20×/s and pins 36
GPU layers. Add a change-guard + idle-settle so idle does zero per-bar work. (2) The `max-width:560px`
mobile guard may not match a home-screen PWA's reported viewport, leaving the refraction filter and
always-on idle animations (sheen, status-dot pulses, rec badge) running; re-key those reductions off
`(hover:none) and (pointer:coarse)` so they apply by device capability, not width.

**Tech Stack:** vanilla JS + CSS, FastAPI static assets.

## Global Constraints

- No build step, no framework, no bundler — vanilla JS/CSS only.
- Controller never instantiates `ReachyMini()`; no Python/backend changes here.
- Preserve the Liquid Glass aesthetic on desktop; degrade gracefully on touch/reduced-motion.
- Never put `filter: url(#…)` on `.vp-stage` (warps live content); refraction stays a `.glass`
  **backdrop**.
- Behavior of the live equalizer must be visually identical (same heights, same easing) — we only
  skip redundant writes.

---

### Task 1: Change-guard + idle-settle the equalizer (`static/main.js`)

**Files:**
- Modify: `apps/reachy_controller/src/reachy_controller/static/main.js` (the `renderEqualizer`
  closure, currently lines ~417-429)

**What & why:** `renderEqualizer` writes all 36 bar heights on every call (20 Hz from
`/api/audio/levels-stream`, `_POLL_INTERVAL_SECONDS = 0.05`). Make each write conditional on the
rounded value changing, snap idle bars onto their floor target so they stop drifting asymptotically,
and early-return once idle has fully settled so the tick does zero per-bar work. Guard the `eq-peak`
text write too.

- [ ] **Step 1: Add per-bar memo + settle state** next to `eqState`/`eqProfile` (after the
  `eqProfile` line):

```javascript
  const eqLast = new Array(NBARS).fill(null);
  let eqSettled = false;
  let eqPeakLast = null;
```

- [ ] **Step 2: Replace the `renderEqualizer` function body** with the change-guarded version:

```javascript
  function renderEqualizer(speechDetected, label = null) {
    const peak = label || (speechDetected ? "speech detected" : "no audio signal");
    if (peak !== eqPeakLast) {
      eqPeakLast = peak;
      $("eq-peak").textContent = peak;
    }
    // Once idle has settled to the floor, do zero per-bar work each tick. The
    // 20 Hz audio stream otherwise rewrites 36 bar heights forever, and those
    // bars sit inside a backdrop-filtered panel that iOS re-rasterizes on every
    // write — the focused-tab battery/heat drain (#36).
    if (!speechDetected && eqSettled) return;
    eqEl.classList.toggle("idle", !speechDetected);
    const normalized = speechDetected ? 0.7 : 0.0;
    let settled = !speechDetected;
    bars.forEach((bar, index) => {
      const target = 5 + normalized * 86 * eqProfile[index];
      eqState[index] += (target - eqState[index]) * 0.42;
      if (!speechDetected && Math.abs(eqState[index] - target) < 0.1) {
        eqState[index] = target;
      } else if (!speechDetected) {
        settled = false;
      }
      const next = `${Math.max(4, eqState[index]).toFixed(1)}%`;
      if (next !== eqLast[index]) {
        eqLast[index] = next;
        bar.style.height = next;
      }
    });
    eqSettled = settled;
  }
```

- [ ] **Step 3: Syntax gate** — `node --check apps/reachy_controller/src/reachy_controller/static/main.js`
  → exit 0.

- [ ] **Step 4: Behavioral verify (Playwright, Mac)** — instrument the page so each `bar.style.height`
  assignment is counted. With the daemon online and no speech, the per-tick write count must fall to
  ~0 within ~1 s (vs. 36/tick continuously on the old code). Drive a `speech_detected:true` payload and
  confirm bars still update (writes resume), then return to idle and confirm they settle again.

- [ ] **Step 5: Commit** — `perf(reachy_controller): idle-settle the equalizer so it stops 20 Hz
  backdrop re-raster (#36)`.

---

### Task 2: Touch-device GPU guards (`static/style.css`)

**Files:**
- Modify: `apps/reachy_controller/src/reachy_controller/static/style.css`

**What & why:** Remove the permanent 36-layer promotion; degrade the GPU-heavy material by device
capability so it applies even when a PWA reports a wide viewport.

- [ ] **Step 1: Drop `will-change: height`** from `.eq .bar` (was line ~760). The bars are written
  directly every active frame; permanent layer promotion on 36 elements is pure idle overhead. Keep
  the `transition: height 0.12s linear` line.

- [ ] **Step 2: Add a capability-keyed perf block** after the `@media (max-width: 560px)` block and
  before the `prefers-reduced-motion` block:

```css
/* Touch devices (phones/tablets) match (hover: none) and (pointer: coarse)
   regardless of the viewport width a home-screen PWA reports — so key the
   GPU-heavy reductions off capability, not the px breakpoint above (which a
   wide-reporting PWA can miss). Drop the animated refraction to a lighter
   frost and stop the always-on idle animations: each kept the iOS compositor
   re-rasterizing every frame while the tab is focused (#36). */
@media (hover: none) and (pointer: coarse) {
  .glass {
    backdrop-filter: blur(16px) saturate(1.55) brightness(1.05);
    -webkit-backdrop-filter: blur(16px) saturate(1.55) brightness(1.05);
  }
  .blob,
  .glass::before,
  .dot.ok,
  .dot.loading,
  .dot.stopping,
  .vp-badge .rec {
    animation: none;
  }
}
```

- [ ] **Step 3: Add `.dot.stopping`** to the existing `prefers-reduced-motion` `animation: none`
  list (it currently lists `.dot.ok, .dot.loading` but not `.dot.stopping`).

- [ ] **Step 4: Verify** the page still loads with no new console errors and the equalizer/material
  render correctly on desktop (Playwright). Confirm the new rules are present and the `@media` query
  matches under touch emulation.

- [ ] **Step 5: Commit** — `perf(reachy_controller): degrade glass material on touch devices by
  capability, not width (#36)`.
