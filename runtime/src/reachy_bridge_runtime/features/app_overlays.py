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
the app the OpenCV overlay, until a later check finds the venv has them all
again. An app that still exits with ``module 'cv2' has no attribute ...``
(an indirect use the scan can't see) keeps the overlay for good; delete its
entry in ``<state dir>/app-overlays.json`` to undo that.

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
import threading
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


# What an exit error says -> the overlay that fixes it. A circular import
# ("partially initialized module 'cv2'") is not a missing name.
SIGNATURES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<!partially initialized )module 'cv2' has no attribute '\w+'"), OPENCV_OVERLAY),
)
SCAN, CRASH = "scan", "crash"


def learned_file() -> Path:
    return paths.state_dir() / "app-overlays.json"


def learned() -> dict[str, dict[str, str]]:
    """App -> {overlay: how it was found (``scan`` or ``crash``)}."""
    try:
        data = json.loads(learned_file().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        app: {o: why for o, why in found.items() if isinstance(o, str) and o in OVERLAYS and isinstance(why, str)}
        for app, found in data.items()
        if isinstance(app, str) and isinstance(found, dict)
    }


def overlays_for(app_name: str) -> list[str]:
    names = list(APPS.get(app_name, ()))
    names += [n for n in learned().get(app_name, {}) if n not in names]
    return names


def learn(app_name: str, error: object) -> str | None:
    """Remember the overlay an app's exit error calls for; its name if new."""
    if not isinstance(error, str):
        return None
    for pattern, overlay in SIGNATURES:
        if (match := pattern.search(error)) and overlay not in overlays_for(app_name):
            _store(app_name, overlay, CRASH)
            logger.warning("%s runs with %s from its next start: it exited with %s", app_name, overlay, match.group(0))
            return overlay
    return None


CV2_USE = re.compile(r"\bcv2\.([A-Za-z_]\w*)")
CV2_TIMEOUT = 10.0
_cv2_names: dict[tuple[str, float], frozenset[str]] = {}


def cv2_uses(package: Path) -> set[str]:
    """Every ``cv2.<name>`` the package's Python files mention."""
    names: set[str] = set()
    for source in package.rglob("*.py"):
        try:
            names |= set(CV2_USE.findall(source.read_text(errors="replace")))
        except OSError:
            continue
    return names


def venv_cv2_names(python: str, site_packages: Path, run=subprocess.run) -> frozenset[str] | None:
    """The names the venv's ``cv2`` has (None without OpenCV). Asked of the
    venv's Python only when its OpenCV changed since the last time."""
    try:
        stamp = (site_packages / "cv2" / "__init__.py").stat().st_mtime
    except OSError:
        return None
    key = (python, stamp)
    if key not in _cv2_names:
        code = "import cv2, json; print(json.dumps(dir(cv2)))"
        try:
            done = run([python, "-I", "-c", code], capture_output=True, text=True, timeout=CV2_TIMEOUT)
            names = json.loads(done.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        if not isinstance(names, list):
            return None
        _cv2_names[key] = frozenset(n for n in names if isinstance(n, str))
    return _cv2_names[key]


def check_app(app_name: str, package: Path, python: str, run=subprocess.run) -> str | None:
    """Give ``app_name`` the OpenCV overlay while it calls something the
    venv's OpenCV lacks, and drop one the scan gave it once the venv has it
    all again; the overlay's name if newly given."""
    if not package.is_dir():
        logger.debug("No package folder for %s at %s; not checking its OpenCV", app_name, package)
        return None
    found = learned().get(app_name, {})
    if found.get(OPENCV_OVERLAY) == CRASH or OPENCV_OVERLAY in APPS.get(app_name, ()):
        return None
    uses = cv2_uses(package)
    available = venv_cv2_names(python, package.parent, run) if uses else None
    if available is None:
        return None
    missing = sorted(uses - available)
    if not missing:
        if OPENCV_OVERLAY in found:
            _store(app_name, OPENCV_OVERLAY, None)
            logger.info("%s runs with the venv's OpenCV again: it has everything the app uses", app_name)
        return None
    if OPENCV_OVERLAY in found:
        return None
    _store(app_name, OPENCV_OVERLAY, SCAN)
    logger.warning("%s runs with %s: the venv's OpenCV has no %s", app_name, OPENCV_OVERLAY, ", ".join(missing))
    return OPENCV_OVERLAY


_store_lock = threading.Lock()


def _store(app_name: str, overlay: str, why: str | None) -> None:
    """Record (or with ``why=None`` forget) an overlay for an app."""
    with _store_lock:
        data = learned()
        found = data.setdefault(app_name, {})
        if why is None:
            found.pop(overlay, None)
        else:
            found[overlay] = why
        if not found:
            del data[app_name]
        target = learned_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        tmp.replace(target)


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
