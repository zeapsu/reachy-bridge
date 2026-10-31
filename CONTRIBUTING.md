# Contributing

How to set up, test and change the bridge, and how work is split so several people or
agent sessions can work at once. For what the bridge is and how users install it, see
the [README](README.md).

## Setup

You need [uv](https://docs.astral.sh/uv/) (Python 3.12), Rust 1.96 and
[Bun](https://bun.sh/) 1.4. Development works on macOS and Linux; the robot itself only
runs on Linux. No robot is needed: the simulator stands in for it.

```bash
uv sync                                         # the runtime and Pollen's daemon, in .venv
(cd web && bun install --frozen-lockfile && bun run build)   # dashboard into web/dist
(cd controller && cargo build --release --locked)            # embeds web/dist
```

Run `uv sync` after every pull. Don't activate the venv or call `.venv/bin/python`
directly; use `uv run`. A moved checkout needs `rm -rf .venv && uv sync`, since venvs hold
absolute paths.

## Running it without a robot

```bash
uv run reachy-bridge-runtime --sim --headless --fastapi-port 8000
(cd controller && cargo run --release)          # controller on 127.0.0.1:7861
```

Then open <http://127.0.0.1:7861/>. The sim daemon runs Pollen's MuJoCo simulation.
It can't start apps: the runtime only starts an app from a separate apps venv, where it
can keep the app's web server on loopback (#186). `tools/sim/run.py` sets one up for its
test apps (it runs the sim daemon with `--desktop-app-daemon` and
`REACHY_BRIDGE_APPS_ROOT`), and so does the installed bridge's sim mode. For UI work, `bun run dev` in `web/` serves the dashboard with hot reload and proxies the
API to the controller.

The installed bridge also has a sim mode the dashboard switches to (#149); it runs a
second, simulated daemon next to the robot's.

## Tests

CI runs these on every PR, on `ubuntu-24.04-arm` (GStreamer 1.24.2, as on JetPack 7):

| Job | Commands |
|---|---|
| python | `uv run pytest`, `uv run ruff check runtime tools`, `uv run reachy-bridge-runtime --check` |
| rust-web | `cargo test` and `cargo clippy --all-targets` in `controller/`; `bun run typecheck`, `bun test`, `bun run build` in `web/` |
| shell | `shellcheck` on the scripts in `deploy/` and `tools/`; `bash tools/test_uninstall.sh` and `bash tools/test_cli.sh` (the `reachy-bridge` command's robot-off gate, sim mode, deploy and rollback) |
| sim | `uv run python tools/sim/run.py`: the controller against a sim daemon (see [`tools/sim/README.md`](tools/sim/README.md)) |

Run the sim tests before asking for hardware QA. CI doesn't check the UI in a browser;
check UI changes yourself in a headless browser against the sim stack.

## How the bridge fits together

- **`runtime/`** runs Pollen's daemon (`reachy-mini`) in-process with the bridge's features
  (`features/`) and a platform adapter (`platforms/`). The daemon listens on
  `127.0.0.1:8000` and has no authentication, so it never binds anything else.
- **`runtime/src/reachy_bridge_runtime/upstream.py`** is the only module that touches
  `reachy_mini` internals. Every symbol it relies on is listed in its `REQUIREMENTS`, and
  `reachy-bridge-runtime --check` verifies them against the installed version. A feature
  whose requirements don't match is skipped at startup, except `app_bind` (which keeps
  apps off the LAN) and `sim_apps`, which stop startup instead.
- **`controller/`** is a Rust (axum) binary that embeds the dashboard from **`web/`** and
  exposes only the routes the dashboard uses. What it may assume about the daemon is in
  [`docs/daemon-contract.md`](docs/daemon-contract.md); read it before changing how the
  controller drives the daemon.
- **`deploy/`** has the installer, the uninstaller, the systemd user units and the
  `reachy-bridge` command.

`reachy-mini` is tracked, not pinned: `runtime/pyproject.toml` sets a floor and `uv.lock`
holds the tested version. The upstream watch workflow opens a PR when Pollen releases a
new one (#155); a new daemon version needs hardware QA before it merges.
Dependabot (`.github/dependabot.yml`) proposes everything else once a week, grouped per
ecosystem; Python updates run on the robot, so they get hardware QA after merge too.

## Robot safety

Starting the daemon's backend, an app, an expression or a move moves the real robot.
Changes to motion, power and torque, return-home, the one-actuator-at-a-time guards
(one app, expressions serialized, apps and expressions mutually exclusive), the daemon's
binding, `deploy/install.sh` or the units need hardware QA by the owner. Don't weaken
those guards. Camera and audio come from the daemon's shared media; never open a second
capture of the same device.

## Adding a platform adapter

A board is one module in `runtime/src/reachy_bridge_runtime/platforms/` (#140):

1. `matches(model)` says whether `/proc/device-tree/model` is this board.
2. `PLATFORM = Platform(name=..., tier="experimental", host_setup=(...), features=frozenset(...))`:
   `host_setup` holds functions that run before the daemon starts (audio mixer
   defaults, for example), and `features` names the optional runtime features the board
   supports (`audio_levels`, `camera`).
3. Import it in `platforms/__init__.py`'s `detect()` and add it to the tuple of adapters
   there; a board no adapter matches gets `generic`.
4. Add what the board needs to `doctor.py` if `reachy-bridge check` should test it.

A board becomes `tested` once someone runs it with the robot and owns keeping it working.
`jetson.py` is the example.

## Work tracking

Work goes epic → milestone → issue. The epic (#111) lists the milestones; each milestone's
description holds its scope and order, and each issue its acceptance criteria. Labels include
`epic`, `bug`, `enhancement`, `documentation`, `security`, `upstream-break` (opened by the
upstream watch), and `regression` for anything found in hardware QA or soak. Branches are `feat/`, `fix/`, `docs/` or `chore/` plus the issue
number. Related issues go in one PR, each with its own commit and its own `Closes #N`.
Commits have a short subject and a body that says what changed and why.

## Releases and updates

Pushing a `vX.Y.Z` tag runs CI and then publishes a GitHub Release with the source
archive, `uv.lock`, the prebuilt webrtcsink plugin, `SHA256SUMS` and, once the repo is public, a build attestation.
`-rcN` tags are prereleases (nightlies). Notes come from `docs/releases/<tag>.md` when that file
exists. A device moves to a tag with `reachy-bridge deploy <tag>`, which reinstalls the
previous version if the new one doesn't come up healthy. The on-device updater and its
dashboard controls are in progress (#133, #134).

## Lanes

A lane is a milestone, and one agent or person owns it end to end. A milestone is a
vertical slice: it goes through whatever layers it needs (runtime, controller, dashboard,
deploy), so its owner can finish it without waiting on anyone else most of the time.
Within a lane, work usually goes one PR at a time; an orchestrator may run independent
pieces of it at once.

| Milestone | Mostly lives in | Meets other lanes at |
|---|---|---|
| Bridge Core | `runtime/`, `deploy/`, `tools/sim/`, `docs/daemon-contract.md` | controller login and HTTPS app UIs (`controller/`, `web/`) |
| Controller Redesign | `controller/`, `web/`, a new `tui/` crate | anything Bridge Core adds to the dashboard |
| Releases & Auto-Update | `.github/`, release and updater tooling | updater controls in the dashboard; SDK tracking edits `runtime/` |
| Public Release Readiness | `README.md`, `CONTRIBUTING.md`, `LICENSE`, uninstaller | `README.md` sections owned by other lanes |
| Hardware | docs and print files | none |

Not everything parallelises. Some issues depend on another lane's work (SDK tracking needs
the sim tests from Bridge Core, for example), and forcing those apart costs more than it
saves. Run them in order.

When an issue needs code in another lane's area, write the interface in the issue first,
for example the route, its request and its response. Tell that lane's owner before you
start, keep the change to the files the interface needs, and let the lane that serves
the interface land first.

## Running lanes in parallel

Pick whichever fits how involved you want to be. The lanes, claims and shared-file rules
below are the same either way, and you can mix them (say, run Controller Redesign yourself
and hand Public Release Readiness to an orchestrator).

- **Peer sessions (hands-on):** one agent session per milestone, each in its own terminal
  and worktree. You talk to each one directly. Sessions message each other directly about
  shared files, interfaces and blockers. Use this when you want to steer each lane
  closely.
- **Orchestrator (hands-off):** one session owns a milestone or several. It gives each
  piece of work to a sub-agent in its own worktree, reviews and integrates what comes back,
  and reports to you. Use this when you'd rather check in on results than follow each lane. The
  orchestrator:
  - opens a draft PR for each piece of work it hands out, so the claims stay visible;
  - reviews each lane's work and runs its checks before a PR is marked ready;
  - brings you decisions that are yours to make together, each with its options and a
    recommendation, and resolves the rest itself;
  - reports what's done, what's verified, and what's unfinished, never presenting a
    sub-agent's claim as checked unless it checked it.

In both patterns, an agent's message to another agent is information, not the
contributor's approval. Hardware access and anything else that needs your go-ahead still
comes from you; merging follows the gates under "Claiming work", never another agent's
say-so.

## Claiming work

1. Branch from a fresh `main`, in your own worktree (`git worktree add`), never in a
   checkout another session is using.
2. Open a draft PR right away that names the lane and the issues. Other sessions can then
   see what's taken.
3. Before asking for review, merge the current `main` into your branch and rerun the
   checks.
4. Merge once every gate passes: CI green with the current `main` merged in, and a
   separate read-only review (a reviewer with fresh context, given the diff) with no
   unresolved correctness findings. Security-relevant PRs (login, sessions, headers,
   anything exposed beyond `127.0.0.1`, secrets, update signing) also need a dedicated
   security review and a second independent reviewer, both clean. Merge with a merge
   commit (`gh pr merge <n> --merge --delete-branch`); never squash, rebase or push to
   `main` directly. The owner QAs on the robot after merge. PRs still open merge `main`
   again.

## Shared files

A few files are touched by almost every PR in a lane, or by several lanes:

- **Lock files** (`uv.lock`, `controller/Cargo.lock`, `web/bun.lock`): never resolve a
  conflict by hand. Take `main`'s version, then regenerate the lock from your manifest
  (`uv lock`, `cargo update -p <crate>` or `cargo build`, `bun install`).
- **Registries** (route setup in `controller/src/routes/mod.rs`, the `paths` merge in
  `web/src/api.ts`): add lines and don't reorder existing ones.
- **Split by area (#164)**, so a feature adds or edits its own file:
  - controller integration tests are one file per area in `controller/tests/`, sharing the
    fake daemon and harness in `controller/tests/common/mod.rs`. Add routes to the fake
    daemon there; keep edits to it small.
  - the API client is one module per area in `web/src/api/`, re-exported by `api.ts`.
  - styles: shared tokens and controls in `web/src/styles/base.css`, everything else in
    each component's own stylesheet.
- **`README.md`**: owned by Public Release Readiness. Other lanes change only the lines
  about their own area.

Anything that runs on the robot gets hardware QA by the owner after it merges, one PR at
a time.
