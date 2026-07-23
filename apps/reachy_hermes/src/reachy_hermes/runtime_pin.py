"""Tracked identity and compatibility profile for Reachy's accepted Hermes runtime.

To update this pin, run the compatibility gate (including ``--live`` on the
robot host), copy its build identity and ``model_profile`` evidence here, and
commit the reviewed change with the gate result.
"""

from __future__ import annotations

from typing import Any


RUNTIME_PIN: dict[str, Any] = {
    "version": "0.19.0",
    # Recorded for the record and logged, but not enforced (only version and any
    # recorded SHA are). model_profile below is likewise documentation.
    "build_date": "2026.7.20",
    # Record these from the accepted robot-host binary when available. A None
    # value documents that the field has not been recorded and is not enforced.
    "upstream_sha": None,
    "local_sha": None,
    "plugins": ["reachy_probe"],
    "model_profile": {
        "provider": "unrecorded",
        "model": "unrecorded",
        "delivery_mode": "unrecorded",
    },
}


def describe() -> dict[str, Any]:
    """Return a logging-safe copy of the accepted runtime pin."""
    return {
        **RUNTIME_PIN,
        "plugins": list(RUNTIME_PIN["plugins"]),
        "model_profile": dict(RUNTIME_PIN["model_profile"]),
    }


__all__ = ["RUNTIME_PIN", "describe"]
