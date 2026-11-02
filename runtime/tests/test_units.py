"""What the daemon units hand to the runtime and to app installs."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

UNITS = Path(__file__).resolve().parents[2] / "deploy/systemd"


def environment(unit: str) -> dict[str, str]:
    """``Environment=`` assignments as systemd reads them: each line is split
    like a shell word list, so an unquoted value with spaces loses all but
    its first word."""
    env = {}
    for line in (UNITS / unit).read_text().splitlines():
        if line.startswith("Environment="):
            for word in shlex.split(line.removeprefix("Environment=")):
                key, sep, value = word.partition("=")
                if sep:
                    env[key] = value
    return env


def test_every_bridge_port_reaches_the_daemons() -> None:
    controller = environment("reachy-bridge-controller.service")
    expected = {
        int(controller["CONTROLLER_PORT"]),
        int(controller["CONTROLLER_APP_PORT"]),
        int(environment("reachy-bridge-audio.service")["AUDIO_GATEWAY_PORT"]),
        8443,  # webrtcsink's signalling server
    }
    for unit in ("reachy-bridge.service", "reachy-bridge-sim.service"):
        expected.add(int(re.search(r"--fastapi-port (\d+)", (UNITS / unit).read_text())[1]))
    for unit in ("reachy-bridge.service", "reachy-bridge-sim.service"):
        ports = {int(p) for p in environment(unit)["REACHY_BRIDGE_PORTS"].split()}
        assert ports == expected, unit


def test_app_installs_are_constrained() -> None:
    constraints = (UNITS.parent / "app-constraints.txt").read_text()
    assert "opencv-python<5" in constraints
    assert "opencv-python-headless<5" in constraints
    for unit in ("reachy-bridge.service", "reachy-bridge-sim.service"):
        assert environment(unit)["UV_CONSTRAINT"] == "%h/.local/share/reachy-bridge/app-constraints.txt"
