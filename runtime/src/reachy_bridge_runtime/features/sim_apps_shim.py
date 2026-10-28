"""Send a sim app's daemon connections to the sim daemon (#218). Installed by reachy-bridge.

The bridge writes this file into the sim daemon's apps venv as
``reachy_bridge_sim_port.py``, with ``ROBOT_PORT`` and ``SIM_PORT`` set, and a
``.pth`` that imports it, so it runs at the start of every Python process that
venv runs. The SDK connects to the daemon on ``localhost:8000`` unless told
otherwise, which on the robot's host is the real robot's daemon: every TCP
connection to a loopback address on ``ROBOT_PORT`` goes to ``SIM_PORT``
instead.

It covers Python's ``socket`` module only (which the SDK, its HTTP clients
and asyncio go through); a connection opened by native code is not moved.

It must not import anything outside the standard library: it runs in the
apps venv, not the bridge's.
"""

import ipaddress
import socket

ROBOT_PORT = 8000
SIM_PORT = 8001


def _is_loopback(host):
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host in ("localhost", ""):
        return True
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


def sim_address(family, address):
    """``address`` with the robot daemon's port swapped for the sim's."""
    if family not in (socket.AF_INET, socket.AF_INET6):
        return address
    if not isinstance(address, tuple) or len(address) < 2:
        return address
    if address[1] != ROBOT_PORT or not _is_loopback(address[0]):
        return address
    return (address[0], SIM_PORT, *address[2:])


def install():
    if getattr(socket.socket.connect, "_reachy_bridge_sim", False):
        return
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def sim_connect(self, address):
        return connect(self, sim_address(self.family, address))

    def sim_connect_ex(self, address):
        return connect_ex(self, sim_address(self.family, address))

    sim_connect._reachy_bridge_sim = True  # type: ignore[attr-defined]
    socket.socket.connect = sim_connect
    socket.socket.connect_ex = sim_connect_ex


install()
