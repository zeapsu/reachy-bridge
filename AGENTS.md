# AGENTS.md — Reachy Mini Lite workspace

> Canonical agent guide. `CLAUDE.md` is a symlink to this file, so both entry points expose the same project instructions.

## Project shape
This is a simulation-first Reachy Mini Lite workspace. The same application code targets simulation
or the physical robot by connecting to the corresponding daemon.

Custom apps live under `apps/`. The upstream SDK clone (`reachy_mini/`) and reference app
(`reachy_mini_conversation_app/`) are gitignored. `reachy_controller` is the on-device web console.

## Machine topology and remote operation
- The normal development machine is an Apple Silicon Mac with no Reachy hardware. Use it for
  editing, tests, browser QA, and headless simulation:
  `uv run python -m reachy_mini.daemon.app.main --sim --headless`. A local hardware daemon cannot
  exercise the physical robot.
- The NVIDIA Jetson is the **robot host**. Its checkout runs `reachy_controller` and the hardware
  daemon, and it is physically connected to the Reachy Mini Lite, camera, microphone, and speaker.
- Never commit the real SSH username, hostname, tailnet name, or address. Use `<robot-host>` in
  tracked commands. Resolve it from private agent memory first; if unavailable, ask the owner.
  Never guess it or copy the actual value into tracked files, issues, or PRs.

Push the working branch, then deploy it through an interactive shell so the operator-local zsh
helpers load:

```bash
ssh <robot-host>
cd ~/reachy-console
git fetch origin
git switch <branch>                 # branch already present on this host
# First checkout only: git switch --track origin/<branch>
git pull --ff-only
uv sync --all-packages
reachy-reset
```

`reachy-up` idempotently starts the detached Jetson daemon and controller. `reachy-down`
gracefully stops the controller, then the daemon. `reachy-reset` is `reachy-down && reachy-up`.
These helpers live in the robot operator's `.zshrc`; they are not repository APIs and are available
only in an interactive zsh session.

If the helpers are unavailable, run the repository commands from the robot-host checkout root in
separate shells:

```bash
uv run python tools/reachy_mini_daemon_jetson.py
REACHY_MANAGE_DAEMON=1 uv run python -m reachy_controller.main
```

The controller's `DaemonSupervisor` already defaults to the same Jetson wrapper; do not duplicate
`REACHY_DAEMON_CMD`.

After `reachy-up` or `reachy-reset`, open the private Tailnet MagicDNS HTTPS URL. The helper exposes
the localhost-bound controller through Tailscale Serve. Resolve that URL from private agent memory
or ask the owner, and never write it into tracked files, issues, or PRs. Because the controller runs
on the Jetson with `REACHY_MANAGE_DAEMON=1`, its power button manages the on-device daemon.

## Environment and commands
- Python 3.12. Treat the root `pyproject.toml` and `uv.lock` as authoritative for dependency and uv
  pins.
- The repository is one uv workspace with one shared `.venv` and one `uv.lock`. From the repository
  root, run `uv sync --all-packages` after every checkout or pull on both machines. Never use plain
  `uv sync`, call `.venv/bin/python` directly, or activate the virtual environment.
- Run an app with `cd apps/<app> && uv run python -m <app>.main`. Controller-launched apps use the
  same shared interpreter directly.
- If an incremental sync leaves `cv2` broken because both OpenCV packages own it, rebuild with
  `rm -rf .venv && uv sync --all-packages`.

## Jetson requirements
- Start the hardware daemon with `uv run python tools/reachy_mini_daemon_jetson.py`. Do not use
  stock `reachy-mini-daemon`; it does not install the required NVIDIA camera and media patches.
- `reachy_chat` defaults to `REACHY_MINI_MEDIA_BACKEND=local` on Lite. The known WebRTC signalling
  warning does not block this local path; investigate it only when remote WebRTC is under test.
- Standalone SDK camera scripts must call `apply_nvidia_unixfd_patch()` before connecting. Shipped
  apps already apply it.

## Controller architecture and hardware safety
- `reachy_controller` is a FastAPI backend with vanilla JavaScript. Do not add a frontend framework,
  bundler, or build step.
- It communicates with daemon HTTP and WebSocket APIs and never constructs `ReachyMini()`.
- Daemon lifecycle management controls only a local process and is gated by
  `REACHY_MANAGE_DAEMON=1`.
- Only one actuating surface may control the robot: run one app at a time; serialize expressions
  one at a time; keep apps and expressions mutually blocking; and require the existing graceful
  return-home path after motor re-enable before expressions can run. Do not weaken or bypass these
  guards.
- Camera and audio use daemon-owned shared media. Never open a competing capture device.
- Drain subprocess stdout and stderr continuously into bounded storage. Never leave a blocking,
  unread `PIPE`.
- Loading telemetry is non-actuating. Starting an app or expression moves the physical robot, so
  coordinate before physical tests. The agent verifies functional state transitions; the owner
  performs subjective motion and audio hardware judgment.
- Never apply `filter: url(#…)` to `.vp-stage`; refraction belongs on the panel backdrop with a
  fallback for browsers that do not support it.

## Repository workflow
- Before substantive work, inspect recent git history and open GitHub issues.
- Use GitHub milestones for work spanning multiple issues. Put scope, non-scope, and dependency order
  in the milestone description; keep detailed requirements and acceptance criteria in child issues.
  Do not add a redundant PRD/tracker issue when the milestone already tracks the complete child set.
- Link pull requests to their issues and target pull requests to `main`.
- Leave merging to the user unless explicitly asked.
