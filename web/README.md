# web — controller dashboard

React + TypeScript dashboard for the reachy-bridge controller, built with Vite and
managed with Bun. The Rust controller (`../controller`) embeds `dist/` and serves
`/` (dashboard) and `/assets/*`.

## Commands

Run from `web/`. Bun isn't on PATH by default; prefix each command with `mise exec bun@1.4.3 --`.

```sh
bun install          # dependencies (exact versions, bun.lock is committed)
bun run dev          # Vite dev server with API proxy (see below)
bun run typecheck    # tsc -b
bun test             # unit tests for the pure logic in src/logic
bun run build        # typecheck, then build into dist/
```

## Dev proxy

`bun run dev` forwards `/api` and `/telemetry` (the robot state stream is
`/telemetry/state-stream`) to a running controller at `http://127.0.0.1:7861`.
Point it elsewhere with `CONTROLLER_URL`:

```sh
CONTROLLER_URL=http://127.0.0.1:7999 bun run dev
```

The dev server listens on `localhost` only.

## Layout

- `index.html`: the Vite entry.
- `src/styles/base.css`: design tokens (light, dark, accents), the glass
  material and the shared controls. Each component imports its own
  stylesheet next to it for its layout.
- `src/glass.ts`: the live parts of the glass: per-element refraction
  (desktop Chromium) and the pointer light.
- `src/api.ts`: typed fetch helper, response types, and every API path.
- `src/logic/`: pure functions (power state, app cards and gates, expression
  gating, log capping, pose, audio meter model, stats, kinematics, app-view
  start steps), each tested by a `*.test.ts` file next to it.
- `src/hooks/`: SSE with reconnect, polling, page visibility, and the stateful
  domain hooks (apps, power, expressions).
- `src/components/`: the shell (top bar, Robot / Apps / System tabs, the phone
  tab bar), Viewport (live camera and the 3D robot), the expression carousel,
  ControlsPanel, AppsPanel, SystemPanel (board stats and logs), AudioPanel,
  DevicesPanel and Appearance (light/dark, accent, wallpaper).

The 10 Hz updates (3D pose, telemetry, audio meter) write to the DOM through refs
with change guards instead of re-rendering React.
