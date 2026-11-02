"""Audio gateway (#219): the robot's mic and speaker over WebRTC, audio only.

One browser at a time listens to the robot mic and talks through the robot
speaker. The controller is the only client: it starts a session for the
device in control (#221), passes the browser's answer on, and ends the
session when control ends. This process listens on loopback only; media
goes over UDP straight between the browser and this host.

The gateway makes the offer (sendrecv Opus, all ICE candidates gathered, no
trickle) and the browser answers. Listening, hearing and both kinds of
talking are valves in the pipeline, so switching any of them never
renegotiates:

    robot mic -> valve(listen) -+-> opus -> webrtcbin -> browser
    robot speaker's output -> valve(hear) -+
    browser -> webrtcbin -> opus -+-> valve(talk) -> robot speaker
                                  +-> valve(apps) -> what apps hear as the robot's mic

Hearing what the robot says and talking to its apps need the speaker and
mic tee (``audio_tee``: an ALSA loopback card). While hearing, the robot's
own speaker is muted, so the sound plays on one device; talking bypasses
both the tee and the mute; and listening is to the real mic, so the
browser doesn't hear itself.

ICE candidates are limited to UDP host candidates on ``AUDIO_GATEWAY_NETS``
(the tailnet by default) in both directions, there's no STUN or TURN, and
UDP ports come from ``AUDIO_GATEWAY_PORTS`` so one firewall rule covers them.

Only the controller can use it. The controller writes a fresh ``audio.key``
(0600) to its state dir when it starts, and the gateway reads it per
request (``audio_policy``). Every request carries ``X-Audio-Auth:
<seconds>.<nonce>.<HMAC(key, "request:<seconds>:<nonce>:<method>:<path>:<sha256(body)>")>``
(fresh within 30 s, each nonce once) and every reply
``X-Audio-Proof: HMAC(key, "reply:<nonce>")``. The key itself never goes
over the socket, so neither another local user, nor an app UI proxied by the
controller, nor something squatting this port can open the mic or pass for
the gateway. A session whose controller stops asking for its status ends.

HTTP API (loopback, JSON):

- ``GET /status``: ``{"session": {...} | null}``
- ``POST /session`` ``{"listen", "talk", "hear", "apps"}``: replaces any
  session; returns ``{"id", "offer"}``
- ``POST /session/<id>/answer`` ``{"sdp"}``
- ``POST /session/<id>/route`` ``{"listen", "talk", "hear", "apps"}``
- ``DELETE /session/<id>``
"""

from __future__ import annotations

import hashlib
import hmac
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

from . import audio_tee  # noqa: E402
from .audio_policy import AUTH_HEADER, KEY_FILE, NETS, NONCE, PROOF_HEADER, filter_candidates, fresh, sign  # noqa: E402

logger = logging.getLogger("reachy_bridge_runtime.audio_gateway")

PORT = int(os.environ.get("AUDIO_GATEWAY_PORT", "8002"))
_TEED = audio_tee.is_installed()
# The real mic: with the tee, apps' mic also carries the browser's voice.
_MIC = audio_tee.MIC if _TEED else audio_tee.SOURCE
SOURCE = os.environ.get("AUDIO_GATEWAY_SOURCE", f"alsasrc device={_MIC}")
# Talking skips the speaker tee: the browser shouldn't hear itself, and the
# speaker's mute is for the robot's own sounds.
_TALK = audio_tee.TALK_SINK if _TEED else audio_tee.SINK
SINK = os.environ.get("AUDIO_GATEWAY_SINK", f"alsasink device={_TALK} sync=false")
# What the robot plays, from the tee; empty without one.
HEAR = os.environ.get("AUDIO_GATEWAY_HEAR", f"alsasrc device={audio_tee.HEAR_SOURCE}" if _TEED else "")
NO_HEAR = "Hearing what Reachy says needs an ALSA loopback card (see reachy-bridge check)"
# Where the browser's voice goes to reach apps; empty without the tee.
APPS = os.environ.get("AUDIO_GATEWAY_APPS", f"alsasink device={audio_tee.APP_VOICE_SINK} sync=false" if _TEED else "")
NO_APPS = "Talking to Reachy's apps needs an ALSA loopback card (see reachy-bridge check)"
# A session whose controller hasn't asked for its status this long ends: the
# controller watches every session (it polls every 2 s) and may be gone.
UNWATCHED_TIMEOUT = 30.0

