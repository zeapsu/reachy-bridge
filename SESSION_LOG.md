# Session Log

## [2026-06-21] Reachy Mini Lite — stack, custom apps, hardware bring-up
Stood up the Reachy Mini SDK stack (uv, reachy-mini 1.8.3, headless MuJoCo 3.3.7), imported the
SDK examples + community apps, and built four custom apps (`reachy_dance`, `reachy_simon`,
`reachy_lookat`, `reachy_chat`). Brought the physical Lite online: fixed a reversed mic ribbon,
matched SDK↔daemon versions, and switched `reachy_chat` TTS from broken pyttsx3 to macOS `say`
(voice loop now works end-to-end on OpenRouter `deepseek/deepseek-v4-flash`). Added project memory
(`AGENTS.md`, canonical; `CLAUDE.md` symlink) and the `USER_GUIDE.html` field manual. Implementation
delegated to Codex via the `codex-implement-review-loop` agent. Main artifact: `~/reachy` (apps/ +
docs/USER_GUIDE.html). Camera exposure unsupported on macOS — deferred.
