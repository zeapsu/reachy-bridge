# Updating reachy-mini

How a coding agent (or a person) takes a new `reachy-mini` release from the
upstream watch to a mergeable PR (#155). The daily
`.github/workflows/upstream-watch.yml` does the detecting and testing. This
playbook covers what comes after: reviewing a green bump, and fixing a red one
inside a small, fixed surface or handing it back with a diagnosis.

## What the watch leaves you

- **Green:** a PR "Lock reachy-mini X" from `chore/reachy-mini-X` that changes
  only `uv.lock`. All of CI passed against it.
- **Red:** an issue labelled `upstream-break` titled "reachy-mini X: breaks
  the bridge", listing the failed jobs, with the new lock pushed to
  `chore/reachy-mini-X`. A lock that doesn't resolve at all is also an
  `upstream-break` issue, with uv's output.

Either way, merging stays human. A new daemon drives the motors, so every
bump gets hardware QA on the robot before the Jetson keeps it.

## The oracle

The checks decide, not your reading of the code:

1. `uv sync --frozen`, then `uv run reachy-bridge-runtime --check`: every
   upstream symbol in `upstream.py`'s `REQUIREMENTS` still exists and has the
   shape the runtime expects.
2. `uv run pytest` and `uv run ruff check runtime tools`.
3. The sim contract tests: `uv run python tools/sim/run.py` (see
   `tools/sim/README.md`; needs the web build and a release controller).
4. `cargo test` and `bun test` only if you touched anything they cover (you
   shouldn't have; see the allowed paths).

## Steps

1. Check out `chore/reachy-mini-X` and run the oracle. Note exactly which
   checks fail and how.
2. Read what changed upstream: the
   [release notes](https://github.com/pollen-robotics/reachy_mini/releases)
   and the source diff between the locked and new wheels. Download both
   wheels from PyPI into an empty directory, unzip them and run
   `diff -ru old/reachy_mini new/reachy_mini --exclude='*.pyc'`. Don't run
   anything from them.
3. Re-check `docs/daemon-contract.md` section by section against the new
   source, including for a green bump: the tests don't cover every sentence.
   Look for new outbound network calls or telemetry too, and say what you
   found in the PR.
4. Classify each failure:
   - **Seam fix:** an upstream symbol was renamed or moved, or a signature
     changed, and the same hook still exists. Fix it in `upstream.py`.
   - **Behaviour change:** the daemon now does something different from what
     the contract says (power, motor modes, idle reset, app lifecycle,
     routes). Update the contract. If the controller or a runtime feature has
     to change to follow, that is not a seam fix: stop (see below).
5. Iterate until the oracle passes or a stopping rule applies.
6. Green: push to the branch, update the contract's version line and any
   changed sections, and open or update the PR with what changed upstream,
   what you changed and why, and "needs hardware QA".

## Allowed paths

A fix may change only:

- `runtime/src/reachy_bridge_runtime/upstream.py` (the seam)
- `uv.lock`, and the `reachy-mini` floor in `runtime/pyproject.toml`
- `docs/daemon-contract.md`

Everything else is out of bounds for an upstream fix: the controller, the
web UI, runtime features and platform adapters, the sim patches in
`tools/sim/`, and every test. Changing a test to make it pass is not a fix.

## Stopping rules

Stop, and comment on the `upstream-break` issue instead of opening a PR, when:

- the fix needs a change outside the allowed paths;
- a test would have to change;
- the diff passes about 200 changed lines (not counting `uv.lock`);
- the change touches motion, torque, power or return-home behaviour in a way
  the contract doesn't already describe;
- three rounds of fixes haven't made the oracle pass.

The comment says which checks fail, what changed upstream that explains it,
your proposed change as a patch in a code block, and which rule stopped you.
A person decides from there.

## The same pattern elsewhere

Board support follows the same shape: a contract, one adapter seam
(`runtime/src/reachy_bridge_runtime/platforms/`), allowed paths, stopping
rules, and the checks as the oracle. A setup helper for a new board would use
this playbook's structure with that seam.
