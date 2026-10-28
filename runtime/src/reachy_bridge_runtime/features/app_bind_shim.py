"""Keep app servers on loopback (#186). Installed by reachy-bridge.

The bridge copies this file into the apps venv as
``reachy_bridge_app_bind.py`` with a ``.pth`` that imports it, so it runs
at the start of every app process. When ``REACHY_BRIDGE_APP_BIND`` is
``loopback`` (the bridge runtime sets it for the apps it starts), a TCP
socket bound to every interface (``0.0.0.0``, ``::`` or ``""``) is bound to
loopback instead. App UIs then reach the network only through the
controller's app proxy, which asks for a login on the LAN.

It must not import anything outside the standard library: it runs in the
apps venv, not the bridge's.
"""

import os
import socket

ENV = "REACHY_BRIDGE_APP_BIND"

_WILDCARD = {socket.AF_INET: ("", "0.0.0.0"), socket.AF_INET6: ("", "::", "::0")}
_LOOPBACK = {socket.AF_INET: "127.0.0.1", socket.AF_INET6: "::1"}


def loopback_address(family, kind, address):
    """The address to bind instead of ``address``, for a socket of ``family``/``kind``."""
    if kind != socket.SOCK_STREAM or not isinstance(address, tuple) or not address:
        return address
    if address[0] in _WILDCARD.get(family, ()):
        return (_LOOPBACK[family], *address[1:])
    return address


def install():
    if os.environ.get(ENV) != "loopback" or getattr(socket.socket.bind, "_reachy_bridge", False):
        return
    bind = socket.socket.bind

    def loopback_bind(self, address):
        return bind(self, loopback_address(self.family, self.type, address))

    loopback_bind._reachy_bridge = True  # type: ignore[attr-defined]
    socket.socket.bind = loopback_bind


install()
