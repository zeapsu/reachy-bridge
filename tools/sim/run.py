"""Run the sim checks end to end: sim daemon, controller, test apps, smoke.py.

    uv run python tools/sim/run.py [--work-dir DIR] [--audio] [--record FILE] [-- COMMAND...]

Starts its own sim daemon (through ``sim_daemon.py``) and controller on free
ports, installs the test apps from ``tools/sim/apps`` into a fresh apps venv,
waits for both servers, runs ``smoke.py`` against them and tears everything
down. Nothing is shared with a daemon or controller already running, and the
checkout's own ``apps_venv`` is left alone.

The controller binary comes from ``SIM_CONTROLLER_BIN`` (default
``controller/target/release/reachy-controller``; build it first, see
``tools/sim/README.md``). Logs, state and the apps venv go in ``--work-dir``
(default: a new temporary directory, kept and printed when a check fails).
Exit code is smoke.py's: 0 = all checks passed. After ``--``, another
command runs in smoke.py's place with the same environment (the browser
tests in ``web/e2e`` do).

``--audio`` also starts the audio gateway, with a test tone in place of each
of the robot's sound sources and files in place of its outputs (``AUDIO``),
so the browser tests can check which sound goes where (``web/e2e/voice.spec.ts``).
It needs GStreamer's WebRTC elements (gstreamer1.0-plugins-bad,
gstreamer1.0-nice) and a local address on the gateway's ICE networks
(``AUDIO_GATEWAY_NETS``, the tailnet by default). Not on a robot: the gateway
would mute and unmute the robot's real speaker as the tests switch hearing.

``--record FILE`` puts a recording proxy (``record.py``) between the daemon
and both the controller and the command, and writes the status codes and
body shapes the daemon answered to ``FILE`` (``differential.py`` compares
two such recordings, #317).
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tools" / "sim"
APPS = sorted(p for p in (SIM / "apps").iterdir() if (p / "pyproject.toml").exists())
READY_TIMEOUT = 120.0
# The dashboard's move datasets (controller/src/datasets.rs). The controller
# lists them at startup, and the daemon loads a dataset it doesn't have on its
# event loop, blocking every other request until the Hugging Face download
# finishes. smoke.py calls the daemon directly, past the power lock that keeps
# the controller's own requests clear of it, so a slow download timed its
# first request out. Downloading them first makes that load quick.
DATASETS = ("pollen-robotics/reachy-mini-emotions-library", "pollen-robotics/reachy-mini-dances-library")


# The robot's sound sources as tones, one frequency each, and its outputs as
# raw files (16 kHz mono S16LE) in <work>/audio. The browser's mic is the third
# source, from the browser tests' fake capture file.
AUDIO_RATE = 16000
TONES = {"mic": 500, "robot": 700, "browser": 1100}
_RAW = f"audio/x-raw,format=S16LE,rate={AUDIO_RATE},channels=1"


def _tone(freq: int) -> str:
    return f"audiotestsrc is-live=true wave=sine freq={freq} volume=0.25"


def _file(path: Path) -> str:
    return f'audioconvert ! audioresample ! {_RAW} ! filesink location="{path}" buffer-mode=unbuffered sync=false'


def audio_env(audio: Path, port: int, key_file: Path) -> dict[str, str]:
    """The gateway's environment for --audio: tones in, files out."""
    return {
        "AUDIO_GATEWAY_PORT": str(port),
        "AUDIO_GATEWAY_KEY_FILE": str(key_file),
        "AUDIO_GATEWAY_SOURCE": _tone(TONES["mic"]),
        "AUDIO_GATEWAY_HEAR": _tone(TONES["robot"]),
        "AUDIO_GATEWAY_SINK": _file(audio / "speaker.raw"),
        "AUDIO_GATEWAY_APPS": _file(audio / "apps.raw"),
        # Clear of a real gateway's 47100-47199.
        "AUDIO_GATEWAY_PORTS": "47300-47399",
        "PYTHONUNBUFFERED": "1",
    }


def wait_port(port: int, proc: subprocess.Popen, name: str) -> None:
    end = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited with code {proc.returncode} before it was ready")
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.5)
    raise RuntimeError(f"{name} not listening on {port} after {READY_TIMEOUT:.0f} s")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def install_apps(apps_root: Path) -> None:
    """A fresh apps venv with the test apps, as the daemon's install would make.

    Created here rather than through the daemon's install API, which would
    also pre-install the latest ``reachy-mini`` from PyPI: the test apps
    don't import it.
    """
    venv = apps_root / "apps_venv"
    subprocess.run(["uv", "venv", "--quiet", "--python", sys.executable, str(venv)], check=True)
    subprocess.run(
        ["uv", "pip", "install", "--quiet", "--python", str(venv / "bin" / "python"), *map(str, APPS)],
        check=True,
    )


def download_datasets() -> None:
    """Put the datasets in the Hugging Face cache, where the daemon looks first."""
    from huggingface_hub import snapshot_download

    for dataset in DATASETS:
        try:
            snapshot_download(dataset, repo_type="dataset", token=False)
        except Exception as e:  # noqa: BLE001 - the checks can run without them
            print(f"could not download {dataset}, running without it: {e}", flush=True)


def wait_up(url: str, proc: subprocess.Popen, name: str) -> None:
    end = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited with code {proc.returncode} before it was ready")
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{name} not ready at {url} after {READY_TIMEOUT:.0f} s")


