"""Audio gateway (#219): who may use it, and how sessions end.

The HTTP handler runs for real on a loopback port, with the gateway's
sessions faked: a real session needs the robot's ALSA devices. What's tested
is what keeps the mic closed to anyone but the controller (signed requests,
one-time nonces, signed replies) and what ends a session nobody can see.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

# It needs GStreamer's WebRTC typelib (gir1.2-gst-plugins-bad-1.0). CI
# installs it, so there a missing one fails rather than skips.
if os.environ.get("CI"):
    from reachy_bridge_runtime import audio_gateway as gateway
else:
    gateway = pytest.importorskip("reachy_bridge_runtime.audio_gateway", exc_type=ValueError)

from reachy_bridge_runtime import audio_policy  # noqa: E402

KEY = "k" * 64


class FakeGateway:
    """Records calls; stands in for GATEWAY behind the handler."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def status(self) -> dict[str, Any]:
        self.calls.append(("status",))
        return {"session": None}

    def open(self, *route: bool) -> dict[str, Any]:
        self.calls.append(("open", *route))
        if route[2]:
            raise ValueError(gateway.NO_HEAR)
        return {"id": "s1", "offer": "v=0"}

    def close(self, sid: str) -> None:
        self.calls.append(("close", sid))
        if sid != "s1":
            raise KeyError(sid)

    def route(self, sid: str, *route: bool) -> None:
        self.calls.append(("route", sid, *route))
        if sid != "s1":
            raise KeyError(sid)


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, FakeGateway]]:
    key_file = tmp_path / "audio.key"
    key_file.write_text(KEY + "\n")
    monkeypatch.setattr(gateway, "KEY_FILE", key_file)
    fake = FakeGateway()
    monkeypatch.setattr(gateway, "GATEWAY", fake)
    monkeypatch.setattr(audio_policy, "SEEN", {})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], fake
    finally:
        httpd.shutdown()
        httpd.server_close()


def auth(method: str, path: str, body: bytes, key: str = KEY, nonce: str | None = None, seconds: int | None = None) -> tuple[str, str]:
    """The controller's X-Audio-Auth header (audio.rs), and its nonce."""
    nonce = nonce or secrets.token_hex(16)
    seconds = int(time.time()) if seconds is None else seconds
    signed = f"{seconds}:{nonce}:{method}:{path}:{hashlib.sha256(body).hexdigest()}"
    return f"{seconds}.{nonce}.{audio_policy.sign(key, 'request', signed)}", nonce


