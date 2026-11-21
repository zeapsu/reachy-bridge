# Contributing

How to set up, test and change the bridge, and how work is split so several people or
agent sessions can work at once. For what the bridge is and how users install it, see
the [README](README.md). Coding agents also follow [`AGENTS.md`](AGENTS.md).

## Setup

You need [uv](https://docs.astral.sh/uv/) (Python 3.12), Rust and [Bun](https://bun.sh/),
at the versions in [`mise.toml`](mise.toml), which CI reads too; with
[mise](https://mise.jdx.dev/), `mise install` gets them all. Development works on macOS and
Linux; the robot itself only runs on Linux. No robot is needed: the simulator stands in
for it.

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
| python | `uv run pytest`, `uv run ruff check runtime tools`, `uv run ruff format --check`, `uv run reachy-bridge-runtime --check` |
| rust-web | `cargo fmt --check`, `cargo test` and `cargo clippy --locked --all-targets -- -D warnings` in `controller/`; `bun install --frozen-lockfile`, `bun run typecheck`, `bun test`, `bun run build` in `web/` |
| shell | `shellcheck` on the scripts in `deploy/` and `tools/`; `bash tools/test_uninstall.sh`, `bash tools/test_cli.sh` (the `reachy-bridge` command's robot-off gate, sim mode, deploy and rollback) and `bash tools/test_review_gate.sh` (the review check), `bash tools/test_catchup_check.sh` and `bash tools/test_precheck.sh` |
| sim | `uv run python tools/sim/run.py`: the controller against a sim daemon (see [`tools/sim/README.md`](tools/sim/README.md)) |
| browser | `uv run python tools/sim/run.py --audio -- bash -c 'cd web && bunx playwright test'`: the dashboard in Chromium against the sim stack, at desktop and phone sizes, and the voice pane's audio routing over WebRTC with a test tone per source (`--audio`; needs gstreamer1.0-plugins-bad and gstreamer1.0-nice, and an address on the tailnet or in `AUDIO_GATEWAY_NETS`) (not required yet) |
| install | `deploy/install.sh` on a fresh runner with a systemd user manager, checked by `tools/check_install.sh`; run twice, then `reachy-bridge uninstall --yes` and `check_install.sh --gone` (not required yet) |

Before you push, `bash tools/precheck.sh` runs the format, lint, type and unit checks for
the areas your branch changed (`--all` for every area); Claude Code sessions run it from a
hook (AGENTS.md, "Verification"). Before each commit, check the change running against the
sim stack: `.claude/skills/verify/SKILL.md` says how, area by area. Reviewers work from
[`REVIEW.md`](REVIEW.md) and the items of
[`docs/reliability-checklist.md`](docs/reliability-checklist.md) that match the diff
(`git show origin/main:tools/checklist.py | python3 -I -`), both read from `main`.

Run the sim tests before asking for hardware QA. The python and rust-web jobs report line
coverage in their job summaries (pytest-cov, `bun test --coverage`, cargo-llvm-cov); it's
recorded, not checked.

The browser tests (`web/e2e/`) cover what a person does with the dashboard, plus
accessibility (axe-core) and layout/copy checks: sideways scrolling fails, and small tap
targets and long text are counted. Those counts and axe's are kept in
`web/e2e/baselines/` and may only go down; after fixing some, rerun with
`UPDATE_BASELINES=1` and commit the new baselines. The job summary shows each count and
the load time, and the HTML report is in the `browser-report` artifact. Locally, run
`bunx playwright install chromium` once in `web/` first.

CI has no sound card, so it can't run the robot's ALSA side of the audio (the speaker
and mic tee, the mic mixer, the speaker mute). A PR that changes audio code gets the
`audio: needs device test` label. To clear it, deploy its latest commit to a robot and,
with the robot off and nobody listening or talking, run `python3 tools/check_audio.py`
there (it plays test tones through the tee, and two quiet beeps on the speaker); when it
passes, label the PR `audio: device-tested`. A new push puts the first label back.

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
ecosystem. Patch and minor updates merge on their own once CI passes, and a workflow keeps
them up to date with `main` until they do; a major bump, or one that changes
`.github/`, needs a review first (see "Claiming work"). Python updates run on the robot,
so they get hardware QA after merge too.

## Robot safety

Starting the daemon's backend, an app, an expression or a move moves the real robot.
Changes to motion, power and torque, return-home, the one-actuator-at-a-time guards
(one app, expressions serialized, apps and expressions mutually exclusive), the daemon's
binding, `deploy/install.sh` or the units need hardware QA by the owner. A PR that
changes robot-side code gets the `needs hardware QA` label from
`.github/workflows/hardware-qa-label.yml` (#306), by path; it stays on after merge until
the owner has QA'd the change on the robot and removes it. Don't weaken those guards.
Camera and audio come from the daemon's shared media; never open a second capture of the
same device.

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
upstream watch), `needs hardware QA` (set by path, removed by the owner after QA on the
robot), and `regression` for anything found in hardware QA or soak. Branches are `feat/`, `fix/`, `docs/` or `chore/` plus the issue
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
4. Merge only once every gate passes: CI green with the current `main` merged in, and a
   separate read-only review (a reviewer with fresh context, given the diff) with no
   unresolved Important findings (as [`REVIEW.md`](REVIEW.md) defines them).
   Security-relevant PRs (the list in REVIEW.md, "Security-relevant PRs") also need a
   dedicated security review and a second independent reviewer, both clean.
5. Record each clean review as a PR comment of its own (not a review body) that holds
   only the marker and the full 40-digit sha of the head commit the reviewer was
   given: `Review: clean @<sha>`, and for a security-relevant PR also
   `Security review: clean @<sha>` and `Second review: clean @<sha>`, each in its own
   comment. The only other text a marker comment may hold is the footer Claude Code's
   cloud sessions sign every comment with (`---` and `_Generated by [Claude Code](https://claude.ai/code)_`,
   below the marker). Don't edit a marker comment; post a new one. The required `review` check
   (`tools/review_gate.sh`, #302) stays pending until the repo owner's account has
   posted them for the PR's current head, so a new push, including a merge of `main`,
   needs fresh markers (except after a clean catch-up with `main`, checked with
   `bash tools/catchup_check.sh <n>` from a checkout of current `main`, step 6); so does a
   PR whose base branch changed, since markers from before the change reviewed another
   diff; and a head commit shared by two open PRs stays pending. Post markers only from the owner's
   account: check that `gh api user -q .login` matches
   `gh repo view --json owner -q .owner.login` first. Get the
   head with `gh pr view <n> --json headRefOid -q .headRefOid`, and wait for the check
   with `gh pr checks <n> --watch` before merging, and confirm the `review` row passed; when it stays pending, its
   description (`gh pr checks <n> --json name,state,description`) and its run's log say
   what's missing. The gate treats a PR as security-relevant when it has the `security`
   or `auth` label or changes a file in the script's `SECURITY_PATHS`: login, sessions, pairing and setup and the guards and
   headers that apply them, the app proxy, bind addresses and what's exposed, request
   signing, app binding, the host checks, the power, apps and logs routes, the upstream
   daemon shim and the apps venv sync, secrets, the installer and udev rules, dependency manifests and lock files,
   build scripts and toolchains, everything under `.github/`, `.gitmodules`, the agent
   rules and every coding agent's config (`CLAUDE.local.md`, `GEMINI.md`, `.cursor/`,
   `.codex/` and the like), and the gate itself. Label a PR
   `security` when it is security-relevant by REVIEW.md's list but touches none of
   those files. Dependabot PRs whose commits are all Dependabot's (or the branch
   updater's merge of `main`) pass without markers when every update is patch or minor,
   nothing under `.github/` changes and there's no `security`/`auth` label; a major,
   unclassified or workflow update needs one `Review` marker (the `security` or `auth`
   label asks for the full set). Bring those up to
   date before their reviews with `@dependabot rebase` (or `@dependabot recreate` once the
   branch updater has pushed to them), not "Update branch", which adds a commit of yours;
   any other commit on a Dependabot PR needs the full set of markers.
6. A clean catch-up with `main` keeps the earlier reviews. `bash tools/catchup_check.sh <n>`,
   run from a checkout of current `main` (never the PR's branch), checks that the PR's head N
   only merges a commit of `main` into its first parent R, the reviewed commit: one merge
   base on `main`'s first-parent history, no conflicts or hand edits, no file changed on both
   sides, N's change against `main` exactly R's reviewed change, no change of base branch,
   and, when R has a `Security review` marker, no `SECURITY_PATHS` file from `main`. It lists
   R's markers; `--post` re-posts exactly those for N. Nothing happens until it is run. When
   it fails, the reviews run again on N. CI tests the merged commit either way. The check
   relies on `main` taking only merge and squash merges (the ruleset): no fast-forwards
   and no merge queue.
7. Merge with a merge commit (`gh pr merge <n> --merge --delete-branch`); never squash, rebase or push to
   `main` directly. Only Dependabot's patch and minor PRs squash, which their auto-merge does. The owner QAs on the robot after merge. PRs still open merge `main`
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
