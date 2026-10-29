"""Audio gateway (#219): the robot's mic and speaker over WebRTC, audio only.

One browser at a time listens to the robot mic and talks through the robot
speaker. The controller is the only client: it starts a session for the
device in control (#221), passes the browser's answer on, and ends the
session when control ends. This process listens on loopback only; media
goes over UDP straight between the browser and this host.

The gateway makes the offer (sendrecv Opus, all ICE candidates gathered, no
trickle) and the browser answers. Listening and talking are valves in the
pipeline, so switching either never renegotiates:

    robot mic -> valve(listen) -> opus -> webrtcbin -> browser
    browser -> webrtcbin -> opus -> valve(talk) -> robot speaker

ICE candidates are limited to ``AUDIO_GATEWAY_NETS`` (the tailnet and
loopback by default) in both directions, there's no STUN or TURN, and UDP
ports come from ``AUDIO_GATEWAY_PORTS`` so one firewall rule covers them.

HTTP API (loopback, JSON):

- ``GET /status``: ``{"session": {...} | null}``
- ``POST /session`` ``{"listen", "talk"}``: replaces any session; returns
  ``{"id", "offer"}``
- ``POST /session/<id>/answer`` ``{"sdp"}``
- ``POST /session/<id>/route`` ``{"listen", "talk"}``
- ``DELETE /session/<id>``
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import GLib, Gst, GstSdp, GstWebRTC  # noqa: E402

logger = logging.getLogger("reachy_bridge_runtime.audio_gateway")

PORT = int(os.environ.get("AUDIO_GATEWAY_PORT", "8002"))
SOURCE = os.environ.get("AUDIO_GATEWAY_SOURCE", "alsasrc device=reachymini_audio_src")
SINK = os.environ.get("AUDIO_GATEWAY_SINK", "alsasink device=reachymini_audio_sink sync=false")
# The tailnet (IPv4 CGNAT range and Tailscale's IPv6 prefix) and loopback.
NETS = [
    ipaddress.ip_network(n.strip())
    for n in os.environ.get("AUDIO_GATEWAY_NETS", "100.64.0.0/10,fd7a:115c:a1e0::/48,127.0.0.0/8,::1/128").split(",")
    if n.strip()
]
PORTS = os.environ.get("AUDIO_GATEWAY_PORTS", "47100-47199")
GATHER_TIMEOUT = 5.0
CALL_TIMEOUT = 10.0
# A session whose browser never answers, or whose ICE never connects, ends.
CONNECT_TIMEOUT = 30.0

REQUIRED = ["webrtcbin", "opusenc", "opusdec", "rtpopuspay", "rtpopusdepay", "valve", "audioconvert", "audioresample"]


def missing_elements() -> list[str]:
    return [name for name in REQUIRED if Gst.ElementFactory.find(name) is None]


def candidate_allowed(line: str) -> bool:
    """An ``a=candidate`` line whose address is in ``NETS``. Others (LAN,
    public, mDNS names) are dropped."""
    parts = line.split()
    if len(parts) < 6:
        return False
    try:
        address = ipaddress.ip_address(parts[4])
    except ValueError:
        return False
    return any(address in net for net in NETS)


def filter_candidates(sdp: str) -> str:
    lines = sdp.replace("\r\n", "\n").split("\n")
    kept = [ln for ln in lines if not ln.startswith("a=candidate:") or candidate_allowed(ln[2:])]
    return "\r\n".join(kept)


def on_main(func: Callable[[], Any]) -> Any:
    """Run ``func`` on the GLib main loop and wait for its result."""
    done = threading.Event()
    box: dict[str, Any] = {}

    def run() -> bool:
        try:
            box["value"] = func()
        except Exception as error:  # noqa: BLE001 - handed back to the caller
            box["error"] = error
        done.set()
        return False

    GLib.idle_add(run)
    if not done.wait(CALL_TIMEOUT):
        raise TimeoutError("the GLib loop didn't answer")
    if "error" in box:
        raise box["error"]
    return box.get("value")


class Session:
    """One browser's audio: a pipeline with a webrtcbin. Built and driven on
    the GLib main loop."""

    def __init__(self, listen: bool, talk: bool) -> None:
        self.id = secrets.token_hex(8)
        self.created = time.monotonic()
        # Last seen connected (or created): a session quiet this long ends.
        self.seen = self.created
        self.listen = listen
        self.talk = talk
        self.packets_in = 0
        self.last_in = 0.0
        self.pipeline = Gst.parse_launch(
            f"webrtcbin name=webrtc bundle-policy=max-bundle "
            f"{SOURCE} ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=1 "
            f"! valve name=listen drop={str(not listen).lower()} "
            f"! opusenc bitrate=32000 inband-fec=true ! rtpopuspay pt=111 "
            # Browsers only accept Opus offered as opus/48000/2.
            f"! application/x-rtp,media=audio,encoding-name=OPUS,payload=111,clock-rate=48000,encoding-params=(string)2 "
            f"! webrtc."
        )
        self.webrtc = self.pipeline.get_by_name("webrtc")
        low, _, high = PORTS.partition("-")
        # Kept for the session's life: PyGObject's wrapper for this
        # transfer-none object drops a reference it doesn't own when
        # collected, which crashes the pipeline later.
        self.ice = self.webrtc.get_property("ice-agent")
        if low and high:
            self.ice.set_property("min-rtp-port", int(low))
            self.ice.set_property("max-rtp-port", int(high))
        # The linked send pad makes a sendrecv transceiver; the browser's
        # audio comes back on it (pad-added).
        self.webrtc.connect("pad-added", self._on_pad)
        self.bus = self.pipeline.get_bus()
        bus = self.bus
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)
        self.gathered = threading.Event()
        self.webrtc.connect("notify::ice-gathering-state", self._on_gathering)
        self.offer: str | None = None
        self.answered = False

    def _on_error(self, _bus: Any, message: Gst.Message) -> None:
        error, debug = message.parse_error()
        logger.warning("Audio session %s: %s (%s)", self.id, error.message, debug)

    def _on_gathering(self, webrtc: Any, _pspec: Any) -> None:
        if webrtc.get_property("ice-gathering-state") == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            self.gathered.set()

    def _on_pad(self, _webrtc: Any, pad: Gst.Pad) -> None:
        """The browser's audio: depayload and play it, behind the talk valve."""
        if pad.get_direction() != Gst.PadDirection.SRC:
            return
        sink = Gst.parse_bin_from_description(
            f"rtpopusdepay ! opusdec ! valve name=talk drop={str(not self.talk).lower()} "
            f"! audioconvert ! audioresample ! {SINK}",
            True,
        )
        self.pipeline.add(sink)
        sink.sync_state_with_parent()
        pad.link(sink.get_static_pad("sink"))
        pad.add_probe(Gst.PadProbeType.BUFFER, self._count_in)

    def _count_in(self, _pad: Any, _info: Any) -> Gst.PadProbeReturn:
        self.packets_in += 1
        self.last_in = time.monotonic()
        return Gst.PadProbeReturn.OK

    def start(self) -> None:
        """Start the pipeline and create the offer (on the main loop)."""
        self.pipeline.set_state(Gst.State.PLAYING)

        def created(promise: Gst.Promise, _data: Any) -> None:
            reply = promise.get_reply()
            offer = reply.get_value("offer")
            self.webrtc.emit("set-local-description", offer, Gst.Promise.new())

        self.webrtc.emit("create-offer", None, Gst.Promise.new_with_change_func(created, None))

    def local_offer(self) -> str:
        description = self.webrtc.get_property("local-description")
        if description is None:
            raise RuntimeError("no offer yet")
        return filter_candidates(description.sdp.as_text())

    def answer(self, sdp: str) -> None:
        result, message = GstSdp.SDPMessage.new_from_text(filter_candidates(sdp))
        if result != GstSdp.SDPResult.OK:
            raise ValueError("unreadable answer")
        answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, message)
        self.webrtc.emit("set-remote-description", answer, Gst.Promise.new())
        self.answered = True

    def route(self, listen: bool, talk: bool) -> None:
        self.listen, self.talk = listen, talk
        self.pipeline.get_by_name("listen").set_property("drop", not listen)
        talk_valve = self.pipeline.get_by_name("talk")
        if talk_valve is not None:
            talk_valve.set_property("drop", not talk)

    def state(self) -> dict[str, Any]:
        ice = self.webrtc.get_property("ice-connection-state")
        connected = ice in (GstWebRTC.WebRTCICEConnectionState.CONNECTED, GstWebRTC.WebRTCICEConnectionState.COMPLETED)
        if connected:
            self.seen = time.monotonic()
        return {
            "id": self.id,
            "listen": self.listen,
            "talk": self.talk,
            "answered": self.answered,
            "connected": connected,
            "ice": ice.value_nick,
            "packets_in": self.packets_in,
            "age": round(time.monotonic() - self.created, 1),
            "quiet": round(time.monotonic() - self.seen, 1),
        }

    def stop(self) -> None:
        self.pipeline.set_state(Gst.State.NULL)
        # The watch holds this session (and its pipeline) alive otherwise.
        self.bus.remove_signal_watch()


