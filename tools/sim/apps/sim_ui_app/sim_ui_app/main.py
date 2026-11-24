"""Sim test app with a web UI whose server starts late.

Real apps bring their web server up 1-3 s after the process starts
(``docs/daemon-contract.md``, "Apps"), so the controller has to wait for the
UI to answer before sending the user there. This app holds its server back
for ``SIM_UI_DELAY`` seconds (default 3) to make that window easy to see.
The daemon reads the UI address from ``custom_app_url`` below without
importing this module. Like real apps, it asks for every interface; the
bridge runtime's app_bind shim keeps it on loopback (#186).

Like an app on reachy-mini >= 1.15, every request goes through the rules of
upstream's ``LocalNetworkGuardMiddleware`` (``reachy_mini/daemon/app/
middleware.py``, which ``ReachyMiniApp`` mounts on its settings UI), so the
sim catches a proxy that passes on a Host or Origin those apps refuse (#340).
The test apps don't install reachy-mini, so the rules are restated here.
``POST /echo`` and the WebSocket at ``/ws`` answer with the Host, Origin and
X-Forwarded-Host the app saw.
"""

import base64
import hashlib
import ipaddress
import json
import os
import struct
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

custom_app_url = "http://0.0.0.0:18742"
PORT = 18742

# --- upstream's LocalNetworkGuardMiddleware (reachy-mini 1.15.0) -----------
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOCAL_HOSTNAMES = frozenset({"localhost", "tauri.localhost"})
_WEBVIEW_SCHEMES = frozenset({"tauri", "capacitor"})


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_local_name(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host in _LOCAL_HOSTNAMES or host.endswith(".local")


def trusted_host(netloc: str | None) -> bool:
    """An IP literal, ``localhost`` or a ``.local`` name."""
    try:
        host = urlsplit(f"//{netloc}").hostname if netloc else None
    except ValueError:
        return False
    return bool(host) and (_parse_ip(host) is not None or _is_local_name(host))


def trusted_origin(origin: str) -> bool:
    """A local name, or a loopback, private or link-local address."""
    try:
        parts = urlsplit(origin)
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme in _WEBVIEW_SCHEMES:
        return host == "localhost"
    if parts.scheme not in ("http", "https") or not host:
        return False
    if _is_local_name(host):
        return True
    ip = _parse_ip(host)
    return ip is not None and (ip.is_loopback or ip.is_private or ip.is_link_local)


def refusal(method: str, hosts: list[str], origins: list[str], websocket: bool) -> tuple[int, str] | None:
    """The guard's answer, or None to let the request through. Upstream
    closes a refused WebSocket with 1008 before accepting it, which the
    client sees as an HTTP 403 to its handshake."""
    if len(hosts) > 1 or len(origins) > 1:
        return (403 if websocket else 400), "Duplicate Host or Origin header"
    if not trusted_host(hosts[0] if hosts else None):
        return (403 if websocket else 400), "Untrusted Host header"
    method = "POST" if websocket else method.upper()
    if method not in _SAFE_METHODS and origins and not trusted_origin(origins[0]):
        return 403, "Cross-origin request rejected"
    return None


# --- the app -------------------------------------------------------------
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class App:
    """Entry-point target; the daemon runs this module, not the class."""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # WebSocket clients refuse an HTTP/1.0 101

    def _guarded(self, method: str) -> bool:
        websocket = self.headers.get("Upgrade", "").lower() == "websocket"
        refused = refusal(method, self.headers.get_all("Host", []), self.headers.get_all("Origin", []), websocket)
        if refused:
            self._send(refused[0], "application/json", json.dumps({"detail": refused[1]}).encode())
        return refused is None

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _seen(self) -> bytes:
        return json.dumps(
            {
                "host": self.headers.get("Host"),
                "origin": self.headers.get("Origin"),
                "forwarded_host": self.headers.get("X-Forwarded-Host"),
            }
        ).encode()

    def do_GET(self) -> None:
        if not self._guarded("GET"):
            return
        if self.path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
            self._websocket()
            return
        body = b"<!doctype html><title>sim_ui_app</title><p>sim_ui_app is up</p>\n"
        self._send(200, "text/html", body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not self._guarded("POST"):
            return
        self._send(200, "application/json", self._seen())

    def _websocket(self) -> None:
        """Accept, send what the handshake carried as one text frame, close."""
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        seen = self._seen()  # well under 64 KiB: a 16-bit length at most
        length = struct.pack("!B", len(seen)) if len(seen) < 126 else struct.pack("!BH", 126, len(seen))
        self.wfile.write(b"\x81" + length + seen)  # text frame
        self.wfile.write(struct.pack("!BBH", 0x88, 2, 1000))  # close, normal
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    delay = float(os.getenv("SIM_UI_DELAY", "3"))
    print(f"sim_ui_app: UI server starts in {delay:g} s", flush=True)
    time.sleep(delay)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"sim_ui_app: serving on {custom_app_url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass  # the daemon stops apps with SIGINT
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
