# Contributing

This covers how work is split up so several people or agent sessions can work at once.
The rest of the contributor guide (setup, style, review) comes with #136.

## Lanes

Open issues are grouped into lanes. A lane is one area of the repo that one person or
session works in at a time. Work in different lanes rarely touches the same lines, so it
can run in parallel; work inside one lane goes one PR at a time.

| Lane | Owns | Typical issues |
|---|---|---|
| Runtime | `runtime/`, `deploy/`, `docs/daemon-contract.md`, `tools/sim/` | platform layer, sim tests |
| Dashboard | `controller/`, `web/` | dashboard features, login, app UIs |
| Releases | `.github/`, release and updater tooling | CI, release builds, updater, SDK tracking |
| Docs | `README.md`, `CONTRIBUTING.md`, `LICENSE`, uninstaller | user docs, licensing, public release |
| TUI | `tui/` (new crate) | the terminal controller |

An issue that needs two lanes is split. Write the interface in the issue first, for example
the route, its request and its response. The lane that serves it lands first, and the
other lane builds against what landed.

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
- **Registries** (route setup in `controller/src/routes/mod.rs`, the paths in
  `web/src/api.ts`): add lines and don't reorder existing ones.
- **Single large files** (`controller/tests/api.rs`, `web/src/style.css`): split by area
  in #164 so that two dashboard PRs stop editing the same file. Until then, keep edits to
  them small and merge `main` often.
- **`README.md`**: owned by the Docs lane. Other lanes change only the lines about their
  own area.

Anything that runs on the robot still waits for hardware QA by the owner, one PR at a
time.
