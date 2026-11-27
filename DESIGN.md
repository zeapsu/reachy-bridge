---
version: "1.0"
name: "Reachy Bridge Design System"
project: "reachy-bridge"
aesthetic: "Tactile Liquid Glass — Hardware-First, State Over Sentences"
source_of_truth:
  tokens: "web/src/styles/base.css"
  refraction: "web/src/glass.ts"
  appearance: "web/src/logic/appearance.ts"
  ux_ratchets: "web/e2e/ux.spec.ts"
  a11y_ratchets: "web/e2e/a11y.spec.ts"
theme:
  default: "dark"
  modes: ["auto", "dark", "light"]
  default_accent: "amber"
colors:
  dark:
    ground: "#0b0f14"
    ink: "#f2f5f8"
    ink_2: "rgba(242, 245, 248, 0.70)"
    ink_3: "rgba(242, 245, 248, 0.48)"
    accent: "#ffb547"
    accent_ink: "#1c1206"
    ok: "#5fe0a8"
    warn: "#ffd166"
    danger: "#ff6b6b"
    tint: "rgba(255, 255, 255, 0.055)"
    tint_strong: "rgba(255, 255, 255, 0.11)"
    well: "rgba(0, 0, 0, 0.18)"
    hair: "rgba(255, 255, 255, 0.10)"
    rim: "rgba(255, 255, 255, 0.55)"
    rim_soft: "rgba(255, 255, 255, 0.12)"
    shadow: "rgba(0, 0, 0, 0.38)"
  light:
    ground: "#e9eef2"
    ink: "#11161c"
    ink_2: "rgba(17, 22, 28, 0.68)"
    ink_3: "rgba(17, 22, 28, 0.48)"
    accent: "#e8890c"
    accent_ink: "#1c1206"
    ok: "#138a5c"
    warn: "#a86b00"
    danger: "#d43c3c"
    tint: "rgba(255, 255, 255, 0.30)"
    tint_strong: "rgba(255, 255, 255, 0.55)"
    well: "rgba(20, 40, 60, 0.07)"
    hair: "rgba(17, 22, 28, 0.10)"
    rim: "rgba(255, 255, 255, 0.95)"
    rim_soft: "rgba(255, 255, 255, 0.40)"
    shadow: "rgba(40, 60, 80, 0.22)"
  accents:
    amber: { accent: "#ffb547", accent_light: "#e8890c", accent_ink: "#1c1206" }
    arctic: { accent: "#6fd6ff", accent_ink: "#04141c" }
    emerald: { accent: "#3ce3a0", accent_ink: "#03170f" }
    ultraviolet: { accent: "#c199ff", accent_ink: "#160a2a" }
    ember: { accent: "#ff8a5c", accent_ink: "#1f0b03" }
typography:
  sans: '"Onest", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif'
  mono: '"JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace'
  base_size: "15px"
  base_line_height: 1.4
radii:
  sheet: "28px"
  subcard: "22px"
  row: "16px"
  chip: "14px"
  tile: "13px"
  field: "12px"
  pill: "999px"
spacing:
  unit: "4px"
  scale: [4, 6, 8, 10, 12, 14, 16, 18, 20, 24, 28, 32]
motion:
  spring: "cubic-bezier(0.2, 1.6, 0.4, 1)"
  ease: "cubic-bezier(0.22, 1, 0.36, 1)"
breakpoints:
  tablet_phone: "860px"
  phone_compact: "480px"
  phone_narrow: "420px"
---

# DESIGN.md — Reachy Bridge

> **Purpose:** Single source of truth for the `reachy-bridge` visual language, interaction philosophy, tokens, and UI constraints. Coding agents and contributors should read this before proposing mockups or changing anything in `web/`.

---

## 1. Visual Theme & Atmosphere

**Tactile Liquid Glass over a calm hardware stage.** Reachy Bridge is a remote controller for a physical desktop robot (Reachy Mini Lite) designed for both power users and households sharing one robot across phones, tablets, and desktops.