PORTS = os.environ.get("AUDIO_GATEWAY_PORTS", "47100-47199")
GATHER_TIMEOUT = 5.0
CALL_TIMEOUT = 10.0
# A session whose browser never answers, or whose ICE never connects, ends.
CONNECT_TIMEOUT = 30.0

# webrtcbin needs libnice's elements (Ubuntu: gstreamer1.0-nice) or refuses
# every stream, which only shows as "webrtc can't handle caps" when linking.
REQUIRED = [
    "webrtcbin",
    "nicesrc",
    "nicesink",
    "opusenc",
    "opusdec",
    "rtpopuspay",
    "rtpopusdepay",
    "valve",
    "audiomixer",
    "tee",
    "queue",
    "audioconvert",
    "audioresample",
]


def missing_elements() -> list[str]:
    return [name for name in REQUIRED if Gst.ElementFactory.find(name) is None]


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

    def __init__(self, listen: bool, talk: bool, hear: bool, apps: bool) -> None:
        self.id = secrets.token_hex(8)
        self.created = time.monotonic()
        # Last seen connected (or created): a session quiet this long ends.
        self.seen = self.created
        self.listen = listen
        self.talk = talk
        self.hear = hear
        self.apps = apps
        self.packets_in = 0
        self.last_in = 0.0
        caps = "audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=1"
        # A closed valve sends gaps, so the mixer doesn't wait for its branch.
        valve = "drop-mode=transform-to-gap"
        mic = f"{SOURCE} ! {caps} ! valve name=listen drop={str(not listen).lower()} {valve}"
        out = (
            f"{mic} ! mix. {HEAR} ! {caps} ! valve name=hear drop={str(not hear).lower()} {valve} ! mix. "
            f"audiomixer name=mix"
            if HEAR
            else mic
        )
        self.pipeline = Gst.parse_launch(
            f"webrtcbin name=webrtc bundle-policy=max-bundle "
            f"{out} "
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
        self.failed: str | None = None

    def _on_error(self, _bus: Any, message: Gst.Message) -> None:
        error, debug = message.parse_error()
        logger.warning("Audio session %s: %s (%s)", self.id, error.message, debug)
        # E.g. the mic or speaker busy: end it rather than show "live" in silence.
        self.failed = error.message

    def _on_gathering(self, webrtc: Any, _pspec: Any) -> None:
        if webrtc.get_property("ice-gathering-state") == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            self.gathered.set()

    def _on_pad(self, _webrtc: Any, pad: Gst.Pad) -> None:
        """The browser's audio: depayload it and play it on the speaker, and
        to apps, each behind its valve."""
        if pad.get_direction() != Gst.PadDirection.SRC:
            return
        # Closed valves send gaps: a sink that never got a buffer would hold
        # the other branch of the tee back.
        gaps = "drop-mode=transform-to-gap"
        talk = f"valve name=talk drop={str(not self.talk).lower()} {gaps} ! audioconvert ! audioresample ! {SINK}"
        apps = f"valve name=apps drop={str(not self.apps).lower()} {gaps} ! audioconvert ! audioresample ! {APPS}"
        sink = Gst.parse_bin_from_description(
            f"rtpopusdepay ! opusdec ! tee name=voice voice. ! queue ! {talk} voice. ! queue ! {apps}"
            if APPS
            else f"rtpopusdepay ! opusdec ! {talk}",
            True,
        )
        self.pipeline.add(sink)
        # A route that came while this was being built found no valves yet.
        for name, on in (("talk", self.talk), ("apps", self.apps)):
            valve = sink.get_by_name(name)
            if valve is not None:
                valve.set_property("drop", not on)
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

    def answer(self, sdp: str) -> Gst.Promise:
        """Apply the browser's answer (once); the caller waits on the promise
        off the main loop."""
        if self.answered:
            raise ValueError("this session already has an answer")
        result, message = GstSdp.SDPMessage.new_from_text(filter_candidates(sdp))
        if result != GstSdp.SDPResult.OK:
            raise ValueError("unreadable answer")
        answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, message)
        promise = Gst.Promise.new()
        self.webrtc.emit("set-remote-description", answer, promise)
        self.answered = True
        return promise

    def route(self, listen: bool, talk: bool, hear: bool, apps: bool) -> None:
        self.listen, self.talk, self.hear, self.apps = listen, talk, hear, apps
        self.pipeline.get_by_name("listen").set_property("drop", not listen)
        for name, on in (("hear", hear), ("apps", apps)):
            valve = self.pipeline.get_by_name(name)
            if valve is not None:
                valve.set_property("drop", not on)
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
            "hear": self.hear,
            "apps": self.apps,
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
        self.polled = time.monotonic()

    def _end(self, unmute: bool = True) -> None:
        if self.session is not None:
            logger.info("Audio session %s ended", self.session.id)
            session, self.session = self.session, None
            try:
                on_main(session.stop)
            finally:
                if unmute:
                    speaker(True)

    def open(self, listen: bool, talk: bool, hear: bool, apps: bool) -> dict[str, Any]:
        check(hear, apps)
        with self.lock:
            # The new session sets the speaker: don't let the robot speak up
            # between the two.
            try:
                self._end(unmute=False)
                session = on_main(lambda: Session(listen, talk, hear, apps))
            except Exception:
                speaker(True)
                raise
            try:
                on_main(session.start)
                if not session.gathered.wait(GATHER_TIMEOUT):
                    logger.warning("Audio session %s: ICE gathering didn't finish; offering what we have", session.id)
                offer = on_main(session.local_offer)
            except Exception:
                # Don't leave its pipeline holding the mic, speaker and ports.
                on_main(session.stop)
                speaker(True)
                raise
            self.session = session
            speaker(not hear)
            logger.info(
                "Audio session %s opened (listen=%s, talk=%s, hear=%s, apps=%s)", session.id, listen, talk, hear, apps
            )
            return {"id": session.id, "offer": offer}

    def route(self, sid: str, listen: bool, talk: bool, hear: bool, apps: bool) -> None:
        check(hear, apps)
        with self.lock:
            if self.session is None or self.session.id != sid:
                raise KeyError(sid)
            session = self.session
            on_main(lambda: session.route(listen, talk, hear, apps))
            speaker(not hear)
            logger.info("Audio session %s routed (listen=%s, talk=%s, hear=%s, apps=%s)", sid, listen, talk, hear, apps)

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

    def status(self, polled: bool = True) -> dict[str, Any]:
        """The session's state; `polled` is false for the gateway's own check."""
        with self.lock:
            now = time.monotonic()
            if polled:
                self.polled = now
            session = self.session
            if session is None:
                return {"session": None}
            state = on_main(session.state)
            if session.failed:
                logger.info("Audio session %s failed (%s); ending it", session.id, session.failed)
                self._end()
                return {"session": None}
            # Never connected, or its browser went away: give the port back.
            if not state["connected"] and state["quiet"] > CONNECT_TIMEOUT:
                logger.info("Audio session %s not connected for %ss; ending it", session.id, CONNECT_TIMEOUT)
                self._end()
                return {"session": None}
            # Nobody watches it: it would go on with no badge and no way to end it.
            if now - max(self.polled, session.created) > UNWATCHED_TIMEOUT:
                logger.info("Audio session %s: the controller stopped watching; ending it", session.id)
                self._end()
                return {"session": None}
            return {"session": state}


