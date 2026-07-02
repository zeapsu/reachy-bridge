# Reachy Controller — full restyle + Expressions card (design)

**Closes #49** (Liquid Glass craft pass, mockup-first) and **#48** (expression carousel).
Advances epic **#4**. Branch base: `feat/reachy-chat-vision`.

## Problem / motivation

The controller is a vanilla-JS "Liquid Glass" console (`apps/reachy_controller`). Part 1 of the UI
epic (#4) landed the base material and the viewport-distortion fix. #49 asks for an *additional*,
design-led craft pass; #48 adds an **Expressions** card that plays emotes/dances over the daemon's
move API. The owner wants this driven as a genuine design exploration — **three distinct
full-console remixes** as separate mockups, not one reskin — forward-compatible with the panels
still coming (#14 audio visualizer, #7 log stream, #3 live camera), and **mobile/PWA-first** (the
phone is the second primary platform). The winning direction is ported to the live UI; the
functional Expressions card ships on top.

**Key architectural finding:** #48 needs **zero backend code**. The transparent `/api/{path:path}`
proxy (`apps/reachy_controller/src/reachy_controller/daemon_proxy.py:191`) already forwards the
move routes to the daemon. The whole card is frontend.

## Design intent (interface-design + frontend-design)

**Human:** a maker/developer who owns a Reachy Mini Lite — a small, expressive desktop companion
robot. At their desk (kiosk on the Jetson) or across the room on a phone (PWA). Part operator
(monitors pose/audio/DOA/Jetson health, sets motor mode, launches apps), part playful owner
(triggers emotes and dances). **Feel:** alive, precise, a little magical — a window into a small
living machine. Not sterile SCADA, not a toy.

**Domain:** expressive antennas (the defining physical feature + emotional channel), head
roll/pitch/yaw, DOA beamforming ("where it's listening"), motor-torque lifecycle (wake/sleep),
emotes & dances (it's a performer), the Jetson substrate, the tailnet presence.

**Color world:** Reachy's white shell + dark faceplate + glowing eyes; a lab at night (blue-black,
monitor glow); amber DOA radar sweep; phosphor-cyan scope traces; one warm "alive" accent. The
current cyan/amber/coral/mint is partly world-derived; each direction must *earn* its palette.

**Signature (the through-line in all three):** the robot's **live presence** — its antennas
mirroring real positions + the DOA sweep showing where it's listening — as a persistent,
characterful element, not a buried schematic panel.

**Rejecting:** (1) generic SaaS card grid with `--gray-700` tokens → tokens from the robot's world;
(2) cold SCADA that ignores the antennas/dances → keep companion warmth, make Expressions joyful and
first-class; (3) the "near-black + one acid accent" AI cliché → every accent motivated by the domain.

## The three directions (each = one standalone mockup)

1. **Observatory** — the live antenna+DOA presence is the hero *scope* readout; calibrated
   instruments around it. Matte carbon, phosphor-cyan traces, amber sweep, tabular mono, 8px
   baseline grid. Warmth injected via a living heartbeat pulse. Motion: scope-sweep / needle easing,
   minimal decoration. **Mobile:** scope pinned top, instruments in a swipeable stack.

2. **Companion** — presence & expression lead; the robot's mood (antennas + where it's listening)
   is always visible and characterful; telemetry demoted to on-demand tabs; **Expressions is a
   joyful first-class filmstrip**. Soft full Liquid Glass, warm "alive" amber alongside cyan,
   cinematic crossfades. **Mobile:** native bottom-tab remote (Stage / Express / Vitals / Controls)
   — desktop is this design scaled up → best PWA parity.

3. **Habitat** — a modular bento grid; each faculty (sight / sound / motion / expression / vitals /
   apps) is a tile tinted by its own domain color, sized by importance; the live-presence tile is
   the 2×2 anchor. Crisp tinted glass, tactile hover-lift + press. **Mobile:** pure-CSS `auto-fit`
   reflow to a single-column stack by priority. Most naturally extensible as panels keep arriving.

## Deliverable & layout rules

- Three standalone `mockups/mockup-{observatory,companion,habitat}.html` (inline CSS, self-contained
  and directly comparable in a browser). The existing tracked `mockup.html` is the M1 reference.
- **Free-rein layout** — remixes may rearrange the grid — but must stay portable to the existing
  vanilla-JS DOM (CSS/markup restructuring, no framework/bundler).