class Gateway:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.session: Session | None = None

    def _end(self) -> None:
        if self.session is not None:
            logger.info("Audio session %s ended", self.session.id)
            session, self.session = self.session, None
            on_main(session.stop)

    def open(self, listen: bool, talk: bool) -> dict[str, Any]:
        with self.lock:
            self._end()
            session = on_main(lambda: Session(listen, talk))
            try:
                on_main(session.start)
                if not session.gathered.wait(GATHER_TIMEOUT):
                    logger.warning("Audio session %s: ICE gathering didn't finish; offering what we have", session.id)
                offer = on_main(session.local_offer)
            except Exception:
                # Don't leave its pipeline holding the mic, speaker and ports.
                on_main(session.stop)
                raise
            self.session = session
            logger.info("Audio session %s opened (listen=%s, talk=%s)", session.id, listen, talk)
            return {"id": session.id, "offer": offer}

    def with_session(self, sid: str, func: Callable[[Session], Any]) -> Any:
        with self.lock:
            if self.session is None or self.session.id != sid:
                raise KeyError(sid)
            session = self.session
            return on_main(lambda: func(session))

    def close(self, sid: str) -> None:
        with self.lock:
            if self.session is None or self.session.id != sid:
                raise KeyError(sid)
            self._end()

    def status(self) -> dict[str, Any]:
        with self.lock:
            session = self.session
            if session is None:
                return {"session": None}
            state = on_main(session.state)
            # Never connected, or its browser went away: give the port back.
            if not state["connected"] and state["quiet"] > CONNECT_TIMEOUT:
                logger.info("Audio session %s not connected for %ss; ending it", session.id, CONNECT_TIMEOUT)
                self._end()
                return {"session": None}
            return {"session": state}


