"""Send a sim app's daemon connections to the sim daemon (#218). Installed by reachy-bridge.

The bridge writes this file into the sim daemon's apps venv as
``reachy_bridge_sim_port.py``, with ``ROBOT_PORT`` and ``SIM_PORT`` set, and a
``.pth`` that imports it, so it runs at the start of every Python process that
venv runs. The SDK connects to the daemon on ``localhost:8000`` unless told
otherwise, which on the robot's host is the real robot's daemon: every TCP
connection to a loopback address on ``ROBOT_PORT`` goes to ``SIM_PORT``
instead.

It covers Python's ``socket`` module only (which the SDK, its HTTP clients
and asyncio go through). uvloop connects in native code, so it is kept out:
asyncio falls back to its own loop (uvicorn's "auto" loop does), and an app
that imports uvloop itself fails to start rather than reach the robot. Other
native code, other interpreters and subprocesses are not covered; that is
why the robot's daemon is stopped for the whole of sim mode, so nothing
listens on its port.

It must not import anything outside the standard library: it runs in the
apps venv, not the bridge's.
"""

import errno
import ipaddress
import socket
import sys

ROBOT_PORT = 8000
SIM_PORT = 8001


def _local(ip):
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


def _is_loopback(host):
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host == "":
        return True
    try:
        return _local(ipaddress.ip_address(host.split("%")[0]))
    except ValueError:
        pass
    # A name, or a form the kernel accepts but ipaddress doesn't ("127.1"):
    # resolve it the way connect will.
    try:
        infos = _getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            if _local(ipaddress.ip_address(str(info[4][0]).split("%")[0])):
                return True
        except ValueError:
            continue
    return False


def sim_address(family, address):
    """``address`` with the robot daemon's port swapped for the sim's.

    Any other host on that port is refused: the SDK falls back to
    ``reachy-mini.local:8000`` when localhost doesn't answer, which could be
    a robot on the network. So a sim app can't reach any service on that port
    off this host.
    """
    if family not in (socket.AF_INET, socket.AF_INET6):
        return address
    if not isinstance(address, tuple) or len(address) < 2:
        return address
    if address[1] != ROBOT_PORT:
        return address
    if not _is_loopback(address[0]):
        raise ConnectionRefusedError(f"sim mode: no robot daemon at {address[0]}:{ROBOT_PORT}")
    return (address[0], SIM_PORT, *address[2:])


_getaddrinfo = socket.getaddrinfo


def install():
    # uvloop connects natively, past the patch below.
    sys.modules.setdefault("uvloop", None)
    if getattr(socket.socket.connect, "_reachy_bridge_sim", False):
        return
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def sim_connect(self, address):
        return connect(self, sim_address(self.family, address))

    def sim_connect_ex(self, address):
        try:
            address = sim_address(self.family, address)
        except ConnectionRefusedError:
            return errno.ECONNREFUSED
        return connect_ex(self, address)

    sim_connect._reachy_bridge_sim = True  # type: ignore[attr-defined]
    socket.socket.connect = sim_connect
    socket.socket.connect_ex = sim_connect_ex


install()