def stop(proc: subprocess.Popen | None, timeout: float = 30.0) -> None:
    """SIGINT the process group (the daemon stops its app and backend), then kill."""
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    except ProcessLookupError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work-dir", type=Path, help="logs, state and apps venv (default: a temp dir)")
    parser.add_argument("--audio", action="store_true", help="also start the audio gateway, on test tones and files")
    parser.add_argument("--record", type=Path, help="record the daemon's answers to this file (differential.py)")
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="run this against the servers instead of smoke.py (after --), with SIM_DAEMON and SIM_CONTROLLER set",
    )
    args = parser.parse_args()
    if args.command and args.command[0] != "--":
        parser.error(f"unexpected argument {args.command[0]!r} (a command goes after --)")

    controller_bin = Path(
        os.getenv("SIM_CONTROLLER_BIN", ROOT / "controller" / "target" / "release" / "reachy-controller")
    )
    if not controller_bin.exists():
        print(f"controller binary not found: {controller_bin} (see tools/sim/README.md)", file=sys.stderr)
        return 2

    command = args.command[1:] or [sys.executable, str(SIM / "smoke.py")]
    temporary = args.work_dir is None
    work = Path(tempfile.mkdtemp(prefix="rb-sim-")) if temporary else args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    daemon_port = free_port()
    while (controller_port := free_port()) == daemon_port:
        pass
    # The app UI proxy too, so a dev controller on the default 7863 can't collide.
    while (app_port := free_port()) in (daemon_port, controller_port):
        pass
    while (audio_port := free_port()) in (daemon_port, controller_port, app_port):
        pass
    audio = work / "audio"
    audio_url = f"http://127.0.0.1:{audio_port}"
    daemon_url = f"http://127.0.0.1:{daemon_port}"
    controller_url = f"http://127.0.0.1:{controller_port}"
    print(f"sim run in {work}: daemon {daemon_url}, controller {controller_url}", flush=True)

    daemon = controller = gateway = proxy = None
    code = 1
    try:
        install_apps(work / "apps")
        download_datasets()
        with open(work / "daemon.log", "wb") as log:
            daemon = subprocess.Popen(
                [
                    sys.executable,
                    str(SIM / "sim_daemon.py"),
                    "--sim",
                    "--headless",
                    "--no-media",
                    "--desktop-app-daemon",
                    "--no-autostart",
                    "--no-wake-up-on-start",
                    "--fastapi-port",
                    str(daemon_port),
                ],
                cwd=ROOT,
                # REACHY_MINI_TELEMETRY=0: no usage events from test runs.
                env={
                    **os.environ,
                    "REACHY_BRIDGE_STATE_DIR": str(work / "daemon-state"),
                    "REACHY_BRIDGE_APPS_ROOT": str(work / "apps"),
                    "REACHY_MINI_TELEMETRY": "0",
                },
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        wait_up(f"{daemon_url}/api/daemon/status", daemon, "daemon")
        if args.record:
            from record import RecordingProxy

            proxy = RecordingProxy(daemon_url)
            proxy.start()
            daemon_url = proxy.url
        with open(work / "controller.log", "wb") as log:
            controller = subprocess.Popen(
                [str(controller_bin)],
                env={
                    **os.environ,
                    "DAEMON_URL": daemon_url,
                    "CONTROLLER_PORT": str(controller_port),
                    "CONTROLLER_APP_PORT": str(app_port),
                    "REACHY_BRIDGE_STATE_DIR": str(work / "controller-state"),
                    "REACHY_DAEMON_UNIT": "none.service",
                    # No gateway unit: --audio starts it, or nothing answers.
                    "AUDIO_GATEWAY_URL": audio_url,
                    "AUDIO_GATEWAY_UNIT": "",
                },
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        wait_up(f"{controller_url}/api/daemon/status", controller, "controller")
        extra = {}
        if args.audio:
            audio.mkdir(exist_ok=True)
            with open(work / "audio-gateway.log", "wb") as log:
                gateway = subprocess.Popen(
                    [sys.executable, "-m", "reachy_bridge_runtime.audio_gateway"],
                    env={**os.environ, **audio_env(audio, audio_port, work / "controller-state" / "audio.key")},
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            wait_port(audio_port, gateway, "audio gateway")
            extra = {
                "SIM_AUDIO": str(audio),
                "SIM_AUDIO_RATE": str(AUDIO_RATE),
                "SIM_AUDIO_GATEWAY": audio_url,
                "SIM_AUDIO_KEY": str(work / "controller-state" / "audio.key"),
                **{f"SIM_TONE_{name.upper()}": str(freq) for name, freq in TONES.items()},
            }
        code = subprocess.run(
            command,
            env={**os.environ, "SIM_DAEMON": daemon_url, "SIM_CONTROLLER": controller_url, **extra},
        ).returncode
    except (RuntimeError, OSError, subprocess.CalledProcessError) as e:
        print(f"sim run failed: {e}", file=sys.stderr)
    finally:
        stop(gateway)
        stop(controller)
        if proxy is not None:
            proxy.save(args.record)
            proxy.close()
        stop(daemon)
    if code == 0 and temporary:
        shutil.rmtree(work, ignore_errors=True)
    else:
        print(
            f"logs: {work / 'daemon.log'} {work / 'controller.log'}"
            + (f" {work / 'audio-gateway.log'}" if args.audio else ""),
            flush=True,
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
