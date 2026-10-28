# Contributing

This covers how work is split up so several people or agent sessions can work at once.
The rest of the contributor guide (setup, style, review) comes with #136.

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
contributor's approval. Merging, hardware access and anything else that needs your
go-ahead still comes from you.

## Claiming work

1. Branch from a fresh `main`, in your own worktree (`git worktree add`), never in a
   checkout another session is using.
2. Open a draft PR right away that names the lane and the issues. Other sessions can then
   see what's taken.
3. Before asking for review, merge the current `main` into your branch and rerun the
   checks.
4. The owner merges PRs in any order. PRs still open merge `main` again.

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

Anything that runs on the robot still waits for hardware QA by the owner, one PR at a
time.