- **Material personality:** Translucent, optically grounded glass controls (frosted backdrop, directional specular rim, subtle chromatic refraction on desktop Chromium) floating over a quiet, low-noise studio gradient or user-chosen wallpaper.
- **State over sentences (Direction B):** The interface communicates state visually—dimming what is unavailable, lighting up what is active, and offering the single obvious next action (for example, a dimmed sleeping 3D robot with a `Power on` button at the base of the stage). Users should never have to read explanatory paragraphs to operate the robot.
- **Calm, uncluttered density:** One surface per job. Control panels stay scannable and tactile; explanations, troubleshooting, and quirks live in `docs/manual.md` (linked from `System`), never inline as justification copy.
- **Dark by default, light first-class:** Defaults to deep studio slate (`#0b0f14`) with warm amber (`#ffb547`) highlights; follows `prefers-color-scheme` in `auto` mode and supports explicit `light` and `dark` overrides applied before first paint.

---

## 2. Color Palette & Roles

All colors are defined as CSS custom properties in `web/src/styles/base.css`. Never hardcode raw hex values in component stylesheets when a semantic token applies.

### Core Surfaces & Ink

| Token | Dark (default) | Light (`data-theme="light"`) | Role |
|---|---|---|---|
| `--ground` | `#0b0f14` | `#e9eef2` | Root viewport canvas and `theme-color` fallback |
| `--wall` | `radial-gradient(90% 70% at 40% 35%, #222a35, #0b0e13 75%)` | `radial-gradient(90% 70% at 40% 35%, #fbfcfd, #d6dce3 75%)` | Default "Studio" ambient wallpaper behind glass |
| `--veil` | `5, 8, 12` (RGB triplet) | `255, 255, 255` (RGB triplet) | Vignette and dim overlay over wallpapers (`--dim-a`, default `0.25`, max `0.70`) |
| `--ink` | `#f2f5f8` | `#11161c` | Primary text, active tab labels, numeric readouts |
| `--ink-2` | `rgba(242, 245, 248, 0.70)` | `rgba(17, 22, 28, 0.68)` | Secondary text, inactive controls, log lines |
| `--ink-3` | `rgba(242, 245, 248, 0.48)` | `rgba(17, 22, 28, 0.48)` | Section headers, muted metadata, notes, placeholders |

### Glass, Wells & Hairlines

| Token | Dark | Light | Role |
|---|---|---|---|
| `--tint` | `rgba(255, 255, 255, 0.055)` | `rgba(255, 255, 255, 0.30)` | Glass body lower gradient stop |
| `--tint-strong` | `rgba(255, 255, 255, 0.11)` | `rgba(255, 255, 255, 0.55)` | Glass top highlight and default button fill |
| `--well` | `rgba(0, 0, 0, 0.18)` | `rgba(20, 40, 60, 0.07)` | Recessed inner containers (segmented track, fields, chips, waiting card, control strip) |
| `--hair` | `rgba(255, 255, 255, 0.10)` | `rgba(17, 22, 28, 0.10)` | `1px` structural row dividers |
| `--rim` | `rgba(255, 255, 255, 0.55)` | `rgba(255, 255, 255, 0.95)` | Specular rim peak along the lit glass edge |
| `--rim-soft` | `rgba(255, 255, 255, 0.12)` | `rgba(255, 255, 255, 0.40)` | Ambient glass edge and top inset highlight |
| `--shadow` | `rgba(0, 0, 0, 0.38)` | `rgba(40, 60, 80, 0.22)` | Ambient elevation drop shadow |

### Semantic Status Colors

Use semantic colors strictly to communicate state—never for decorative chrome.

| Token | Dark | Light | Role |
|---|---|---|---|
| `--ok` | `#5fe0a8` | `#138a5c` | Healthy / live / robot running / connected |
| `--warn` | `#ffd166` | `#a86b00` | Transitional (`loading`, `stopping`), Sim badge, silent mic warning |
| `--danger` | `#ff6b6b` | `#d43c3c` | Errors, destructive actions (`Revoke`, `Uninstall`), failed checks |

