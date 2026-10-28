"""Keep app servers on loopback (#186). Installed by reachy-bridge.

The bridge copies this file into the apps venv as
``reachy_bridge_app_bind.py`` with a ``.pth`` that imports it, so it runs
at the start of every Python process that venv runs: the apps and any Python
they spawn. A TCP socket bound to anything but loopback (``0.0.0.0``, ``::``,
``""``, ``"0"``, the LAN address, a host name) is bound to loopback instead.
App UIs then reach the network only through the controller's app proxy,
which asks for a login on the LAN.

It covers Python's ``socket`` module only: a listener opened by native code
(a C extension, GStreamer, a Node or ffmpeg child) is not moved.

It must not import anything outside the standard library: it runs in the
apps venv, not the bridge's.
"""

import ipaddress
import socket


def _is_loopback(host):
    if host == "localhost":
        return True
    try:
        numeric = socket.getaddrinfo(host, None, flags=socket.AI_NUMERICHOST)[0][4][0]
        ip = ipaddress.ip_address(str(numeric).split("%")[0])
    except (OSError, ValueError, IndexError):
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def loopback_address(family, kind, address, v6only=True):
    """The address to bind instead of ``address``, for a socket of ``family``/``kind``.

    A dual-stack IPv6 socket (``v6only`` off, Linux's default for ``::``)
    gets IPv4 loopback, so clients of ``127.0.0.1`` (the controller's proxy
    and readiness check) still reach it.
    """
    if kind != socket.SOCK_STREAM or family not in (socket.AF_INET, socket.AF_INET6):
        return address
    if not isinstance(address, tuple) or not address or not isinstance(address[0], str):
        return address
    if _is_loopback(address[0]):
        return address
    if family == socket.AF_INET:
        return ("127.0.0.1", *address[1:])
    return ("::1" if v6only else "::ffff:127.0.0.1", address[1], 0, 0)


def install():
    if getattr(socket.socket.bind, "_reachy_bridge", False):
        return
    bind = socket.socket.bind

    def loopback_bind(self, address):
        v6only = True
        if self.family == socket.AF_INET6:
            try:
                v6only = bool(self.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY))
            except OSError:
                pass
        return bind(self, loopback_address(self.family, self.type, address, v6only))

    loopback_bind._reachy_bridge = True  # type: ignore[attr-defined]
    socket.socket.bind = loopback_bind


install()
