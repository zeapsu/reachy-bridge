"""Per-app overlays: an app that needs an older OpenCV gets one (#279)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from reachy_bridge_runtime import upstream
from reachy_bridge_runtime.features import app_bind, app_overlays

OVERLAY = app_overlays.OPENCV_OVERLAY


@pytest.fixture(autouse=True)
def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("REACHY_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("REACHY_BRIDGE_APP_OVERLAYS", str(tmp_path / "overlays"))
    app_overlays._cv2_names.clear()
    return tmp_path


def built(base: Path, name: str = OVERLAY) -> Path:
    (base / name).mkdir(parents=True)
    (base / name / ".complete").write_text("x\n")
    return base / name


def test_build_installs_each_missing_overlay_into_its_own_folder(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(args: list[str]) -> Any:
        calls.append(args)
        target = Path(args[args.index("--target") + 1])
        (target / "cv2").mkdir(parents=True)
        return types.SimpleNamespace(returncode=0)

    base = tmp_path / "overlays"
    (base / "opencv-4.0.0").mkdir(parents=True)  # an old pin
    assert app_overlays.build("/venv/bin/python", base, run) == []
    assert (base / OVERLAY / "cv2").is_dir() and (base / OVERLAY / ".complete").is_file()
    assert not (base / "opencv-4.0.0").exists()
    assert calls[0][:4] == ["uv", "pip", "install", "--quiet"] and "--no-deps" in calls[0]
    assert app_overlays.build("/venv/bin/python", base, run) == []
    assert len(calls) == 1  # already built


def test_a_failed_build_leaves_nothing_half_done(tmp_path: Path) -> None:
    base = tmp_path / "overlays"
    assert app_overlays.build("/venv/bin/python", base, lambda args: types.SimpleNamespace(returncode=1)) == [OVERLAY]
    assert list(base.iterdir()) == []


def test_an_app_that_exits_on_a_missing_cv2_name_gets_the_overlay_next_time(tmp_path: Path) -> None:
    folder = built(tmp_path / "overlays")
    assert app_overlays.paths_for("yoda") == []
    error = "Process exited with code 1\nAttributeError: module 'cv2' has no attribute 'CascadeClassifier'"
    assert app_overlays.learn("yoda", error) == OVERLAY
    assert app_overlays.learn("yoda", error) is None  # once
    assert app_overlays.paths_for("yoda") == [folder]
    assert json.loads((tmp_path / "state/app-overlays.json").read_text()) == {"yoda": [OVERLAY]}
    assert app_overlays.learn("radio", "Process exited with code 1\nOSError: PortAudio library not found") is None
    assert app_overlays.learn("radio", None) is None


def test_an_unbuilt_overlay_is_skipped_so_the_app_still_starts(caplog: Any) -> None:
    app_overlays.learn("yoda", "module 'cv2' has no attribute 'CascadeClassifier'")
    assert app_overlays.paths_for("yoda") == []
    assert "isn't built" in caplog.text


def test_the_scan_finds_cv2_names_the_venv_lacks(tmp_path: Path) -> None:
    package = tmp_path / "yoda"
    (package / "sub").mkdir(parents=True)
    (package / "vision.py").write_text("import cv2\nd = cv2.CascadeClassifier(cv2.data.haarcascades)\n")
    (package / "sub/frame.py").write_text("x = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)\n")
    assert app_overlays.cv2_uses(package) == {"CascadeClassifier", "data", "cvtColor", "COLOR_BGR2GRAY"}

    def venv(names: list[str]) -> Any:
        return lambda *a, **k: types.SimpleNamespace(stdout=json.dumps([1.0, names]))

    assert (
        app_overlays.check_app(
            "yoda", package, "/py", venv(["data", "cvtColor", "COLOR_BGR2GRAY", "CascadeClassifier"])
        )
        is None
    )
    app_overlays._cv2_names.clear()
    assert app_overlays.check_app("yoda", package, "/py", venv(["data", "cvtColor", "COLOR_BGR2GRAY"])) == OVERLAY
    assert app_overlays.overlays_for("yoda") == [OVERLAY]
    # No OpenCV in the venv, or no package: nothing to decide.
    assert app_overlays.check_app("other", package, "/py", lambda *a, **k: types.SimpleNamespace(stdout="")) is None
    assert app_overlays.check_app("other", tmp_path / "missing", "/py") is None


def test_the_shim_puts_overlays_first_on_the_path(tmp_path: Path) -> None:
    shim = types.ModuleType("shim")
    exec(app_bind.shim_source().replace("\ninstall()\n", "\n"), shim.__dict__)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    path = ["/stdlib", str(b), "/site-packages"]
    shim.add_overlays(os.pathsep.join([str(a), str(b), str(tmp_path / "missing")]), path)
    assert path == [str(a), str(b), "/stdlib", "/site-packages"]


class Manager:
    wireless_version = False
    desktop_app_daemon = True
    current_app = None


def test_only_the_app_being_started_sees_its_overlays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = built(tmp_path / "overlays")
    app_overlays.learn("yoda", "module 'cv2' has no attribute 'CascadeClassifier'")
    venv = tmp_path / "apps_venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "lib/python3.12/site-packages").mkdir(parents=True)
    monkeypatch.setattr(upstream, "app_python", lambda manager, name: venv / "bin/python")
    seen: dict[str, str | None] = {}

    async def original(manager: Any, app_name: str) -> str:
        seen[app_name] = os.environ.get(app_overlays.PATHS_ENV)
        return "started"

    asyncio.run(app_bind._start_app(original, Manager(), "yoda"))
    asyncio.run(app_bind._start_app(original, Manager(), "radio"))
    assert seen == {"yoda": str(folder), "radio": None}
    assert app_overlays.PATHS_ENV not in os.environ


def test_a_crash_after_start_is_learned_from(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    venv = tmp_path / "apps_venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "lib/python3.12/site-packages").mkdir(parents=True)
    monkeypatch.setattr(upstream, "app_python", lambda manager, name: venv / "bin/python")
    manager = Manager()

    async def scenario() -> None:
        status = types.SimpleNamespace(error=None)

        async def monitor() -> None:
            status.error = (
                "Process exited with code 1\nAttributeError: module 'cv2' has no attribute 'CascadeClassifier'"
            )

        async def original(manager: Any, app_name: str) -> Any:
            manager.current_app = types.SimpleNamespace(monitor_task=asyncio.create_task(monitor()), status=status)
            return status

        await app_bind._start_app(original, manager, "yoda")
        await manager.current_app.monitor_task
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert app_overlays.overlays_for("yoda") == [OVERLAY]


@pytest.mark.skipif(sys.platform != "linux", reason="uses a real venv")
def test_the_shim_and_env_pick_the_overlay_module(tmp_path: Path) -> None:
    """End to end in a real interpreter: a module in the overlay shadows the
    venv's copy only when the variable names the overlay."""
    site = app_bind.install_shim(_venv(tmp_path / "apps_venv"))
    (site / "overlaid_mod.py").write_text("VERSION = 5\n")
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "overlaid_mod.py").write_text("VERSION = 4\n")
    code = f"import site; site.addsitedir({str(site)!r}); import overlaid_mod; print(overlaid_mod.VERSION)"

    def version(env: dict[str, str]) -> str:
        return subprocess.run(
            [sys.executable, "-I", "-c", code], capture_output=True, text=True, env=env, check=True
        ).stdout.strip()

    assert version({}) == "5"
    assert version({app_overlays.PATHS_ENV: str(overlay)}) == "4"


def _venv(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    (root / "lib/python3.12/site-packages").mkdir(parents=True)
    return root / "bin/python"
