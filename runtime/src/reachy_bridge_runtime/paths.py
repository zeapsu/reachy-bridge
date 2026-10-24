"""Bridge state location, shared with the controller (``controller/src/state.rs``)."""

from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    if override := os.getenv("REACHY_BRIDGE_STATE_DIR"):
        return Path(override)
    base = os.getenv("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "reachy-bridge"