def check(hear: bool, apps: bool) -> None:
    """Refuse routes this device can't do."""
    if hear and not HEAR:
        raise ValueError(NO_HEAR)
    if apps and not APPS:
        raise ValueError(NO_APPS)


def speaker(on: bool) -> None:
    """The robot's speaker plays its own sounds unless a browser hears them."""
    if HEAR:
        audio_tee.set_speaker(on)


GATEWAY = Gateway()


class Handler(BaseHTTPRequestHandler):
    server_version = "reachy-bridge-audio"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        logger.debug(format, *args)

    proof = ""

    def _reply(self, status: int, body: Any = None) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if self.proof:
            self.send_header(PROOF_HEADER, self.proof)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    raw = b""

    def _body(self) -> dict[str, Any]:
        value = json.loads(self.raw or b"{}")
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value

    def _authorized(self) -> bool:
        """Check the request's signature (which covers its method, path and
        body), freshness and nonce; sign the reply with the same nonce."""
        self.proof = ""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            key = KEY_FILE.read_text().strip()
        except (OSError, ValueError):
            return False
        if not 0 <= length <= 65536:
            return False
        self.raw = self.rfile.read(length)
        parts = (self.headers.get(AUTH_HEADER) or "").split(".")
        if not key or len(parts) != 3 or not parts[0].isdigit() or not NONCE.fullmatch(parts[1]):
            return False
        seconds, nonce, mac = parts
        signed = f"{seconds}:{nonce}:{self.command}:{self.path}:{hashlib.sha256(self.raw).hexdigest()}"
        if not hmac.compare_digest(mac.encode(), sign(key, "request", signed).encode()):
            return False
        if not fresh(nonce, int(seconds)):
            return False
        self.proof = sign(key, "reply", nonce)
        return True

    def _route(self, method: str) -> None:
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if not self._authorized():
            return self._reply(HTTPStatus.FORBIDDEN, {"detail": "only the controller can use the audio gateway"})
        try:
            if method == "GET" and parts == ["status"]:
                return self._reply(HTTPStatus.OK, GATEWAY.status())
            if method == "POST" and parts == ["session"]:
                body = self._body()
                route = (
                    bool(body.get("listen", True)),
                    bool(body.get("talk")),
                    bool(body.get("hear")),
                    bool(body.get("apps")),
                )
                opened = GATEWAY.open(*route)
                return self._reply(HTTPStatus.OK, opened)
            if len(parts) == 2 and parts[0] == "session" and method == "DELETE":
                GATEWAY.close(parts[1])
                return self._reply(HTTPStatus.NO_CONTENT)
            if len(parts) == 3 and parts[0] == "session" and method == "POST":
                body = self._body()
                if parts[2] == "answer":
                    sdp = body.get("sdp")
                    if not isinstance(sdp, str) or len(sdp) > 32768:
                        return self._reply(HTTPStatus.BAD_REQUEST, {"detail": "sdp is required"})
                    promise = GATEWAY.with_session(parts[1], lambda s: s.answer(sdp))
                    if promise.wait() != Gst.PromiseResult.REPLIED:
                        raise ValueError("the answer wasn't applied")
                    reply = promise.get_reply()
                    if reply is not None and reply.has_field("error"):
                        raise ValueError(f"the answer was refused: {reply.get_value('error').message}")
                    return self._reply(HTTPStatus.NO_CONTENT)
                if parts[2] == "route":
                    route = (
                        bool(body.get("listen")),
                        bool(body.get("talk")),
                        bool(body.get("hear")),
                        bool(body.get("apps")),
                    )
                    GATEWAY.route(parts[1], *route)
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
            GATEWAY.status(polled=False)
        except Exception:
            logger.exception("Audio gateway: status check failed")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Gst.init(None)
    missing = missing_elements()
    if missing:
        hint = " (sudo apt install gstreamer1.0-nice)" if {"nicesrc", "nicesink"} & set(missing) else ""
        raise SystemExit(f"audio gateway: missing GStreamer elements: {', '.join(missing)}{hint}")
    # A gateway that stopped mid-session may have left the speaker muted.
    speaker(True)
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