def call(
    port: int,
    method: str,
    path: str,
    body: Any = None,
    header: str | None = None,
    signed_body: bytes | None = None,
) -> tuple[int, dict[str, str], Any]:
    data = b"" if body is None else json.dumps(body).encode()
    nonce = ""
    if header is None:
        header, nonce = auth(method, path, data if signed_body is None else signed_body)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Length": str(len(data))}
    if header:
        headers[audio_policy.AUTH_HEADER] = header
    conn.request(method, path, body=data, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    reply_headers = {k: v for k, v in response.getheaders()}
    reply_headers["_nonce"] = nonce
    conn.close()
    return response.status, reply_headers, json.loads(raw) if raw else None


def test_a_signed_request_gets_a_signed_reply(server: tuple[int, FakeGateway]) -> None:
    port, fake = server
    status, headers, body = call(port, "GET", "/status")
    assert status == 200
    assert body == {"session": None}
    assert headers[audio_policy.PROOF_HEADER] == audio_policy.sign(KEY, "reply", headers["_nonce"])
    assert fake.calls == [("status",)]


@pytest.mark.parametrize(
    "header",
    [
        "",  # none
        "garbage",
        "1.2.3",  # not a nonce
        f"{int(time.time())}.{'a' * 32}.{'0' * 64}",  # wrong mac
    ],
)
def test_unsigned_or_badly_signed_requests_are_refused(server: tuple[int, FakeGateway], header: str) -> None:
    port, fake = server
    status, headers, _ = call(port, "GET", "/status", header=header)
    assert status == 403
    assert audio_policy.PROOF_HEADER not in headers
    assert fake.calls == []


def test_a_request_signed_with_another_key_is_refused(server: tuple[int, FakeGateway]) -> None:
    port, fake = server
    header, _ = auth("GET", "/status", b"", key="x" * 64)
    assert call(port, "GET", "/status", header=header)[0] == 403
    assert fake.calls == []


def test_the_signature_covers_method_path_and_body(server: tuple[int, FakeGateway]) -> None:
    port, fake = server
    header, _ = auth("GET", "/status", b"")
    assert call(port, "DELETE", "/session/s1", header=header)[0] == 403
    header, _ = auth("POST", "/session", b"")
    assert call(port, "POST", "/session/s1/route", {"talk": True}, header=header)[0] == 403
    # The same path, another body: someone turning hearing on.
    status, _, _ = call(port, "POST", "/session", {"hear": True}, signed_body=json.dumps({}).encode())
    assert status == 403
    assert fake.calls == []


def test_a_nonce_works_once(server: tuple[int, FakeGateway]) -> None:
    port, fake = server
    header, _ = auth("GET", "/status", b"")
    assert call(port, "GET", "/status", header=header)[0] == 200
    assert call(port, "GET", "/status", header=header)[0] == 403
    assert fake.calls == [("status",)]


def test_a_stale_request_is_refused(server: tuple[int, FakeGateway]) -> None:
    port, _ = server
    header, _ = auth("GET", "/status", b"", seconds=int(time.time()) - audio_policy.SIGNED_WINDOW - 5)
    assert call(port, "GET", "/status", header=header)[0] == 403


def test_without_a_key_file_everything_is_refused(server: tuple[int, FakeGateway], tmp_path: Path) -> None:
    port, fake = server
    (tmp_path / "audio.key").unlink()
    assert call(port, "GET", "/status")[0] == 403
    (tmp_path / "audio.key").write_text("")
    header, _ = auth("GET", "/status", b"", key="")
    assert call(port, "GET", "/status", header=header)[0] == 403
    assert fake.calls == []


def test_an_oversized_body_is_refused_unread(server: tuple[int, FakeGateway]) -> None:
    port, fake = server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    header, _ = auth("POST", "/session", b"")
    conn.putrequest("POST", "/session")
    conn.putheader("Content-Length", str(10**6))
    conn.putheader(audio_policy.AUTH_HEADER, header)
    conn.endheaders()
    assert conn.getresponse().status == 403
    conn.close()
    assert fake.calls == []


def test_routes(server: tuple[int, FakeGateway]) -> None:
    port, fake = server
    # Listening is on unless the controller says otherwise; the rest are off.
    assert call(port, "POST", "/session", {})[2] == {"id": "s1", "offer": "v=0"}
    assert call(port, "POST", "/session/s1/route", {"talk": True})[0] == 204
    assert call(port, "DELETE", "/session/s1")[0] == 204
    assert fake.calls == [("open", True, False, False, False), ("route", "s1", False, True, False, False), ("close", "s1")]


def test_errors_map_to_statuses(server: tuple[int, FakeGateway]) -> None:
    port, _ = server
    assert call(port, "DELETE", "/session/nope")[0] == 404
    assert call(port, "POST", "/session/nope/route", {})[0] == 404
    status, _, body = call(port, "POST", "/session", {"hear": True})
    assert status == 400 and body["detail"] == gateway.NO_HEAR
    assert call(port, "POST", "/session/s1/answer", {"sdp": 5})[0] == 400
    assert call(port, "POST", "/session/s1/answer", {"sdp": "x" * 40000})[0] == 400
    assert call(port, "POST", "/session", [1])[0] == 400
    assert call(port, "GET", "/nothing")[0] == 404


class FakeSession:
    def __init__(self, listen: bool, talk: bool, hear: bool, apps: bool) -> None:
        self.id = secrets.token_hex(8)
        self.created = time.monotonic()
        self.route_args = (listen, talk, hear, apps)
        self.failed: str | None = None
        self.connected = False
        self.quiet = 0.0
        self.stopped = False
        self.gathered = threading.Event()
        self.gathered.set()

    def start(self) -> None:
        pass

    def local_offer(self) -> str:
        return "v=0"

    def state(self) -> dict[str, Any]:
        return {"id": self.id, "connected": self.connected, "quiet": self.quiet}

    def route(self, *route: bool) -> None:
        self.route_args = route

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[bool]]:
    """A real Gateway around fake sessions, run inline; records speaker calls."""
    monkeypatch.setattr(gateway, "Session", FakeSession)
    monkeypatch.setattr(gateway, "on_main", lambda func: func())
    speaker: list[bool] = []
    monkeypatch.setattr(gateway, "speaker", speaker.append)
    return gateway.Gateway(), speaker


def test_a_new_session_replaces_the_old_without_unmuting_between(gw: tuple[Any, list[bool]]) -> None:
    g, speaker = gw
    g.open(True, False, False, False)
    first = g.session
    g.open(True, True, False, False)
    assert first.stopped and g.session is not first
    assert speaker == [True, True]  # each open sets it once; the replace doesn't unmute first


