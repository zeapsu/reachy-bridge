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
    assert json.loads((tmp_path / "state/app-overlays.json").read_text()) == {"yoda": {OVERLAY: "crash"}}
    # A circular import isn't a missing name.
    assert (
        app_overlays.learn("radio", "AttributeError: partially initialized module 'cv2' has no attribute 'imread'")
        is None
    )
    assert app_overlays.learn("radio", "Process exited with code 1\nOSError: PortAudio library not found") is None
    assert app_overlays.learn("radio", None) is None


def test_an_unbuilt_overlay_is_skipped_so_the_app_still_starts(caplog: Any) -> None:
    app_overlays.learn("yoda", "module 'cv2' has no attribute 'CascadeClassifier'")
    assert app_overlays.paths_for("yoda") == []
    assert "isn't built" in caplog.text


def scanned_app(tmp_path: Path) -> Path:
    site = tmp_path / "site-packages"
    package = site / "yoda"
    (package / "sub").mkdir(parents=True)
    (package / "vision.py").write_text("import cv2\nd = cv2.CascadeClassifier(cv2.data.haarcascades)\n")
    (package / "sub/frame.py").write_text("x = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)\n")
    (site / "cv2").mkdir()
    (site / "cv2/__init__.py").write_text("")
    return package


def venv(names: list[str], calls: list[int] | None = None) -> Any:
    def run(*args: Any, **kwargs: Any) -> Any:
        if calls is not None:
            calls.append(1)
        return types.SimpleNamespace(stdout=json.dumps(names))

    return run


def test_the_scan_finds_cv2_names_the_venv_lacks(tmp_path: Path) -> None:
    package = scanned_app(tmp_path)
    assert app_overlays.cv2_uses(package) == {"CascadeClassifier", "data", "cvtColor", "COLOR_BGR2GRAY"}
    calls: list[int] = []
    assert (
        app_overlays.check_app("yoda", package, "/py", venv(["data", "cvtColor", "COLOR_BGR2GRAY"], calls)) == OVERLAY
    )
    assert app_overlays.overlays_for("yoda") == [OVERLAY]
    assert app_overlays.check_app("yoda", package, "/py", venv([], calls)) is None  # already given
    assert len(calls) == 1  # the venv's OpenCV is asked once until it changes


def test_a_scanned_overlay_is_dropped_once_the_venv_has_everything(tmp_path: Path) -> None:
    package = scanned_app(tmp_path)
    app_overlays.check_app("yoda", package, "/py", venv(["data", "cvtColor", "COLOR_BGR2GRAY"]))
    (package.parent / "cv2/__init__.py").write_text("# reinstalled\n")
    os.utime(package.parent / "cv2/__init__.py", (1, 1))
    full = ["data", "cvtColor", "COLOR_BGR2GRAY", "CascadeClassifier"]
    assert app_overlays.check_app("yoda", package, "/py", venv(full)) is None
    assert app_overlays.overlays_for("yoda") == []


def test_a_crash_learned_overlay_stays(tmp_path: Path) -> None:
    package = scanned_app(tmp_path)
    app_overlays.learn("yoda", "module 'cv2' has no attribute 'CascadeClassifier'")
    full = ["data", "cvtColor", "COLOR_BGR2GRAY", "CascadeClassifier"]
    assert app_overlays.check_app("yoda", package, "/py", venv(full)) is None
    assert app_overlays.overlays_for("yoda") == [OVERLAY]


def test_nothing_to_decide_without_opencv_or_a_package(tmp_path: Path) -> None:
    package = scanned_app(tmp_path)
    (package.parent / "cv2/__init__.py").unlink()
    assert app_overlays.check_app("yoda", package, "/py", venv(["data"])) is None
    assert app_overlays.check_app("other", tmp_path / "missing", "/py") is None


def test_a_malformed_record_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    (tmp_path / "state/app-overlays.json").write_text(
        json.dumps({"yoda": [["x"]], "radio": {OVERLAY: 1}, "a": {"nope": "scan"}})
    )
    assert app_overlays.overlays_for("yoda") == []
    assert app_overlays.overlays_for("radio") == []
    assert app_overlays.overlays_for("a") == []


def test_the_shim_puts_overlays_first_on_the_path(tmp_path: Path) -> None:
    shim = types.ModuleType("shim")
    exec(app_bind.shim_source().replace("\ninstall()\n", "\n"), shim.__dict__)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    path = ["/stdlib", str(b), "/venv/lib/python3.12/site-packages"]
    shim.add_overlays(os.pathsep.join([str(a), str(b), str(tmp_path / "missing")]), path)
    assert path == ["/stdlib", str(a), str(b), "/venv/lib/python3.12/site-packages"]


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

    async def failing(manager: Any, app_name: str) -> str:
        raise RuntimeError("An app is already running")

    with pytest.raises(RuntimeError):
        asyncio.run(app_bind._start_app(failing, Manager(), "yoda"))
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
