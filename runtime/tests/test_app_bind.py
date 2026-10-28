"""App UIs and signalling stay on loopback (#186)."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from reachy_bridge_runtime import upstream
from reachy_bridge_runtime.features import app_bind


def fake_venv(root: Path) -> Path:
    """A venv layout: ``<root>/bin/python`` and one site-packages."""
    (root / "bin").mkdir(parents=True)
    (root / "lib/python3.12/site-packages").mkdir(parents=True)
    return root / "bin/python"


def bound_host(site_packages: Path, host: str, env: dict[str, str], family: int = socket.AF_INET) -> str:
    """Where a fresh Python with the shim's site-packages binds ``host``."""
    code = (
        "import site, socket, sys\n"
        f"site.addsitedir({str(site_packages)!r})\n"
        f"s = socket.socket({int(family)}, socket.SOCK_STREAM)\n"
        f"s.bind(({host!r}, 0))\n"
        "print(s.getsockname()[0])\n"
    )
    clean = {k: v for k, v in os.environ.items() if k != app_bind.ENV}
    result = subprocess.run([sys.executable, "-I", "-c", code], env={**clean, **env}, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def test_shim_moves_wildcard_tcp_binds_to_loopback(tmp_path: Path) -> None:
    site_packages = app_bind.install_shim(fake_venv(tmp_path / "apps_venv"))
    on = {app_bind.ENV: "loopback"}
    assert bound_host(site_packages, "0.0.0.0", on) == "127.0.0.1"
    assert bound_host(site_packages, "", on) == "127.0.0.1"
    assert bound_host(site_packages, "127.0.0.1", on) == "127.0.0.1"
    if socket.has_ipv6:
        assert bound_host(site_packages, "::", on, socket.AF_INET6) == "::1"
    # Without the variable (pip, uv, anything else run with the apps venv) nothing changes.
    assert bound_host(site_packages, "0.0.0.0", {}) == "0.0.0.0"


def test_shim_leaves_udp_and_explicit_addresses_alone() -> None:
    shim = types.ModuleType("shim")
    exec(app_bind.shim_source(), shim.__dict__)  # without the variable, install() does nothing
    assert shim.loopback_address(socket.AF_INET, socket.SOCK_DGRAM, ("0.0.0.0", 5353)) == ("0.0.0.0", 5353)
    assert shim.loopback_address(socket.AF_INET, socket.SOCK_STREAM, ("192.168.1.5", 80)) == ("192.168.1.5", 80)
    assert shim.loopback_address(socket.AF_INET6, socket.SOCK_STREAM, ("::", 80, 0, 0)) == ("::1", 80, 0, 0)
    assert shim.loopback_address(socket.AF_UNIX, socket.SOCK_STREAM, "/tmp/sock") == "/tmp/sock"


def test_install_is_idempotent(tmp_path: Path) -> None:
    python = fake_venv(tmp_path / "apps_venv")
    site_packages = app_bind.install_shim(python)
    module = site_packages / f"{app_bind.MODULE}.py"
    before = module.stat().st_mtime_ns
    app_bind.install_shim(python)
    assert module.stat().st_mtime_ns == before
    assert (site_packages / app_bind.PTH).read_text() == f"import {app_bind.MODULE}\n"


class Manager:
    wireless_version = False
    desktop_app_daemon = True


def start(monkeypatch: pytest.MonkeyPatch, python: Path) -> list[str]:
    started: list[str] = []

    async def original(manager: Any, app_name: str, *args: Any, **kwargs: Any) -> str:
        started.append(app_name)
        return "started"

    monkeypatch.setattr(upstream, "app_python", lambda manager, name: python)
    asyncio.run(app_bind._start_app(original, Manager(), "radio"))
    return started


def test_start_app_installs_the_shim_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    python = fake_venv(tmp_path / "apps_venv")
    assert start(monkeypatch, python) == ["radio"]
    assert (tmp_path / "apps_venv/lib/python3.12/site-packages" / app_bind.PTH).exists()


def test_start_app_refuses_when_the_shim_cant_be_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    python = tmp_path / "apps_venv/bin/python"  # no site-packages
    with pytest.raises(RuntimeError, match="loopback"):
        start(monkeypatch, python)


def test_start_app_refuses_the_bridges_own_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="desktop-app-daemon"):
        start(monkeypatch, Path(sys.prefix) / "bin/python")


def test_signalling_server_binds_loopback() -> None:
    class Sink:
        def __init__(self) -> None:
            self.props: dict[str, Any] = {}

        def set_property(self, name: str, value: Any) -> None:
            self.props[name] = value

    sink = Sink()
    assert app_bind._configure_webrtc(lambda server, pipeline: sink, object(), "pipeline") is sink
    assert sink.props == {"signalling-server-host": "127.0.0.1"}
