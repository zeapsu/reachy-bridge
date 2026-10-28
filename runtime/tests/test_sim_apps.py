"""A sim daemon's apps never reach the robot's daemon (#218)."""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from reachy_bridge_runtime import platforms, upstream
from reachy_bridge_runtime.features import sim_apps
from reachy_bridge_runtime.main import for_daemon_args


def fake_venv(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    (root / "lib/python3.12/site-packages").mkdir(parents=True)
    return root / "bin/python"


def tagged_server(tag: bytes) -> int:
    """A loopback server that answers every connection with ``tag``."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()

    def serve() -> None:
        while True:
            conn, _ = server.accept()
            conn.sendall(tag)
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return server.getsockname()[1]


def reached(site_packages: Path, host: str, port: int) -> str:
    """Which server a fresh Python with the shim reaches when it dials ``host:port``."""
    code = (
        "import site, socket\n"
        f"site.addsitedir({str(site_packages)!r})\n"
        f"s = socket.create_connection(({host!r}, {port}), timeout=5)\n"
        "print(s.recv(16).decode())\n"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def test_shim_sends_the_robot_daemons_port_to_the_sim(tmp_path: Path) -> None:
    robot = tagged_server(b"robot")
    sim = tagged_server(b"sim")
    site_packages = sim_apps.install_shim(fake_venv(tmp_path / "apps_venv"), robot, sim)
    for host in ("127.0.0.1", "localhost"):
        assert reached(site_packages, host, robot) == "sim", host
    assert reached(site_packages, "127.0.0.1", sim) == "sim"
    other = tagged_server(b"other")
    assert reached(site_packages, "127.0.0.1", other) == "other"


def test_sim_address_only_moves_loopback_on_the_robot_port() -> None:
    assert sim_apps.SHIM.read_text().count("ROBOT_PORT = 8000\n") == 1
    namespace: dict[str, Any] = {}
    exec(compile(sim_apps.shim_source(8000, 8001).replace("\ninstall()\n", "\n"), "shim", "exec"), namespace)
    move = namespace["sim_address"]
    assert move(socket.AF_INET, ("127.0.0.1", 8000)) == ("127.0.0.1", 8001)
    assert move(socket.AF_INET6, ("::1", 8000, 0, 0)) == ("::1", 8001, 0, 0)
    assert move(socket.AF_INET, ("192.0.2.7", 8000)) == ("192.0.2.7", 8000)
    assert move(socket.AF_INET, ("127.0.0.1", 8042)) == ("127.0.0.1", 8042)
    assert move(socket.AF_UNIX, "/tmp/x") == "/tmp/x"


def test_only_a_sim_daemon_with_apps_gets_the_feature() -> None:
    board = platforms.Platform(name="x", tier="tested")
    assert sim_apps.FEATURE in for_daemon_args(board, ["--sim", "--desktop-app-daemon"]).features
    assert sim_apps.FEATURE not in for_daemon_args(board, ["--sim"]).features
    assert sim_apps.FEATURE not in for_daemon_args(board, ["--desktop-app-daemon"]).features


def test_refuses_the_robots_port_and_apps_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REACHY_BRIDGE_ROBOT_DAEMON_PORT", raising=False)
    with pytest.raises(RuntimeError, match="port"):
        sim_apps.ports(["--fastapi-port", "8000"])
    assert sim_apps.ports(["--fastapi-port", "8001"]) == (8000, 8001)
    monkeypatch.delenv("REACHY_BRIDGE_APPS_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="REACHY_BRIDGE_APPS_ROOT"):
        sim_apps.apps_root()
    monkeypatch.setattr(upstream, "default_apps_root", lambda: tmp_path)
    monkeypatch.setenv("REACHY_BRIDGE_APPS_ROOT", str(tmp_path))
    with pytest.raises(RuntimeError, match="robot's apps folder"):
        sim_apps.apps_root()


class Manager:
    wireless_version = False
    desktop_app_daemon = True


def started_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python: Path) -> list[str]:
    root = tmp_path / "sim"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("REACHY_BRIDGE_APPS_ROOT", str(root))
    monkeypatch.setattr(upstream, "default_apps_root", lambda: tmp_path / "robot")
    monkeypatch.setattr(upstream, "set_apps_root", lambda r: None)
    wrappers: list[Any] = []
    monkeypatch.setattr(upstream, "wrap_start_app", wrappers.append)
    monkeypatch.setattr(upstream, "app_python", lambda manager, name: python)
    sim_apps.apply(["--sim", "--fastapi-port", "8001"])
    started: list[str] = []

    async def original(manager: Any, app_name: str) -> None:
        started.append(app_name)

    asyncio.run(wrappers[0](original, Manager(), "radio"))
    return started


def test_start_app_points_the_app_at_the_sim_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    python = fake_venv(tmp_path / "sim/apps_venv")
    assert started_with(tmp_path, monkeypatch, python) == ["radio"]
    site_packages = tmp_path / "sim/apps_venv/lib/python3.12/site-packages"
    assert "SIM_PORT = 8001\n" in (site_packages / f"{sim_apps.MODULE}.py").read_text()
    assert (site_packages / sim_apps.PTH).exists()


def test_start_app_refuses_an_interpreter_outside_the_sim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    python = fake_venv(tmp_path / "robot/apps_venv")
    with pytest.raises(RuntimeError, match="sim's apps folder"):
        started_with(tmp_path, monkeypatch, python)
    assert not (tmp_path / "robot/apps_venv/lib/python3.12/site-packages" / sim_apps.PTH).exists()


def test_start_app_refuses_when_the_shim_cant_be_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    python = tmp_path / "sim/apps_venv/bin/python"  # no site-packages
    with pytest.raises(RuntimeError, match="sim daemon"):
        started_with(tmp_path, monkeypatch, python)


def test_set_apps_root_moves_apps_venv_and_keeps_the_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from reachy_mini.apps.sources import local_common_venv

    monkeypatch.setattr(local_common_venv, "_get_venv_parent_dir", local_common_venv._get_venv_parent_dir)
    default = upstream.default_apps_root()
    upstream.set_apps_root(tmp_path)
    assert local_common_venv._get_app_venv_path("radio", False, True) == tmp_path / "apps_venv"
    assert upstream.default_apps_root() == default != tmp_path
