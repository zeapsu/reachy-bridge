"""The apps venvs follow the daemon's reachy-mini and OpenCV."""

from __future__ import annotations

import json
import subprocess
import types
from pathlib import Path

import pytest

from reachy_bridge_runtime.features import apps_venv_sync

WANTED = {"reachy-mini": "1.13.0", "opencv-python": "4.14.0.94", "opencv-python-headless": "4.14.0.94"}


def fake_uv(installed: dict[str, str], fail_install: bool = False):
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs):
        calls.append(args)
        if args[2] == "install" and fail_install:
            raise subprocess.CalledProcessError(1, args)
        listing = [{"name": name, "version": version} for name, version in installed.items()]
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(listing))

    return run, calls


def test_moves_only_what_the_venv_has_and_differs() -> None:
    installed = {"reachy-mini": "1.12.0", "opencv-python-headless": "5.0.0.93", "numpy": "2.5.4"}
    assert apps_venv_sync.pins(installed, WANTED) == ["reachy-mini==1.13.0", "opencv-python-headless==4.14.0.94"]


def test_a_newer_package_comes_back_to_the_daemons() -> None:
    assert apps_venv_sync.pins({"reachy-mini": "1.14.0"}, WANTED) == ["reachy-mini==1.13.0"]


def test_sync_installs_the_pins_into_that_python() -> None:
    run, calls = fake_uv({"reachy-mini": "1.12.0"})
    assert apps_venv_sync.sync("/apps/bin/python", WANTED, run) == ["reachy-mini==1.13.0"]
    assert calls[-1] == ["uv", "pip", "install", "--quiet", "--python", "/apps/bin/python", "reachy-mini==1.13.0"]


def test_sync_installs_nothing_when_already_matching() -> None:
    run, calls = fake_uv({"reachy-mini": "1.13.0", "opencv-python": "4.14.0.94"})
    assert apps_venv_sync.sync("/apps/bin/python", WANTED, run) == []
    assert len(calls) == 1  # the listing only


def test_daemon_versions_read_the_running_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apps_venv_sync, "version", {"reachy-mini": "1.13.0", "opencv-python-headless": "4.14.0.94"}.get)
    wanted = apps_venv_sync.daemon_versions()
    assert wanted["reachy-mini"] == "1.13.0"
    assert {wanted[name] for name in apps_venv_sync.OPENCV} == {"4.14.0.94"}


def test_main_skips_a_missing_venv_and_reports_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(apps_venv_sync, "daemon_versions", lambda: WANTED)
    assert apps_venv_sync.main([str(tmp_path / "missing")]) == 0

    python = tmp_path / "python"
    python.touch()
    run, _ = fake_uv({"reachy-mini": "1.12.0"}, fail_install=True)
    real_sync = apps_venv_sync.sync
    monkeypatch.setattr(apps_venv_sync, "sync", lambda p, w: real_sync(p, w, run))
    assert apps_venv_sync.main([str(python)]) == 1
    assert "couldn't update" in capsys.readouterr().err
