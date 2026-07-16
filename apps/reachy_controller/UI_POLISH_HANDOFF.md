# Reachy Controller — UI Polish Handoff

For the next agent doing **visual polish** of the Liquid Glass UI. This repo runs on a headless
arm64 Jetson where browsers/Playwright are unavailable, so visual QA should happen on a
**browser-capable machine** (pull this branch, render it). Functional/robot verification happens
later back on the Jetson (that's where the daemon + hardware are).

## What this app is
`apps/reachy_controller/` — a FastAPI + **vanilla JS, no build step** "Liquid Glass" control console
for a Reachy Mini robot. Binds `127.0.0.1:7861`. It talks to the robot daemon (`:8000`) over HTTP/WS
and **never instantiates `ReachyMini()`**. Don't introduce a framework or a build step.

## Run it (to view the UI)
```bash
cd apps/reachy_controller
uv run python -m reachy_controller.main      # serves http://127.0.0.1:7861
```
The **robot daemon will be down** in a polish session, which is fine: the Jetson stats panel is live
(local), and the daemon-dependent panels show honest offline states (schematic idle, camera
"Daemon offline", controls disabled, audio idle). That's expected — polish the *visuals*, not the data.

## Files
- `src/reachy_controller/static/index.html` — structure (glass panels, top bar, hero viewport, launcher, stats, audio, controls).
- `src/reachy_controller/static/style.css` — the Liquid Glass material + layout (~900 lines).
- `src/reachy_controller/static/main.js` — wiring (EventSource/fetch to real endpoints; the viewport mode switch + canvas/SVG render loops).
- **`mockup.html`** (app root) — the **design source of truth**: the original standalone Liquid Glass
  mockup with mock data. The live UI was ported from it. When in doubt about intended look/behavior,
  compare against this file.

## Design intent (don't regress)
"Instrument Glass": cockpit/HUD for a friendly desk robot. Dark workshop-dusk background with
drifting color fields the glass refracts; translucent panels with specular rim + slow sheen sweep +
pointer-tracked highlight; large continuous-radius corners. Palette: amber `#FFC857` = live/active,
cyan `#6FE3FF` = telemetry, coral `#FF6B6B` = stop/danger, mint `#5EE6C3` = ok. Fonts: Space Grotesk
(display), Inter (UI), JetBrains Mono (numeric readouts). Respect `prefers-reduced-motion`, keep it
responsive to mobile, keep visible keyboard focus.

## KNOWN ISSUE — fix this first
**The hero swappable viewport (camera / sim / schematic) renders distorted.** Likely a CSS
sizing/aspect-ratio/overflow bug or canvas width/height not being set to the element's rendered size
in the port. Compare the viewport's CSS + the canvas-sizing JS against `mockup.html` (where it looked
correct) and reconcile. Check all three modes and the telemetry-HUD overlay, and check responsive
widths.

## Guardrails
- Vanilla JS only; no framework, no bundler.
- Don't break the wired endpoints (telemetry `/telemetry/state-stream`, `/api/system/stats-stream`,
  `/api/audio/levels-stream`, `/api/apps/status`, `/api/camera/availability`, `/api/volume/*`,
  `/api/motors/*`, `/api/media/*`). Changing markup is fine; keep the JS hooks (ids/classes it queries).
- Keep `uv run pytest -q` green (backend tests assert the page references its live endpoints).
- Don't implement the live camera video stream — that's a separate Jetson-side milestone (M5b).
- Implementation convention for this repo is Codex-authored (see CLAUDE.md); follow whatever the
  polishing harness uses, but the constraint is: verify the rendered result, don't trust summaries.

## After polishing
Push the branch; the Jetson session will pull it and run **functional verification** with the robot
daemon up (live telemetry, camera, controls, audio).
