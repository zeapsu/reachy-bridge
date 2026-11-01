"""Keep app servers on loopback (#186). Installed by reachy-bridge.

The bridge copies this file into the apps venv as
``reachy_bridge_app_bind.py`` with a ``.pth`` that imports it, so it runs
at the start of every Python process that venv runs: the apps and any Python
they spawn. A TCP socket bound to anything but loopback (``0.0.0.0``, ``::``,
``""``, ``"0"``, the LAN address, a host name) is bound to loopback instead,
except a specific address with port 0, which is how a client picks its
source address.
App UIs then reach the network only through the controller's app proxy,
which asks for a login on the LAN.

It covers Python's ``socket`` module only: a listener opened by native code
(a C extension, GStreamer, a Node or ffmpeg child) is not moved.

A TCP server on one of the bridge's own ports (``REACHY_BRIDGE_PORTS``,
set by the daemon's unit: the controller, the app proxy, the daemons, the
audio gateway and signalling) can never work, and an app that catches the
bind error runs on with no UI. So the process exits, with one line saying
why, which the app's card shows (#274):
- at once, if the bind would succeed (the bridge service isn't up, and the
  app would take its port);
- if the bind fails, after ``GRACE`` seconds unless the process has started
  listening on another port by then. Gradio and other port finders probe
  with a bind on each port in turn and move on; they stay up.

It must not import anything outside the standard library: it runs in the
apps venv, not the bridge's.
"""

import ipaddress
import os
import socket
import threading

PORTS_ENV = "REACHY_BRIDGE_PORTS"
GRACE = 10.0
REFUSED_EXIT = 78  # EX_CONFIG
REFUSED_PREFIX = "reachy-bridge: "


def bridge_ports(value=None):
    """The ports in ``REACHY_BRIDGE_PORTS`` (spaces or commas between them)."""
    if value is None:
        value = os.environ.get(PORTS_ENV, "")
    ports = set()
    for item in value.replace(",", " ").split():
        try:
            ports.add(int(item))
        except ValueError:
            continue
    return frozenset(ports)


def refused_port(family, kind, address, ports):
    """The bridge port a TCP bind of ``address`` would take, or None."""
    if kind != socket.SOCK_STREAM or family not in (socket.AF_INET, socket.AF_INET6):
        return None
    if not isinstance(address, tuple) or len(address) < 2:
        return None
    port = address[1]
    return port if isinstance(port, int) and port in ports else None


def refuse(port):
    try:
        message = f"{REFUSED_PREFIX}this app's UI wants port {port}, which the bridge uses\n"
        os.write(2, message.encode())
    finally:
        os._exit(REFUSED_EXIT)


def _is_wildcard(host):
    try:
        numeric = socket.getaddrinfo(host, None, flags=socket.AI_NUMERICHOST)[0][4][0]
        ip = ipaddress.ip_address(str(numeric).split("%")[0])
    except (OSError, ValueError, IndexError):
        return host == ""
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_unspecified


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
    and readiness check) still reach it. A specific address with port 0 is
    left alone: that is a client choosing its source address before
    ``connect``, which loopback would break.
    """
    if kind != socket.SOCK_STREAM or family not in (socket.AF_INET, socket.AF_INET6):
        return address
    if not isinstance(address, tuple) or not address:
        return address
    host = address[0]
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    if not isinstance(host, str):
        return address
    if _is_loopback(host):
        return address
    port = address[1] if len(address) > 1 else None
    if port == 0 and not _is_wildcard(host):
        return address
    if family == socket.AF_INET:
        return ("127.0.0.1", *address[1:])
    return ("::1" if v6only else "::ffff:127.0.0.1", address[1], 0, 0)


def install():
    if getattr(socket.socket.bind, "_reachy_bridge", False):
        return
    bind = socket.socket.bind
    listen = socket.socket.listen
    ports = bridge_ports()
    state = {"listening": False, "timer": None}

    def refuse_unless_listening(port):
        if not state["listening"]:
            refuse(port)

    def loopback_bind(self, address):
        port = refused_port(self.family, self.type, address, ports)
        if port is not None:
            try:
                bind(self, loopback_address(self.family, self.type, address, _v6only(self)))
            except OSError:
                if state["timer"] is None and not state["listening"]:
                    timer = threading.Timer(GRACE, refuse_unless_listening, (port,))
                    timer.daemon = True
                    state["timer"] = timer
                    timer.start()
                raise
            refuse(port)
        return bind(self, loopback_address(self.family, self.type, address, _v6only(self)))

    def tracking_listen(self, *args):
        result = listen(self, *args)
        if self.family in (socket.AF_INET, socket.AF_INET6):
            state["listening"] = True
            if state["timer"] is not None:
                state["timer"].cancel()
        return result

    loopback_bind._reachy_bridge = True  # type: ignore[attr-defined]
    socket.socket.bind = loopback_bind
    socket.socket.listen = tracking_listen


def _v6only(sock):
    if sock.family != socket.AF_INET6:
        return True
    try:
        return bool(sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY))
    except OSError:
        return True


install()
