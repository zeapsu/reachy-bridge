"""App UIs and signalling stay on loopback (#186)."""

from __future__ import annotations

import asyncio
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


def bound_host(site_packages: Path, host: str, family: int = socket.AF_INET, v6only: bool | None = None) -> str:
    """Where a fresh Python with the shim's site-packages binds ``host``."""
    code = (
        "import site, socket, sys\n"
        f"site.addsitedir({str(site_packages)!r})\n"
        f"s = socket.socket({int(family)}, socket.SOCK_STREAM)\n"
        + (f"s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, {int(v6only)})\n" if v6only is not None else "")
        + f"s.bind(({host!r}, 0))\n"
        "print(s.getsockname()[0])\n"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def lan_address() -> str | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))
        except OSError:
            return None
        return s.getsockname()[0]


def test_shim_moves_every_non_loopback_tcp_bind_to_loopback(tmp_path: Path) -> None:
    site_packages = app_bind.install_shim(fake_venv(tmp_path / "apps_venv"))
    for host in ("0.0.0.0", "", "0", "0.0"):
        assert bound_host(site_packages, host) == "127.0.0.1", host
    lan = lan_address()
    if lan and not lan.startswith("127."):
        assert bound_host(site_packages, lan) == "127.0.0.1"
    # Loopback stays as asked.
    assert bound_host(site_packages, "127.0.0.1") == "127.0.0.1"
    assert bound_host(site_packages, "localhost") in ("127.0.0.1", "::1")
    if socket.has_ipv6:
        assert bound_host(site_packages, "::", socket.AF_INET6, v6only=True) == "::1"
        # Dual stack: IPv4 loopback, so 127.0.0.1 clients (the proxy) still reach it.
        assert bound_host(site_packages, "::", socket.AF_INET6, v6only=False) == "::ffff:127.0.0.1"
        assert bound_host(site_packages, "::ffff:0.0.0.0", socket.AF_INET6, v6only=False) == "::ffff:127.0.0.1"


def test_shim_leaves_udp_and_loopback_alone() -> None:
    shim = types.ModuleType("shim")
    shim.__dict__["__name__"] = "shim"
    source = app_bind.shim_source().replace("\ninstall()\n", "\n")  # don't patch the test process
    exec(source, shim.__dict__)
    assert shim.loopback_address(socket.AF_INET, socket.SOCK_DGRAM, ("0.0.0.0", 5353)) == ("0.0.0.0", 5353)
    assert shim.loopback_address(socket.AF_INET, socket.SOCK_STREAM, ("192.168.1.5", 80)) == ("127.0.0.1", 80)
    assert shim.loopback_address(socket.AF_INET, socket.SOCK_STREAM, ("127.0.0.5", 80)) == ("127.0.0.5", 80)
    assert shim.loopback_address(socket.AF_INET6, socket.SOCK_STREAM, ("::", 80, 0, 0)) == ("::1", 80, 0, 0)
    assert shim.loopback_address(socket.AF_INET6, socket.SOCK_STREAM, ("::", 80), v6only=False) == ("::ffff:127.0.0.1", 80, 0, 0)
    assert shim.loopback_address(socket.AF_UNIX, socket.SOCK_STREAM, "/tmp/sock") == "/tmp/sock"
    assert shim.loopback_address(socket.AF_INET, socket.SOCK_STREAM, (b"0.0.0.0", 80)) == ("127.0.0.1", 80)


def test_site_packages_follows_pyvenv_cfg(tmp_path: Path) -> None:
    python = fake_venv(tmp_path / "apps_venv")
    (tmp_path / "apps_venv/lib/python3.11/site-packages").mkdir(parents=True)  # stale leftover
    (tmp_path / "apps_venv/pyvenv.cfg").write_text("home = /usr/bin\nversion_info = 3.12.7\n")
    assert app_bind.site_packages(python) == tmp_path / "apps_venv/lib/python3.12/site-packages"


def test_install_replaces_a_planted_symlink_instead_of_writing_through_it(tmp_path: Path) -> None:
    python = fake_venv(tmp_path / "apps_venv")
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    target = tmp_path / "apps_venv/lib/python3.12/site-packages" / app_bind.PTH
    target.symlink_to(victim)
    app_bind.install_shim(python)
    assert victim.read_text() == "keep"
    assert not target.is_symlink() and target.read_text() == f"import {app_bind.MODULE}\n"


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


class Sink:
    def __init__(self, has_property: bool = True) -> None:
        self.props: dict[str, Any] = {}
        self.has_property = has_property

    def find_property(self, name: str) -> object | None:
        return object() if self.has_property else None

    def set_property(self, name: str, value: Any) -> None:
        self.props[name] = value


def test_signalling_server_binds_loopback() -> None:
    sink = Sink()
    assert app_bind._configure_webrtc(lambda server, pipeline: sink, object(), "pipeline") is sink
    assert sink.props == {"signalling-server-host": "127.0.0.1"}


def test_a_webrtcsink_without_the_host_property_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="signalling-server-host"):
        app_bind._configure_webrtc(lambda server, pipeline: Sink(has_property=False), object(), "pipeline")
