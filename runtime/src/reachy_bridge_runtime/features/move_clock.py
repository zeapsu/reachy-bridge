"""Keep a move's clock inside the move, so power-on doesn't fail at random.

Upstream's move loops (``Backend.play_move``, which runs every goto, the
wake-up and the sleep move, and ``Backend.goto_joint_positions``) read the
wall clock twice per step::

    while time.time() - t0 < duration:
        t = time.time() - t0
        ... time_trajectory(t / duration) ...

``time_trajectory`` raises ``ValueError("time value is out of range [0,1]")``
outside [0, 1]. When the thread is switched out between the two reads near
the end of a move (the MuJoCo or motor-control thread holds the GIL for a
moment), ``t`` comes back past ``duration`` and the move raises instead of
finishing. During ``daemon/start?wake_up=true`` that leaves the daemon in
``error`` with the backend up and the wake-up move cut short: the dashboard
shows the robot off, and a person has to press power again (#318: the sim
browser test waited 60 s for a power-on that had already failed).

The move was due to end at that step anyway, so a time past the end is
clamped to it: the target pose. A negative time (only a wall clock stepped
back gives one) still raises as before; clamping it would hold the start
pose until the clock caught up, then replay the move.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from reachy_bridge_runtime import upstream

FEATURE = "move_clock"


def _clamped(original: Callable[..., float], t: float, *args: Any, **kwargs: Any) -> float:
    return original(min(t, 1.0), *args, **kwargs)


def apply() -> None:
    upstream.wrap_time_trajectory(_clamped)
