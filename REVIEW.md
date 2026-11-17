# REVIEW.md — what a reviewer looks for

The focus for the read-only reviewers in AGENTS.md's merge gates. Reviewers read this
file and the reliability checklist **from `main`**, never from the PR's branch, so a
change can't edit the checks that judge it:

```bash
git fetch origin main
git show origin/main:REVIEW.md
# main's checklist script, which reads main's checklist, run on this checkout's diff:
git show origin/main:tools/checklist.py | python3 -I -
```

A PR that changes this file, `docs/reliability-checklist.md` or `tools/checklist.py` is
security-relevant (they are in `SECURITY_PATHS`), and is still judged by the version on
`main`.

## How to review

- You get the diff and the head commit's sha, not the author's conclusions. Review that
  commit; a push during the review makes the result stale.
- Run the checklist items that match the diff (main's `tools/checklist.py` prints them,
  usually a handful). For each, say whether its check holds.
- Tag each finding **Important** (a bug, a broken rule, a missing check from the
  checklist), **Nit** (style, wording, small cleanups) or **Pre-existing** (true before this
  PR). Only Important findings block a merge; Pre-existing ones are not held against the
  PR, but say if one should become an issue.
- Give each finding a file and line, a concrete failure (inputs or state, then the wrong
  result) and why it matters. A finding you can't make concrete is a question, not a
  finding.
- On a later round, read the earlier rounds' findings and answers first and check their
  fixes, instead of starting from scratch.

## Focus by area

**Controller (`controller/`)**
- Every route sits behind the login and the guards it needs; there is no catch-all daemon
  proxy. Only routes the dashboard uses exist.
- One actuating surface at a time: one app, expressions serialized, apps and expressions
  mutually blocking, graceful return-home after motor re-enable. The control lock gives one
  driver at a time (#245, #251). None of these guards may weaken.
- Calls to the daemon follow `docs/daemon-contract.md` (power, motor modes, idle reset,
  app lifecycle).
- App UIs go only through the logged-in proxy on :7863 (#186, #213).

**Runtime (`runtime/`)**
- Only `upstream.py` touches `reachy_mini` internals, and every symbol it uses is in its
  `REQUIREMENTS`.
- The daemon binds `127.0.0.1` only; telemetry stays off (`REACHY_MINI_TELEMETRY=0`).
- Camera and audio come from daemon-owned shared media; no second capture of a device.
- Subprocess output is drained continuously into bounded storage; no unread `PIPE`.

**Dashboard (`web/`)**
- State is shown through the UI (dimmed, greyed, an obvious next action), not sentences.
  No justification text.
- Text that protects people stays: warnings before the robot moves or goes limp, security
  notes, the Take over line, pairing commands, real errors.
- `docs/manual.md` is updated when the UI changes.

**Deploy and the `reachy-bridge` command (`deploy/`)**
- Deploy, install and restart refuse unless the robot is off; nothing adds or uses
  `--force` to get round that.
- Units stop the robot with SIGINT so it sleeps and releases torque.
- No private values (hosts, addresses, paths) in tracked files.

**Tests**
- A behaviour change comes with a test at the level where it shows: a browser test for
  something a person does in the dashboard, a sim check for controller ↔ daemon
  behaviour, a unit test for logic.
- No test is skipped, slowed with a sleep or retried to pass. A flaky test is fixed, or
  deleted with the reason in the commit.

## Security-relevant PRs

These also need a dedicated security review and a second independent review (AGENTS.md,
merge gates): login, sessions, pairing, headers, CORS/framing, the app proxy, anything
exposed beyond `127.0.0.1`, secrets, update signing, workflows, systemd units, the
`reachy-bridge` command, the installer and uninstaller, audio request signing, and the
agent rules and review files (`AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, this file, the
checklist). `SECURITY_PATHS` in `tools/review_gate.sh` is the authoritative path list; a PR
that is security-relevant by this list but touches none of those paths gets the
`security` label.

The security reviewer looks for: authentication or guard bypasses, anything newly
reachable from beyond loopback, secrets in logs or responses, injection into shell
commands or paths, weakened gates (review, robot-off, actuating surface), and changes
that let a PR edit what judges it.