GATEWAY = Gateway()


class Handler(BaseHTTPRequestHandler):
    server_version = "reachy-bridge-audio"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        logger.debug(format, *args)

    def _reply(self, status: int, body: Any = None) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            raise ValueError("body too large")
        value = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value

    def _route(self, method: str) -> None:
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        try:
            if method == "GET" and parts == ["status"]:
                return self._reply(HTTPStatus.OK, GATEWAY.status())
            if method == "POST" and parts == ["session"]:
                body = self._body()
                return self._reply(HTTPStatus.OK, GATEWAY.open(bool(body.get("listen", True)), bool(body.get("talk", False))))
            if len(parts) == 2 and parts[0] == "session" and method == "DELETE":
                GATEWAY.close(parts[1])
                return self._reply(HTTPStatus.NO_CONTENT)
            if len(parts) == 3 and parts[0] == "session" and method == "POST":
                body = self._body()
                if parts[2] == "answer":
                    sdp = body.get("sdp")
                    if not isinstance(sdp, str) or len(sdp) > 32768:
                        return self._reply(HTTPStatus.BAD_REQUEST, {"detail": "sdp is required"})
                    GATEWAY.with_session(parts[1], lambda s: s.answer(sdp))
                    return self._reply(HTTPStatus.NO_CONTENT)
                if parts[2] == "route":
                    listen, talk = bool(body.get("listen")), bool(body.get("talk"))
                    GATEWAY.with_session(parts[1], lambda s: s.route(listen, talk))
                    return self._reply(HTTPStatus.NO_CONTENT)
            return self._reply(HTTPStatus.NOT_FOUND, {"detail": "not found"})
        except KeyError:
            return self._reply(HTTPStatus.NOT_FOUND, {"detail": "no such session"})
        except (ValueError, json.JSONDecodeError) as error:
            return self._reply(HTTPStatus.BAD_REQUEST, {"detail": str(error)})
        except Exception as error:  # noqa: BLE001 - reported to the controller
            logger.exception("Audio gateway request failed")
            return self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": str(error)})

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._route("DELETE")


def reap() -> None:
    """End a disconnected session even when nobody asks for the status (the
    controller polls it, but may be gone)."""
    while True:
        time.sleep(5)
        try:
            GATEWAY.status()
        except Exception:
            logger.exception("Audio gateway: status check failed")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Gst.init(None)
    missing = missing_elements()
    if missing:
        raise SystemExit(f"audio gateway: missing GStreamer elements: {', '.join(missing)}")
    loop = GLib.MainLoop()
    threading.Thread(target=loop.run, name="glib", daemon=True).start()
    threading.Thread(target=reap, name="reap", daemon=True).start()
    # Loopback only: the controller is the only client.
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    logger.info("Audio gateway on 127.0.0.1:%d (ICE nets %s, UDP %s)", PORT, ",".join(map(str, NETS)), PORTS)
    try:
        server.serve_forever()
    finally:
        loop.quit()


if __name__ == "__main__":
    main()
