# Milestone 4 Controls Proxy and Audio Visualizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add controller-owned Reachy control endpoints and a real-signal audio-level SSE stream.

**Architecture:** Extend `DaemonProxy` with narrow JSON helpers, expose them through two focused FastAPI routers, and register both before the catch-all daemon router. Derive visualizer levels exclusively from `doa.speech_detected` in full daemon state.

**Tech Stack:** Python 3.12, FastAPI, httpx, Pydantic, asyncio, pytest, pytest-asyncio

---

### Task 1: Lock proxy behavior with failing tests

**Files:**
- Modify: `tests/test_controls.py`
- Modify: `src/reachy_controller/daemon_proxy.py`

- [ ] Add async MockTransport tests for all ten helpers, asserting exact methods, paths, and JSON bodies.
- [ ] Add tests proving HTTP failures and invalid JSON return `None`.
- [ ] Run `uv run pytest tests/test_controls.py -q` and confirm failure because helpers do not exist.
- [ ] Implement the ten helpers with `get_json()` or `request()`, `raise_for_status()`, and `response.json()`.
- [ ] Re-run `uv run pytest tests/test_controls.py -q` and confirm proxy tests pass.

### Task 2: Add validated controls routes

**Files:**
- Modify: `tests/test_controls.py`
- Create: `src/reachy_controller/routes/controls.py`
- Modify: `src/reachy_controller/main.py`

- [ ] Add route tests for successful delegation, all offline 503 mappings, volume bounds, motor mode validation, and request body forwarding.
- [ ] Run the focused tests and confirm missing routes or catch-all behavior causes the expected failures.
- [ ] Add `VolumeSetRequest` and `PlaySoundRequest` Pydantic models with field descriptions.
- [ ] Add the ten `/api` endpoints, returning 400 for invalid volume/mode and 503 `Daemon unavailable` when helpers return `None`.
- [ ] Register the controls router before `daemon_router` and re-run focused tests.

### Task 3: Add the real-signal audio SSE stream

**Files:**
- Create: `tests/test_audio.py`
- Create: `src/reachy_controller/routes/audio.py`
- Modify: `src/reachy_controller/main.py`

- [ ] Add generator tests for active level `0.7`, idle level `0.0`, UTC timestamp presence, daemon-unavailable events, and cancellation handling.
- [ ] Run `uv run pytest tests/test_audio.py -q` and confirm failure because the module is absent.
- [ ] Implement a 20 Hz generator polling `get_full_state()`, emitting `audio-level` events, continuing after a `daemon-unavailable` event, and emitting 15-second keepalives.
- [ ] Add `/api/audio/levels-stream`, register its router before `daemon_router`, and re-run focused tests.

### Task 4: Verify the milestone

**Files:**
- Verify all files above.

- [ ] Run `uv run pytest -q`; require zero failures and at least the original 25 tests.
- [ ] Run `uv run ruff check src tests`; require zero errors.
- [ ] Start `uv run python -m reachy_controller.main` with the daemon down and curl motor, volume, and audio SSE endpoints.
- [ ] Send SIGINT and confirm a clean shutdown without tracebacks.
- [ ] Run `rg -n 'ReachyMini\(|/home/zeapsu' src`; require no matches.
- [ ] Review `git diff`/working-tree changes against every Milestone 4 requirement.
