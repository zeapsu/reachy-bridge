"""Edge-device adapters. A board is one module here: detection, its support
tier, and the host fixes and features it turns on (#140). Adding a board
means adding an adapter, not editing the runtime."""

from __future__ import annotations

import platform as _platform
from dataclasses import dataclass, field
from collections.abc import Callable


@dataclass(frozen=True)
class Platform:
    name: str
    tier: str  # "tested" or "experimental"
    # Host fixes run before the daemon starts (audio mixer, ~/.asoundrc).
    host_setup: tuple[Callable[[], None], ...] = ()
    # Optional features this board supports, by feature name.
    features: frozenset[str] = field(default_factory=frozenset)


def device_model() -> str:
    if _platform.system() != "Linux":
        return ""
    try:
        with open("/proc/device-tree/model") as model:
            return model.read()
    except OSError:
        return ""


def detect() -> Platform:
    from reachy_bridge_runtime.platforms import generic, jetson

    for adapter in (jetson,):
        if adapter.matches(device_model()):
            return adapter.PLATFORM
    return generic.PLATFORM
