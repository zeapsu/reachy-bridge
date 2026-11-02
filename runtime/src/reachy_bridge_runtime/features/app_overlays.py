"""Per-app package overlays for apps that need an older library (#279).

Community apps share one venv, so one app's loose requirement sets the
version for all of them. reachy_baby_yoda asks for ``opencv-python-headless
>=4.5`` but uses ``cv2.CascadeClassifier`` and the Haar cascade files, which
OpenCV 5 no longer ships; another app's install brought 5 into the venv.

Instead of holding every app back, an overlay is a folder of packages
installed with ``uv pip install --target`` (no dependencies: it reuses the
venv's numpy). ``install.sh`` builds the overlays; when a listed app starts,
``app_bind`` hands its overlay folders to the app's processes in
``REACHY_BRIDGE_APP_PATHS``, and the app shim puts them first on
``sys.path``. Every other app keeps the venv's versions.

Apps don't say they need an older version (Baby Yoda allows 5), so the
bridge looks at what they call. Before each start, the ``cv2.<name>`` uses in
the app's package are checked against the venv's OpenCV; any missing gives
the app the OpenCV overlay. An app that still exits with ``module 'cv2' has
no attribute ...`` (an indirect use the scan can't see) is remembered too.
Both land in ``<state dir>/app-overlays.json`` with the hand-kept ``APPS``.

    python -m reachy_bridge_runtime.features.app_overlays [apps python]

builds any overlay that is missing (an overlay's name carries its version).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from reachy_bridge_runtime import paths

logger = logging.getLogger(__name__)

PATHS_ENV = "REACHY_BRIDGE_APP_PATHS"

# Overlay name -> exact requirements. A new pin gets a new folder (the name
# carries the version), so a stale overlay is never half-replaced.
OPENCV_OVERLAY = "opencv-4.14.0.94"
OVERLAYS: dict[str, tuple[str, ...]] = {
    OPENCV_OVERLAY: ("opencv-python-headless==4.14.0.94",),
}

# App -> overlays it always runs with, for what the checks below can't see.
APPS: dict[str, tuple[str, ...]] = {}


# What an exit error says -> the overlay that fixes it.
SIGNATURES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"module 'cv2' has no attribute '\w+'"), OPENCV_OVERLAY),
)


def learned_file() -> Path:
    return paths.state_dir() / "app-overlays.json"


def learned() -> dict[str, list[str]]:
    try:
        data = json.loads(learned_file().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {app: [o for o in names if o in OVERLAYS] for app, names in data.items() if isinstance(names, list)}


def overlays_for(app_name: str) -> list[str]:
    names = list(APPS.get(app_name, ()))
    names += [n for n in learned().get(app_name, []) if n not in names]
    return names


def learn(app_name: str, error: object) -> str | None:
    """Remember the overlay an app's exit error calls for; its name if new."""
    if not isinstance(error, str):
        return None
    for pattern, overlay in SIGNATURES:
        if (match := pattern.search(error)) and overlay not in overlays_for(app_name):
            return _remember(app_name, overlay, f"it exited with {match.group(0)}")
    return None


CV2_USE = re.compile(r"\bcv2\.([A-Za-z_]\w*)")
_cv2_names: dict[str, frozenset[str] | None] = {}


def cv2_uses(package: Path) -> set[str]:
    """Every ``cv2.<name>`` the package's Python files mention."""
    names: set[str] = set()
    for source in package.rglob("*.py"):
        try:
            names |= set(CV2_USE.findall(source.read_text(errors="replace")))
        except OSError:
            continue
    return names


def venv_cv2_names(python: str, run=subprocess.run) -> frozenset[str] | None:
    """The names the venv's ``cv2`` has (None without OpenCV), once per
    install of it."""
    code = "import cv2, json, os; print(json.dumps([os.path.getmtime(cv2.__file__), dir(cv2)]))"
    try:
        done = run([python, "-I", "-c", code], capture_output=True, text=True, timeout=30)
        stamp, names = json.loads(done.stdout)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return None
    key = f"{python}:{stamp}"
    if key not in _cv2_names:
        _cv2_names[key] = frozenset(names)
    return _cv2_names[key]


def check_app(app_name: str, package: Path, python: str, run=subprocess.run) -> str | None:
    """Give ``app_name`` the OpenCV overlay if it calls something the venv's
    OpenCV lacks; the overlay's name if that's new."""
    if OPENCV_OVERLAY in overlays_for(app_name) or not package.is_dir():
        return None
    uses = cv2_uses(package)
    if not uses:
        return None
    available = venv_cv2_names(python, run)
    if available is None:
        return None
    missing = sorted(uses - available)
    if not missing:
        return None
    return _remember(app_name, OPENCV_OVERLAY, f"the venv's OpenCV has no {', '.join(missing)}")


def _remember(app_name: str, overlay: str, why: str) -> str:
    data = learned()
    data.setdefault(app_name, []).append(overlay)
    target = learned_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(target)
    logger.warning("%s runs with %s from now on: %s", app_name, overlay, why)
    return overlay


def root() -> Path:
    """``~/.local/share/reachy-bridge/app-overlays``, next to the bridge's venv."""
    if override := os.getenv("REACHY_BRIDGE_APP_OVERLAYS"):
        return Path(override)
    return Path(sys.prefix).parent / "app-overlays"


def paths_for(app_name: str, base: Path | None = None) -> list[Path]:
    """The built overlay folders ``app_name`` runs with; a missing one is
    logged and left out, so the app still starts."""
    base = root() if base is None else base
    found = []
    for name in overlays_for(app_name):
        folder = base / name
        if (folder / ".complete").is_file():
            found.append(folder)
        else:
            logger.warning("%s runs without its %s overlay: it isn't built (rerun deploy/install.sh)", app_name, name)
    return found


def build(python: str, base: Path | None = None, run=subprocess.run) -> list[str]:
    """Build every missing overlay for ``python``'s venv; return the failures."""
    base = root() if base is None else base
    base.mkdir(parents=True, exist_ok=True)
    failed = []
    for name, requirements in OVERLAYS.items():
        folder = base / name
        if (folder / ".complete").is_file():
            continue
        staging = base / f".{name}.new"
        shutil.rmtree(staging, ignore_errors=True)
        args = [
            "uv",
            "pip",
            "install",
            "--quiet",
            "--no-deps",
            "--python",
            python,
            "--target",
            str(staging),
            *requirements,
        ]
        if run(args).returncode != 0:
            shutil.rmtree(staging, ignore_errors=True)
            failed.append(name)
            continue
        (staging / ".complete").write_text("\n".join(requirements) + "\n")
        shutil.rmtree(folder, ignore_errors=True)
        staging.rename(folder)
    for stale in base.iterdir():
        if stale.is_dir() and stale.name not in OVERLAYS and not stale.name.startswith("."):
            shutil.rmtree(stale, ignore_errors=True)
    return failed


def main(argv: list[str]) -> int:
    python = argv[0] if argv else sys.executable
    failed = build(python)
    for name in failed:
        print(f"app overlays: couldn't build {name}; the apps that need it run without it", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
