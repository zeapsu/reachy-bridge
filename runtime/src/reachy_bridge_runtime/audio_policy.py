"""Audio gateway policy (#219), apart from GStreamer so it can be tested
anywhere: which ICE candidates pass, and how the controller and the gateway
sign requests and replies to each other."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import threading
import time
from pathlib import Path

# The tailnet: IPv4 CGNAT range and Tailscale's IPv6 prefix.
NETS = [
    ipaddress.ip_network(n.strip())
    for n in os.environ.get("AUDIO_GATEWAY_NETS", "100.64.0.0/10,fd7a:115c:a1e0::/48").split(",")
    if n.strip()
]


def _key_file() -> Path:
    """The controller's ``audio.key``: in its state dir, found the same way."""
    configured = os.environ.get("AUDIO_GATEWAY_KEY_FILE")
    if configured:
        return Path(configured)
    state = os.environ.get("REACHY_BRIDGE_STATE_DIR")
    if not state:
        state = str(Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "reachy-bridge")
    return Path(state) / "audio.key"


KEY_FILE = _key_file()
AUTH_HEADER = "X-Audio-Auth"
PROOF_HEADER = "X-Audio-Proof"
NONCE = re.compile(r"[0-9a-f]{32}")
# A signed request is good this long, and its nonce only once.
SIGNED_WINDOW = 30
SEEN: dict[str, float] = {}
SEEN_LOCK = threading.Lock()


def fresh(nonce: str, seconds: int) -> bool:
    """The request is recent and its nonce unused; remember it."""
    now = time.time()
    if abs(now - seconds) > SIGNED_WINDOW:
        return False
    with SEEN_LOCK:
        for old in [n for n, at in SEEN.items() if now - at > 2 * SIGNED_WINDOW]:
            del SEEN[old]
        if nonce in SEEN:
            return False
        SEEN[nonce] = now
        return True


def sign(key: str, what: str, data: str) -> str:
    """HMAC-SHA256 of ``"<what>:<data>"``, in hex (the controller's ``audio::sign``)."""
    return hmac.new(key.encode(), f"{what}:{data}".encode(), hashlib.sha256).hexdigest()


# foundation component transport priority address port "typ" type [extensions],
# single spaces as libnice splits them.
CANDIDATE = re.compile(
    r"candidate:[!-~]{1,32} \d{1,3} (?i:udp) \d{1,10} ([0-9A-Fa-f.:]{2,45}) \d{1,5} typ host( [!-~]+ [!-~]+)*"
)


def candidate_allowed(line: str) -> bool:
    """A UDP host candidate (``candidate:...``, without ``a=``) whose
    address is in ``NETS``. Others (LAN, public, loopback, mDNS names, TCP,
    relayed) are dropped."""
    match = CANDIDATE.fullmatch(line)
    if not match:
        return False
    try:
        address = ipaddress.ip_address(match.group(1))
    except ValueError:
        return False
    return any(address in net for net in NETS)


def filter_candidates(sdp: str) -> str:
    """Keep only allowed candidates. A line that mentions a candidate in any
    other shape (leading space, ``a=remote-candidates``) is dropped too."""
    lines = sdp.replace("\r\n", "\n").split("\n")
    kept = [
        ln
        for ln in lines
        if "candidate" not in ln
        or ln == "a=end-of-candidates"
        or (ln.startswith("a=candidate:") and candidate_allowed(ln[2:]))
    ]
    return "\r\n".join(kept)
