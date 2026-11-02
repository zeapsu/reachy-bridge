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


def ipv6_works() -> bool:
    """Whether this host can bind IPv6 loopback.

    ``socket.has_ipv6`` only says Python was built with it, and a host with
    IPv6 turned off by sysctl still opens IPv6 sockets but can't bind them.
    """
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
    except OSError:
        return False
    return True


def bound_host(
    site_packages: Path, host: str, family: int = socket.AF_INET, v6only: bool | None = None, port: int = 0
) -> str:
    """Where a fresh Python with the shim's site-packages binds ``host``."""
    code = (
        "import site, socket, sys\n"
        f"site.addsitedir({str(site_packages)!r})\n"
        f"s = socket.socket({int(family)}, socket.SOCK_STREAM)\n"
        + (f"s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, {int(v6only)})\n" if v6only is not None else "")
        + f"s.bind(({host!r}, {port}))\n"
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
        with socket.socket() as probe:  # a server port; port 0 on a specific address is a client's source
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        assert bound_host(site_packages, lan, port=port) == "127.0.0.1"
    # Loopback stays as asked.
    assert bound_host(site_packages, "127.0.0.1") == "127.0.0.1"
    assert bound_host(site_packages, "localhost") in ("127.0.0.1", "::1")
    if ipv6_works():
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
    assert shim.loopback_address(socket.AF_INET6, socket.SOCK_STREAM, ("::", 80), v6only=False) == (
        "::ffff:127.0.0.1",
        80,
        0,
        0,
    )
    assert shim.loopback_address(socket.AF_UNIX, socket.SOCK_STREAM, "/tmp/sock") == "/tmp/sock"
    assert shim.loopback_address(socket.AF_INET, socket.SOCK_STREAM, (b"0.0.0.0", 80)) == ("127.0.0.1", 80)
    # A client picking its source address before connect keeps it; a wildcard server on port 0 doesn't.
    assert shim.loopback_address(socket.AF_INET, socket.SOCK_STREAM, ("192.168.1.5", 0)) == ("192.168.1.5", 0)
    assert shim.loopback_address(socket.AF_INET, socket.SOCK_STREAM, ("0.0.0.0", 0)) == ("127.0.0.1", 0)
    assert shim.loopback_address(socket.AF_INET, socket.SOCK_STREAM, ("", 0)) == ("127.0.0.1", 0)


def run_with_shim(site_packages: Path, body: str, ports: str) -> subprocess.CompletedProcess[str]:
    code = (
        "import site, socket, time\n"
        f"site.addsitedir({str(site_packages)!r})\n"
        "import reachy_bridge_app_bind as shim\n"
        "shim.GRACE = 0.2\n" + body
    )
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        env={"REACHY_BRIDGE_PORTS": ports},
        timeout=30,
    )


def refusal(port: int) -> str:
    return f"reachy-bridge: this app's UI wants port {port}, which the bridge uses"


def test_shim_exits_an_app_stuck_on_a_bridge_port(tmp_path: Path) -> None:
    site_packages = app_bind.install_shim(fake_venv(tmp_path / "apps_venv"))
    with socket.socket() as held:  # the bridge, already serving on it
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        body = (
            "s = socket.socket()\n"
            "try:\n"
            f"    s.bind(('0.0.0.0', {port}))\n"
            "except OSError:\n"
            "    pass\n"  # an app that swallows the error still stops
            "time.sleep(2)\n"
            "print('still running')\n"
        )
        result = run_with_shim(site_packages, body, f"9, {port}")
    assert result.returncode == 78
    assert "still running" not in result.stdout
    assert result.stderr.strip().splitlines()[-1] == refusal(port)


def test_a_port_finder_that_moves_on_stays_up(tmp_path: Path) -> None:
    site_packages = app_bind.install_shim(fake_venv(tmp_path / "apps_venv"))
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        body = (
            "probe = socket.socket()\n"
            "try:\n"
            f"    probe.bind(('127.0.0.1', {port}))\n"
            "except OSError:\n"
            "    probe.close()\n"
            "server = socket.socket()\n"
            "server.bind(('127.0.0.1', 0))\n"
            "server.listen()\n"
            "time.sleep(1)\n"
            "print('still running')\n"
        )
        result = run_with_shim(site_packages, body, str(port))
    assert result.returncode == 0, result.stderr
    assert "still running" in result.stdout


def test_taking_a_free_bridge_port_exits_at_once(tmp_path: Path) -> None:
    site_packages = app_bind.install_shim(fake_venv(tmp_path / "apps_venv"))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    result = run_with_shim(
        site_packages, f"socket.socket().bind(('0.0.0.0', {port}))\nprint('still running')\n", str(port)
    )
    assert result.returncode == 78
    assert result.stderr.strip().splitlines()[-1] == refusal(port)
    # Without the variable nothing is refused.
    assert bound_host(site_packages, "0.0.0.0", port=port) == "127.0.0.1"


def test_shim_refuses_only_tcp_binds_of_bridge_ports() -> None:
    shim = types.ModuleType("shim")
    exec(app_bind.shim_source().replace("\ninstall()\n", "\n"), shim.__dict__)
    ports = shim.bridge_ports("7861,7863 8000 junk")
    assert ports == {7861, 7863, 8000}
    assert shim.refused_port(socket.AF_INET, socket.SOCK_STREAM, ("127.0.0.1", 7861), ports) == 7861
    assert shim.refused_port(socket.AF_INET6, socket.SOCK_STREAM, ("::", 8000, 0, 0), ports) == 8000
    assert shim.refused_port(socket.AF_INET, socket.SOCK_STREAM, ("0.0.0.0", 8042), ports) is None
    assert shim.refused_port(socket.AF_INET, socket.SOCK_DGRAM, ("0.0.0.0", 7861), ports) is None
    assert shim.refused_port(socket.AF_UNIX, socket.SOCK_STREAM, "/tmp/7861", ports) is None


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