### Accent Schemes (`data-scheme` on `<html>`)

User-selectable in `System › Settings › Appearance` (`web/src/logic/appearance.ts`). `--accent-ink` is always dark for high-contrast legibility on filled primary controls.

| Scheme | `--accent` | `--accent-ink` | Notes |
|---|---|---|---|
| `amber` (default) | `#ffb547` (light: `#e8890c`) | `#1c1206` | Warm hardware indicator glow |
| `arctic` | `#6fd6ff` | `#04141c` | Cool cyan |
| `emerald` | `#3ce3a0` | `#03170f` | Fresh green |
| `ultraviolet` | `#c199ff` | `#160a2a` | Soft violet (labeled "Violet") |
| `ember` | `#ff8a5c` | `#1f0b03` | Warm coral-orange |

### Built-in Wallpapers

`studio` (theme's `--wall`), `dawn` (soft peach-to-periwinkle gradient), `fjord` (deep slate-teal gradient), `paper` (warm off-white), or `photo` (user upload scaled to max `1920px` longest side in `localStorage`), always softened by the `.wall::after` radial vignette veil.

---

## 3. Typography Rules

- **Primary UI font (`--font`):** `Onest` (`400`, `500`, `600`, `700`), falling back to `ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif`.
- **Monospace font (`--mono`):** `JetBrains Mono` (`400`, `500`), falling back to `ui-monospace, "SF Mono", Menlo, monospace`. Always pair with `font-variant-numeric: tabular-nums` for telemetry, logs, slider readouts, and pairing codes so live numbers never jitter.
- **Rendering:** `-webkit-font-smoothing: antialiased` on `body`.

### Type Scale

| Role | Font | Size / Line-height | Weight | Tracking / Case | Color |
|---|---|---|---|---|---|
| Pairing code (`.login-code`) | `--mono` | `clamp(40px, 12vw, 56px)` | `600` | `0.08em`, `tabular-nums` | `--accent` |
| Brand wordmark (`.brand`) | `--font` | `19px` | `700` | `-0.02em` | `--ink` |
| Stage overlay title (`.stage-note-title`) | `--font` | `19px` | `600` | `-0.01em` | `--ink` |
| Selected item title (`.picked b`) | `--font` | `17px` | `600` | `-0.01em` | `--ink` |
| Body default (`body`) | `--font` | `15px / 1.4` | `400` | normal | `--ink` |
| Setting / list row (`.row`) | `--font` | `14.5px` | `400–600` | normal | `--ink` |
| Tab / field / control (`.field`, `.seg`) | `--font` | `13.5–14px` | `400–600` | normal | `--ink` / `--ink-2` |
| Button (`.btn`) | `--font` | `13.5px` | `600` | normal | `--ink` / `--accent-ink` |
| Status pill & note (`.status`, `.note`) | `--font` | `13px / 1.45` | `400–600` | normal | `--ink-2` / `--ink-3` |
| Chip / badge / small meta (`.chip`, `.role-tag`) | `--font` | `12–12.5px` | `500–600` | normal | `--ink-2` |
| Sheet & group heading (`.sheet h2`, `.group > .label`) | `--font` | `12px` | `600` | `0.09em`, `UPPERCASE` | `--ink-3` |
| Phone tab label / EQ band / Sim badge | `--font` / `--mono` | `11px` | `600–700` | `0.02–0.12em` | `--ink-2` / `#1b1300` |

**Coarse-pointer input rule:** On `@media (pointer: coarse)`, `.field` bumps to `16px` to prevent iOS Safari auto-zoom on focus. Keep placeholder copy short so it never clips at `16px`.

---

## 4. Component Stylings

Shared primitives live in `web/src/styles/base.css`; panel-specific layout lives next to each component (`ComponentName.css`).

### Glass Sheets (`.glass.sheet`)
- `border-radius: 28px` (`--r-sheet`), `padding: 20px` (`16px` on `≤480px`), `display: flex; flex-direction: column; gap: 16px` (`18px` in `System`).
- Header bar: `min-height: 32px`, uppercase `12px` `h2` pushed left (`margin-right: auto`), optional action/chip on the right.

### Buttons (`.btn`, always paired with `.press` for tactile feedback)
- **Geometry:** Pill (`border-radius: 999px`), `min-height: 36px`, `padding: 7px 16px`, `font-size: 13.5px`, `font-weight: 600`, `gap: 6px`, `16×16px` icon.
- **Variants:**
  - *Default:* `background: var(--tint-strong)` with top rim highlight and soft shadow.
  - *Primary (`.btn.primary`):* `background: var(--accent); color: var(--accent-ink)` with bright top highlight (`rgba(255,255,255,0.45)`) and accent-tinted shadow. Reserve for the primary CTA in a card or stage (`Power on`, `Play`, `Approve`).
  - *Danger (`.btn.danger`):* `color: var(--danger)` on neutral button surface (text-colored, not loud red fill).
  - *Quiet (`.btn.quiet`):* No fill, no shadow, `color: var(--ink-2)`.
  - *Icon (`.btn.icon`):* `36×36px` circle (`padding: 0`).
- **States:** `:active:not(:disabled)` scales to `(0.94, 0.955)` in `0.12s` and springs back in `0.45s var(--spring)`; `:disabled` uses `opacity: 0.45`.

### Segmented Control & Sliding Droplet (`.seg` + `.droplet`)
- Pill track (`background: var(--well)`, `padding: 4px`, inset shadow) with equal-width column grid buttons.
- Active selection sits on an animated `.droplet` pill (`0.55s cubic-bezier(0.25, 1.5, 0.4, 1)`), rendered as a luminous glass capsule in dark mode and `rgba(255,255,255,0.85)` in light mode.
- Optional notification dot (`.seg-badge`, `7×7px` `--accent` circle with glow) signals pending items (such as a browser waiting for approval on `System › Access`).

### Switches (`.switch`)
- `52×30px` pill track (`rgba(127,127,127,0.28)` off → `var(--accent)` when `aria-checked="true"`).
- Wide pill thumb (`32×24px`, white-to-`#eceff2` gradient) translating `14px` with `0.5s var(--spring)`.

### Range Sliders (`.slider` + `input[type="range"]`)
- `6px` rounded track filled to `--fill` with `var(--accent)`.
- `34×24px` pill thumb that scales to `(1.18, 1.1)` while dragging (`var(--spring)`).
- Label row above with `tabular-nums` mono `<output>` right-aligned.

### Text Fields (`.field`)
- `border-radius: 12px`, `padding: 9px 13px`, borderless recessed `background: var(--well)` with subtle inset shadow.

### Status Chips & Indicator Dots (`.chip`, `.dot`)
- Pill chip (`padding: 5px 11px`, `background: var(--well)`, `gap: 7px`) pairing a `7×7px` `.dot` with a 1–2 word label.
- Dot states carry an `8px` matching glow: `.ok`/`.live` (`--ok`), `.loading`/`.stopping` (`--warn`), `.error` (`--danger`), idle (`--ink-3`, no glow).

### Signature Domain Components
- **Top Bar & Power Button (`.top`, `.power`):** 3-column desktop grid (`brand | centered glass tabs | status + release + power`). `.power` is a `44×44px` circular glass button that fills with a radial `--accent` glow when `.running` and breathes (`opacity` pulse `1.6s`) while `.starting`/`.stopping`.
- **Robot Stage (`.stage`):** Live camera (`#05070a` frame) or interactive Three.js 3D Reachy standing directly on the wallpaper (never wrapped in glass). When offline, `#robot-3d.offline` transitions to `opacity: 0.35; filter: grayscale(1)` and reveals a normal-sized `.btn.primary.stage-power` at `bottom: 32px`.
- **Expression Carousel (`.carousel`):** Horizontal snap track (`72×72px` emoji tiles) passing beneath a centered `88×88px` `.lensring` (`border-radius: 26px`); dims to `grayscale(1) opacity(0.35)` when robot is off.
- **Manual Drive (`.pane.drive`):** Two tactile thumb pads (`max-width: 128px` on phone) with spring-return knobs (turning `--accent` when held), antenna sliders, and a `Hold` pin button next to `Home`.
- **Voice Panel (`.voice`):** One-line chips with icons (`Listen`, `Talk`, `Apps`). Active Talk chip fills horizontally with the live mic level (`--level`); mic picker appears whenever Talk is active (amber when silent).
- **Compact App Rows (`.app-row`):** `44px` gradient monogram tile (`border-radius: 13px`), expandable title/status copy, star pin toggle (`36×36px`), and Start/Stop/Open action buttons separated by `--hair` dividers.
- **Access & Waiting Card (`.acc-waiting`, `.role-tag`):** Recessed `22px` card inside `System › Access` listing waiting browsers (address + age, never the pairing code) with an inline mono code field (`17px/600`, `0.06em` tracking) and confirmation-gated `Approve as Admin` / `Make admin` / `Demote` / `Revoke` controls.

---

## 5. Layout Principles

- **Information Architecture (Layout B):**
  - Top-level navigation: **`Robot` | `Apps` | `System`** (centered glass pill on desktop; fixed floating bottom tab bar on phones `≤860px`).
  - `Robot` side sheet segments: **`Expressions` | `Drive` | `Controls` | `Voice`**.
  - `System` single-sheet sub-tabs: **`Settings` | `Access` | `Diagnostics`** (remembers active sub-tab on reload).
  - Non-admin profiles see the `System` tab **locked** (dimmed with a lock icon), never hidden.
- **Viewport Framing (`.app`):**
  - `min-height: 100dvh`, outer padding `max(16px, env(safe-area-inset-*))` with `32px` bottom breathing room on desktop (`110px + safe-area-inset-bottom` on phones to clear the floating tab bar).
  - Primary vertical gap between frame sections: `16px`.
- **Page Grids:**
  - *Robot view (`.view.robot`):* `grid-template-columns: minmax(0, 1fr) minmax(320px, 380px)`, `gap: 24px`. Stage stretches vertically (`min-height: min(640px, calc(100dvh - 120px))`).
  - *Apps & System views (`.view.list`):* Centered single column, `max-width: 760px`, `margin-inline: auto`.
  - *Diagnostics (`.diag-grid`):* Two equal columns (`gap: 20px 28px`) for board gauges beside audio EQ, with logs spanning full width (`.wide`, `grid-column: 1 / -1`) underneath.
- **Spacing Rhythm:** Built on a `4px` base unit (`6px` tight chip gaps, `8–12px` control/row gaps, `16px` sheet internal rhythm, `20px` sheet padding, `24px` stage-to-sidebar separation).
- **In-App App View:** Third-party HF Spaces app UIs open inside an overlay on mobile/tablet (dimmed dashboard on desktop, full-screen on phones) with controller chrome to close, pop out, or stop the app.
- **CSS Architecture:** Put shared tokens and controls in `web/src/styles/base.css`; keep each panel's layout in its own colocated `ComponentName.css` file.

---

## 6. Depth & Elevation

Liquid glass is a **strictly layered optical system**, not decorative blur:

1. **Layer 0 — Ambient Wallpaper (`.wall`, `z-index: 0`):** Fixed full-bleed studio gradient or dimmed photo behind everything.
2. **Layer 1 — Content Layer (`.stage`, camera feed, 3D robot, log output):** Sits directly on the wallpaper with **no glass effect**.
3. **Layer 2 — Control Layer (`.glass`, `z-index: 1`):** Sheets, top navigation pill, status pill, power button, and modal/stage notices:
   - Translucent vertical gradient (`--tint-strong` → `--tint`).
   - Backdrop frost: `backdrop-filter: blur(var(--frost, 18px)) saturate(185%) brightness(1.04)` (`--frost: 22px` for nav pills, `14px` for compact status pills).
   - Multi-layer elevation shadow: `0 18px 40px -12px var(--shadow)`, `0 2px 6px -2px var(--shadow)`, plus top and bottom inset highlights.
   - Directional specular rim (`.glass::before`): `1.2px` masked `conic-gradient` rotating with `--ang`.
   - Pointer-tracked glare (`.glass::after`): soft `radial-gradient` at `(--lx, --ly)` using `mix-blend-mode: plus-lighter`.
   - Desktop Chromium refraction (`.refract .glass[data-lens]` via `web/src/glass.ts`): per-element SVG `feDisplacementMap` with RGB channel dispersion (`1.0 / 0.92 / 0.84`).
4. **Layer 2b — Recessed Wells (`.well`):** Inner groupings inside a glass sheet (segmented tracks, input fields, waiting cards, control strips) recess inward with `background: var(--well)` and `inset 0 1px 2px` shadow. **Never nest `.glass` inside `.glass`.**
5. **Floating Overlays (`z-index` ladder):**
   - `20`: `.phone-tabs` fixed bottom navigation pill.
   - `50`: `.app-view` embedded app overlay.
   - `60`: `.control-toast` transient control-handoff pill (`top: 84px`, `max-width: calc(100% - 32px)`).

### Performance & Accessibility Degradation
- **Touch / coarse-pointer devices:** Lower `--frost` to `12px`, disable `.glass::after` glare, and skip SVG refraction (`lensCapable` is desktop Chromium only) to protect mobile compositor frame rates.
- **`prefers-reduced-transparency: reduce`:** Drops `backdrop-filter` entirely and switches `.glass` to opaque `color-mix(in srgb, var(--ground) 92%, var(--ink) 8%)`.
- **`prefers-reduced-motion: reduce`:** Collapses transitions to `0.01ms` and disables keyframe animations.

---

## 7. Do's and Don'ts

### Visual & Material
- **Do** reserve `.glass` for the control layer (sheets, tabs, floating buttons, toasts).
- **Don't** stack `.glass` on `.glass`, and **don't** apply `.glass` to content (camera video, 3D robot canvas, logs).
- **Do** use semantic CSS variables (`--ground`, `--ink`, `--accent`, `--ok`, `--warn`, `--danger`, `--well`, `--hair`) so both dark/light modes and all 5 accent schemes work automatically.
- **Don't** introduce arbitrary gradients, heavy borders, or saturated fills on secondary controls.
- **Do** preserve Reachy's intentionally uneven eyes in app icons and regenerate icons only via `tools/icons/build_icons.py`.

### Copy & UX (Direction B — State Over Sentences)
- **Do** show state through UI affordances: dim unavailable controls (`opacity: 0.35–0.45`, `grayscale(1)`), surface the single obvious next action (`Power on`), and pair icons with short names in the top bar.
- **Do** keep visible UI copy to **≤ 12 words** per element (enforced as a CI ratchet in `web/e2e/ux.spec.ts`).
- **Don't** write justification text, filler explanations, or status sentences in the dashboard; move explanations and quirks to `docs/manual.md` in the same PR.
- **Do** keep text where it protects people: warnings before the robot moves or goes limp, security notes, the `Take over` prompt, pairing commands, confirmation prompts for privilege/destructive changes, and real errors.
- **Do** say **"Robot service down"** (never "daemon") in user-facing text and API error messages.
- **Don't** use "Kid" / "Grown-up" labels for family profiles; use neutral profile names and the `Profiles` / `Access` terminology.
- **Do** show the `System` tab as locked (greyed with a lock icon) for non-admins rather than hiding it, and always enforce permissions server-side in `controller/`.
- **Don't** change the web manifest `display` away from `"browser"` (iOS standalone PWAs isolate cookies and lose device pairing).

### Design Process
- **Do** present mockups rendered in headless Chromium against the sim stack before implementing a new design direction; match approved mockups faithfully (icons, sizes, spacing).
- **Don't** hinder power users (multi-device control, quick keyboard/mouse actions) when simplifying flows for household members.

---

## 8. Responsive Behavior & Accessibility

### Breakpoints

| Breakpoint | Layout & Component Adaptations |
|---|---|
| `> 860px` (Desktop, tested at `1280×800`) | Centered top glass tabs; 2-column Robot stage + `320–380px` control sheet; 2-column Diagnostics grid; desktop Chromium lens refraction + pointer glare active. |
| `≤ 860px` (Tablet / Phone, tested on Pixel 7) | Top bar collapses to `auto minmax(0, 1fr)`; top tabs hide and fixed bottom `.phone-tabs` pill appears (`width: min(360px, calc(100% - 32px))`); Robot stacks `stage` above `side` (`46vh` stage, shrinking to `34dvh` in `.driving` mode so robot + thumb pads fit one screen); Drive pads cap at `128px` and antennas sit 2-up; Diagnostics stacks to 1 column. |
| `≤ 480px` (Compact Phone) | Brand wordmark hides when `.voice-badge` is active to keep who-is-listening + status legible; `.sheet.sys` padding drops to `16px`; Diagnostics gauges shrink `96px → 72px`; App tiles shrink `44px → 40px` and details flush-left; Access peer IP hides and multi-button action rows wrap cleanly below the label. |
| `≤ 420px` (Narrow Phone) | Status pill and Release button tighten (`padding: 7px 11px; font-size: 12px`). |

### Automated Accessibility & UX Guardrails
Enforced in `web/e2e/a11y.spec.ts` and `web/e2e/ux.spec.ts` (with ratchet baselines in `web/e2e/baselines/` that may only go down):
- **WCAG compliance:** `wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa`, `wcag22aa`, and `best-practice` via `axe-core` across every tab with the robot off and on.
- **Zero horizontal overflow:** `documentElement.scrollWidth <= clientWidth` on desktop and phone (hard failure).
- **Touch targets:** Hard minimum `24×24px` (WCAG 2.5.8 ratchet); target `36×36px` for standard buttons/stars and `44×44px` for primary circular actions like `.power`.
- **Keyboard focus:** Visible `2px solid var(--accent)` outline with `3px` offset (`:focus-visible`) on all interactive controls.
- **Reduced motion & transparency:** Full CSS media-query support in `base.css`.

---

## 9. Agent Prompt Guide

Use this quick checklist whenever designing or implementing UI in `reachy-bridge`:

1. **Before writing CSS or JSX:**
   - Reuse existing primitives from `web/src/styles/base.css`: `.glass.sheet`, `.btn` (`.primary` / `.danger` / `.quiet` / `.icon`) + `.press`, `.seg` + `.droplet`, `.switch`, `.slider`, `.field`, `.chip` + `.dot`, `.row`, `.group`, `.note`.
   - Never hardcode colors—use `--ground`, `--ink`, `--ink-2`, `--ink-3`, `--accent`, `--accent-ink`, `--ok`, `--warn`, `--danger`, `--well`, `--hair`, `--tint`, `--tint-strong`, `--rim`, `--rim-soft`.
   - Never nest `.glass` inside `.glass`; use `--well` for inner cards/tracks.
2. **Copy & interaction audit:**
   - Every visible label or note must be **≤ 12 words** unless it is a required safety warning, security note, pairing command, or real error.
   - Replace status sentences with visual state (dimmed controls, status chip dot, obvious next-action button).
   - Update `docs/manual.md` in the same PR whenever visible UI behavior changes.
3. **Verification before commit:**
   - Typecheck & unit tests: `cd web && bun run typecheck && bun test && bun run build`
   - Sim + Playwright E2E (desktop + phone + axe + UX ratchets):
     `uv run python tools/sim/run.py -- bash -c 'cd web && bunx playwright test'`
   - Visual review: capture screenshots in headless Chromium (scratch profile) against the sim stack in both dark and light modes, desktop (`1280×800`) and phone (`Pixel 7`).
4. **Cross-project reuse note:**
   - Keep this 9-section structure + YAML frontmatter (`version`, `colors`, `typography`, `radii`, `spacing`, `motion`, `breakpoints`) as the portable `DESIGN.md` template across repos so any coding agent can read a project's complete visual & UX contract in one file without needing shared chat memory.
