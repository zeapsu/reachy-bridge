"""Keep the apps venvs on the daemon's reachy-mini and OpenCV.

Apps import their own ``reachy_mini`` from ``apps_venv``. Upstream installs it
once, unpinned, when it creates that venv, and an app's install only moves it
when the app asks for more, to the newest release rather than the daemon's. So
``install.sh`` calls this after replacing the daemon's venv:

    python -m reachy_bridge_runtime.features.apps_venv_sync <apps python>...

Only packages a venv already has change: a venv without OpenCV doesn't gain
it. An interpreter that doesn't exist is skipped. OpenCV follows the daemon too
because nothing in the apps venv asks for 5, and Baby Yoda needs 4 (#279).

The pins ignore what other installed apps require, so an app that asks for more
than the daemon has is pulled back. ``uv pip check`` then prints the conflict
(the install carries on), which names the app to look at.
"""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

OPENCV = (
    "opencv-python",
    "opencv-python-headless",
    "opencv-contrib-python",
    "opencv-contrib-python-headless",
)


def daemon_versions() -> dict[str, str]:
    """Package -> the version the venv should have, from the running (daemon's) venv."""
    wanted = {"reachy-mini": version("reachy-mini")}
    opencv = version("opencv-python-headless")
    wanted.update(dict.fromkeys(OPENCV, opencv))
    return wanted


def pins(installed: dict[str, str], wanted: dict[str, str]) -> list[str]:
    """The requirements that move the packages ``installed`` has to ``wanted``."""
    return [f"{name}=={want}" for name, want in wanted.items() if name in installed and installed[name] != want]


def installed_versions(python: str, run=subprocess.run) -> dict[str, str]:
    result = run(
        ["uv", "pip", "list", "--format", "json", "--python", python],
        capture_output=True,
        text=True,
        check=True,
    )
    return {package["name"]: package["version"] for package in json.loads(result.stdout)}


def sync(python: str, wanted: dict[str, str], run=subprocess.run) -> list[str]:
    """Move ``python``'s venv to ``wanted``; return what changed."""
    requirements = pins(installed_versions(python, run), wanted)
    if requirements:
        run(["uv", "pip", "install", "--quiet", "--python", python, *requirements], check=True)
    return requirements


def conflicts(python: str, run=subprocess.run) -> str:
    """What ``uv pip check`` reports wrong in ``python``'s venv, or nothing."""
    result = run(["uv", "pip", "check", "--python", python], capture_output=True, text=True)
    if result.returncode == 0:
        return ""
    # uv writes the report to stderr, between lines about the check itself.
    noise = ("Using Python", "Checked ", "Found ")
    return "\n".join(line for line in result.stderr.splitlines() if line.strip() and not line.startswith(noise))


def main(argv: list[str]) -> int:
    wanted = daemon_versions()
    failed = False
    for python in argv:
        if not Path(python).exists():
            continue
        try:
            changed = sync(python, wanted)
        except (subprocess.CalledProcessError, OSError, ValueError, KeyError, TypeError) as exc:
            print(f"apps venv sync: couldn't update {python}: {exc}", file=sys.stderr)
            failed = True
            continue
        if changed:
            print(f"apps venv sync: {python}: {', '.join(changed)}")
            if found := conflicts(python):
                print(f"apps venv sync: warning: requirements now unmet in {python}:\n{found}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