- Every mockup accounts for **all six panels** (viewport, audio, Jetson stats, apps, controls,
  **Expressions**) **plus reserved slots** for the audio visualizer (#14) and the stats↔logs swap (#7).
- Both **desktop** (~1440px) and **mobile** (~390px) treatments in each mockup.

## Expressions card — functional spec (#48)

- Two visually separated sections — **Emotions** and **Dances** — each a horizontal scrollable
  chip-strip (filmstrip). Don't mix them in one flat strip.
- Populate **both sections dynamically** from
  `GET /api/move/recorded-move-datasets/list/{dataset}` (no hard-coded name list):
  - `pollen-robotics/reachy-mini-emotions-library` → Emotions
  - `pollen-robotics/reachy-mini-dances-library` → Dances
- Tap a chip → `POST /api/move/recorded-move-dataset/{dataset}/{move}`. **Play one move at a time**;
  reflect a "playing…" state; do not fire concurrent moves.
- Degrade gracefully when the daemon is offline (consistent with the other panels).
- Out of scope: recording/editing moves; per-move thumbnails/previews.

## Constraints (AGENTS.md)

- **Vanilla JS, no build step, no framework/bundler.** (Next.js was considered and rejected —
  runtime mismatch with the FastAPI/Jetson host, battery/PWA cost, and no view-layer problem to
  solve; the app is a telemetry dashboard that hand-rolls change-guards to *avoid* re-renders.)
- **Never** put `filter: url(#…)` (feDisplacementMap) on `.vp-stage` — it warps live
  camera/sim/schematic content. Liquid-glass refraction goes on the panel **backdrop**
  (`backdrop-filter: url(#lg-refract)`, Chromium-only behind `@supports`) with a CSS-only rim/frost
  fallback for Safari/Firefox.
- Honor `prefers-reduced-motion` and `prefers-reduced-transparency`.
- The port must preserve every existing JS hook/ID so `main.js` keeps working.

## Reuse (don't reinvent) — `static/main.js`

- `apiJson()` (:55) — fetch wrapper for the card's list + play calls.
- `guard()` (:220) — change-guard, for "playing…" / offline state writes.
- `renderApps()` (:326) + `performAppAction()` (:403) — the chip-render + action-dispatch pattern
  the Expressions card mirrors.
- `setControlsEnabled()` (:141) — daemon-offline gating.

## Verification

- **Serve:** `uv run python -m reachy_controller.main`; daemon-offline panels are the expected QA
  state. Drive with Playwright at 1440px and 390px.
- **#48 logic (Mac, no daemon):** fake `fetch` for the list/play routes; assert chips render from
  the list, tap POSTs the correct path, concurrent taps are guarded, offline disables the card.
- **Existing tests:** `cd apps/reachy_controller && uv run python -m pytest` stays green (no backend
  change; the proxy already forwards the move routes).
- **Real actuation (#48):** owner light HW pass on the Jetson — tapping a chip actuates the real
  robot, so one move at a time, coordinated. Ships as the functional-verification part of the PR.

---

## Addendum (2026-07-01) — the shipped #49 direction: "Vivarium"

The three remix mockups above (+ a hyperreal-glass synthesis) were **rejected** — "worse than the
original; the original Liquid Glass is the bar." The direction that shipped instead was designed
anchored to the shipped UI (rendered first, then changed as deltas — see memory
`design-anchor-to-shipped-ui`): **same layout, same material family; restructured depth, hierarchy
and character.** Review artifact: `apps/reachy_controller/mockups/mockup-vivarium.html`
(self-contained: real stylesheet inlined + the delta layer that became the port diff).

The concept: the console is a **glass vivarium** — one hero pane you look *through* at the living
creature (the stage), quiet instrument glass you look *at* (everything else).

1. **Glass altitudes** — only the viewport keeps the full hyperreal material + sheen;
   `.glass.instrument` (statusbar + 5 panels) recedes: flatter fill, thinner edge, no sheen,
   lighter frost. Atmosphere gains a static vignette + grain.
2. **The creature got the craft** — schematic redrawn (lens-ring eyes, the larger right eye is the
   camera; torso; floor glow) with idle breathing on a wrapper `<g class="breath">` so CSS never
   fights the transform attributes main.js writes (pivots preserved: head 160,104 · body 160,152 ·
   antennas 132,70/188,70). **Sleep state** (eyes close, breath slows) rides the existing
   `#rig.offline` class; the **listening glow** on the DOA arc rides `#rig.speaking`. Zero JS for
   the states.
3. **Accent discipline** — cyan belongs to the creature only; one theme accent carries everything
   interactive (gauges unified, sliders, EQ, memory bar, spinner, focus rings); mint/coral stay
   semantic. Also finished #49's tokenisation: previously-hardcoded amber rgba tints
   (`.app-card.running`, `.dot.live` glow, switch-checked, hover glows) now derive from the accent
   via `color-mix`, so every scheme recolours them.
4. **DOA leads the HUD** at display size (the signature readout: where the robot is listening).
5. **Expressions as a performance deck** — larger chips, edge-fade scroll masks, pulsing cue dot on
   the playing chip. No emoji.
6. **Status pills join into one `.pillbar` strip**; right column reordered apps → expressions →
   controls.
7. **Motion** — creature breath + one-time panel entrance + press feedback; all disabled under
   `prefers-reduced-motion`; breath also disabled on touch. The `(hover:none)`/
   `prefers-reduced-transparency` blocks repeat `.glass.instrument` so the higher-specificity
   instrument rule cannot re-enable backdrop blur on phones (#43 guard preserved).

main.js changes: only the three gauge arc colour literals (→ `var(--amber)`).