def test_a_failed_open_unmutes_and_frees_the_pipeline(gw: tuple[Any, list[bool]], monkeypatch: pytest.MonkeyPatch) -> None:
    g, speaker = gw
    made: list[FakeSession] = []

    class Broken(FakeSession):
        def __init__(self, *route: bool) -> None:
            super().__init__(*route)
            made.append(self)

        def local_offer(self) -> str:
            raise RuntimeError("no offer")

    monkeypatch.setattr(gateway, "Session", Broken)
    with pytest.raises(RuntimeError):
        g.open(True, False, False, False)
    assert g.session is None
    assert made[0].stopped
    assert speaker == [True]


def test_hearing_and_apps_need_the_tee(gw: tuple[Any, list[bool]], monkeypatch: pytest.MonkeyPatch) -> None:
    g, _ = gw
    g.open(True, False, False, False)
    current = g.session
    monkeypatch.setattr(gateway, "HEAR", "")
    monkeypatch.setattr(gateway, "APPS", "")
    with pytest.raises(ValueError) as refused:
        g.open(True, False, True, False)
    assert str(refused.value) == gateway.NO_HEAR
    with pytest.raises(ValueError) as refused:
        g.open(True, False, False, True)
    assert str(refused.value) == gateway.NO_APPS
    # A refused route leaves the session there was alone.
    assert g.session is current and not current.stopped


def _end_by_close(g: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    g.close(g.session.id)


def _end_by_failure(g: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    g.session.failed = "ICE failed"
    assert g.status() == {"session": None}


def _end_unconnected(g: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    g.session.quiet = gateway.CONNECT_TIMEOUT + 1
    assert g.status() == {"session": None}


def _end_unwatched(g: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    g.session.connected = True
    later = time.monotonic() + gateway.UNWATCHED_TIMEOUT + 1
    monkeypatch.setattr(gateway.time, "monotonic", lambda: later)
    assert g.status(polled=False) == {"session": None}


@pytest.mark.parametrize("end", [_end_by_close, _end_by_failure, _end_unconnected, _end_unwatched])
def test_however_a_hearing_session_ends_the_speaker_comes_back(
    gw: tuple[Any, list[bool]], monkeypatch: pytest.MonkeyPatch, end: Callable[[Any, pytest.MonkeyPatch], None]
) -> None:
    g, speaker = gw
    monkeypatch.setattr(gateway, "HEAR", "alsasrc")
    g.open(True, False, True, False)
    session = g.session
    assert speaker == [False]  # the browser hears the robot instead
    end(g, monkeypatch)
    assert g.session is None and session.stopped
    assert speaker == [False, True]


def test_a_failed_session_ends_at_the_next_status(gw: tuple[Any, list[bool]]) -> None:
    g, speaker = gw
    g.open(True, False, False, False)
    g.session.failed = "ICE failed"
    session = g.session
    assert g.status() == {"session": None}
    assert session.stopped


def test_a_session_that_never_connects_ends(gw: tuple[Any, list[bool]]) -> None:
    g, _ = gw
    g.open(True, False, False, False)
    g.session.quiet = gateway.CONNECT_TIMEOUT - 1
    assert g.status()["session"] is not None
    g.session.quiet = gateway.CONNECT_TIMEOUT + 1
    assert g.status() == {"session": None}


def test_a_session_nobody_watches_ends(gw: tuple[Any, list[bool]], monkeypatch: pytest.MonkeyPatch) -> None:
    g, _ = gw
    g.open(True, False, False, False)
    g.session.connected = True
    now = time.monotonic()
    # The gateway's own check doesn't count as the controller watching.
    monkeypatch.setattr(gateway.time, "monotonic", lambda: now + gateway.UNWATCHED_TIMEOUT - 1)
    assert g.status(polled=False)["session"] is not None
    monkeypatch.setattr(gateway.time, "monotonic", lambda: now + gateway.UNWATCHED_TIMEOUT + 1)
    assert g.status(polled=False) == {"session": None}


def test_a_watched_session_stays(gw: tuple[Any, list[bool]], monkeypatch: pytest.MonkeyPatch) -> None:
    g, _ = gw
    g.open(True, False, False, False)
    g.session.connected = True
    start = time.monotonic()
    for step in range(1, 4):
        at = start + step * (gateway.UNWATCHED_TIMEOUT - 1)
        monkeypatch.setattr(gateway.time, "monotonic", lambda at=at: at)
        assert g.status()["session"] is not None


def test_only_the_current_session_can_be_driven(gw: tuple[Any, list[bool]]) -> None:
    g, _ = gw
    g.open(True, False, False, False)
    with pytest.raises(KeyError):
        g.route("other", True, True, False, False)
    with pytest.raises(KeyError):
        g.close("other")
    g.close(g.session.id)
    assert g.session is None
