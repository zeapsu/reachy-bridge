# Session Log

## [2026-06-24] reachy_controller — Liquid Glass control console (M1–M6a)
Built a new `apps/reachy_controller/` webapp: a FastAPI + vanilla-JS "Liquid Glass" control console
for the Reachy Mini (binds 127.0.0.1:7861, exposed tailnet-only via tailscale serve, never grabs the
robot lock). Landed M1 telemetry SSE, M2 app launcher, M3 Jetson stats, M4 controls + audio equalizer,
M5 camera-ownership coordination, and M6a the Liquid Glass UI port wired to live endpoints (65 tests
pass; each milestone verified against the running app — caught a 0.0.0.0 bind, a self-HTTP call, and a
wpctl parse bug that agent summaries hid). Also removed a stale public Tailscale funnel. Remaining:
viewport CSS distortion, M6b settings/docs, M5b live camera reader, and full daemon-up functional
verification. Main artifact: `apps/reachy_controller/` (+ `UI_POLISH_HANDOFF.md`, `mockup.html`).

## [2026-06-21] reachy_chat — XVF3800 audio config + mic-silence debug
Traced the startup "7 failed audio parameter(s)" warnings to a broken control-channel read-back on
this unit's XVF3800 firmware (writes succeed, verify-reads return retry/unknown status) → fixed by
applying `AUDIO_STARTUP_CONFIG` with `verify=False`. Separately, ad-hoc `PP_*` probe writes during
debugging corrupted the DSP state (mic frames flowed but RMS=0); recovered with
`audio_control_utils REBOOT --values 1` (verified mic RMS ~0.2 after a normal app start). Documented
both + the stop-current-app/Ctrl-C gotcha in AGENTS.md. Main artifact: `apps/reachy_chat/src/reachy_chat/console.py`.

## [2026-06-21] reachy_chat — camera vision context for OpenRouter backend
Added live-camera visual context to the OpenRouter chat path: a `looks_visual` intent heuristic
captures the current frame and injects it as a base64 `image_url` into the user turn (history stays
text-only), with default model switched to multimodal `minimax/minimax-m3`. 22/22 unit tests pass;
live daemon+API-key run still pending. Main artifact: `apps/reachy_chat/src/reachy_chat/openrouter_chat.py`.

## [2026-06-21] Reachy Mini Lite — stack, custom apps, hardware bring-up
Stood up the Reachy Mini SDK stack (uv, reachy-mini 1.8.3, headless MuJoCo 3.3.7), imported the
SDK examples + community apps, and built four custom apps (`reachy_dance`, `reachy_simon`,
`reachy_lookat`, `reachy_chat`). Brought the physical Lite online: fixed a reversed mic ribbon,
matched SDK↔daemon versions, and switched `reachy_chat` TTS from broken pyttsx3 to macOS `say`
(voice loop now works end-to-end on OpenRouter `deepseek/deepseek-v4-flash`). Added project memory
(`AGENTS.md`, canonical; `CLAUDE.md` symlink) and the `USER_GUIDE.html` field manual. Implementation
delegated to Codex via the `codex-implement-review-loop` agent. Main artifact: `~/reachy` (apps/ +
docs/USER_GUIDE.html). Camera exposure unsupported on macOS — deferred.
