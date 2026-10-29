"""Audio gateway (#219): which ICE candidates it accepts."""

import os

import pytest

pytest.importorskip("gi")
os.environ.setdefault("AUDIO_GATEWAY_KEY_FILE", "/nonexistent/audio.key")

from reachy_bridge_runtime.audio_gateway import candidate_allowed, filter_candidates  # noqa: E402


@pytest.mark.parametrize(
    ("line", "allowed"),
    [
        ("candidate:842163049 1 udp 1677729535 100.101.102.103 54400 typ host generation 0 network-cost 999", True),
        ("candidate:1 1 UDP 2015363327 100.64.1.2 47100 typ host", True),
        ("candidate:1 1 UDP 2015363327 fd7a:115c:a1e0::5 47100 typ host", True),
        ("candidate:1 1 UDP 2015363327 192.168.1.5 47100 typ host", False),
        ("candidate:1 1 UDP 2015363327 127.0.0.1 47100 typ host", False),
        ("candidate:1 1 TCP 2015363327 100.64.1.2 9 typ host tcptype active", False),
        ("candidate:1 1 udp 2015363327 100.64.1.2\t192.168.1.5 47100 typ host", False),
        ("candidate:1 1 udp 1 abc.local 47100 typ host", False),
        ("candidate:1 1 udp 1 100.64.1.2 47100 typ srflx raddr 0.0.0.0 rport 0", False),
    ],
)
def test_only_udp_host_candidates_on_the_tailnet(line: str, allowed: bool) -> None:
    assert candidate_allowed(line) is allowed


def test_candidate_lines_in_any_other_shape_are_dropped() -> None:
    sdp = (
        "v=0\r\n"
        " a=candidate:1 1 udp 1 192.168.1.5 1 typ host\r\n"
        "a=candidate:1 1 udp 1 100.64.1.2 2 typ host\r\n"
        "a=remote-candidates:1 192.168.1.5 1\r\n"
        "a=end-of-candidates\r\n"
    )
    assert filter_candidates(sdp) == "v=0\r\na=candidate:1 1 udp 1 100.64.1.2 2 typ host\r\na=end-of-candidates\r\n"


def test_a_nonce_is_good_once_and_only_while_fresh() -> None:
    import time

    from reachy_bridge_runtime.audio_gateway import fresh

    now = int(time.time())
    assert fresh("a" * 32, now)
    assert not fresh("a" * 32, now), "replayed"
    assert not fresh("b" * 32, now - 120), "stale"
