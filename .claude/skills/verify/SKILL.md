---
name: verify
description: Smoke-test a reachy-bridge change end to end against the sim stack (sim daemon, release controller, headless Chromium) before committing. Use before every commit that changes code, and before asking for hardware QA.
---

# Verify a change against the sim stack

Run the change itself, not just its unit tests: build what changed, start the sim stack,
and drive the part you touched. It never touches the robot host, a real robot or
`tailscale serve`; everything runs on loopback in this checkout.

`tools/precheck.sh` covers formatters, linters, type checks and unit tests; this skill
covers the running product. CI runs both again, but don't push to find out.

## 1. Build what changed

Find the areas: `git diff --name-only origin/main...; git status --short`.

- `web/` changed: `(cd web && bun install --frozen-lockfile && bun run build)`. The
  controller embeds `web/dist`, so rebuild the controller after it too.
- `controller/` or `web/` changed:
  `(cd controller && cargo build --release --locked)`.
- `uv.lock` or `pyproject.toml` changed: `uv sync`.

Use `mise exec --` in front if your default toolchains differ from `mise.toml`.

## 2. Run the sim smoke test

```bash
uv run python tools/sim/run.py
```

About 40 s. It starts its own sim daemon and controller on free ports and runs
`tools/sim/smoke.py` (power, apps, expressions, the #146 daemon-contract checks). On a
failure it keeps and prints its work dir; read the controller and daemon logs there
before changing anything.

## 3. Drive the part you touched

Pick what matches the diff. Run the specific test, not the whole suite, while iterating.

- **Dashboard (`web/src/`) or a controller route the dashboard uses:** the matching browser
  test, at both sizes:
  ```bash
  uv run python tools/sim/run.py -- bash -c 'cd web && bunx playwright test e2e/dashboard.spec.ts -g "<test name>"'
  ```
  `bunx playwright install chromium` once first (in a cloud session, set
  `PLAYWRIGHT_BROWSERS_PATH` to the preinstalled browsers instead). No test covers the
  change yet? Add one to `web/e2e/` (one per thing a person does) rather than checking by
  hand only. For a look at a new view, take a screenshot with a scratch script against
  `$SIM_CONTROLLER` (run it through `run.py -- ...`), headless Chromium, a fresh profile,
  desktop 1280×800 and phone (Pixel 7) sizes, light and dark. Never screenshot the owner's
  browser.
- **Voice or audio routing:** `run.py --audio -- bash -c 'cd web && bunx playwright test --project voice'`
  (needs gstreamer1.0-plugins-bad and gstreamer1.0-nice). CI can't test the ALSA side;
  the `hardware-qa` status stays pending until the owner's device test.
- **Runtime (`runtime/`):** the sim smoke test above, plus `uv run reachy-bridge-runtime --check`
  when `upstream.py`, `uv.lock` or the `reachy-mini` floor changed.
- **Controller ↔ daemon behaviour:** a check in `tools/sim/smoke.py`, tagged with its issue.
  What the controller may assume is in `docs/daemon-contract.md`.
- **`deploy/` or the `reachy-bridge` command:** `bash tools/test_cli.sh` and
  `bash tools/test_uninstall.sh`.
- **The review gate, catch-up check or hardware QA gate:** `bash tools/test_review_gate.sh`,
  `bash tools/test_catchup_check.sh`, `bash tools/test_hardware_qa_gate.sh`.
- **Docs only:** nothing to run; say so in the stamp.

## 4. Stamp the commit

End the commit body with one line saying how the change was verified, so unverified
commits can be found later:

```
Verified: sim smoke, e2e dashboard "an app starts" (desktop, phone)
Verified: none (docs only)
```

## When it fails

- Fix the cause. Never skip a test, add a sleep or a retry, or raise a timeout to get it
  green. A test you can't make reliable gets deleted, with the reason in the commit.
- After three fix rounds that leave the same check failing, stop and ask the owner, with
  what you tried and the failing output.
- If the sim itself misbehaves (it doesn't come up, a port is taken), say so; don't work
  round it in the change.

Make this skill faster or more reliable when you find a way: it is the smoke-test harness
every change goes through.
