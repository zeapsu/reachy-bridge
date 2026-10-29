"""Run the bridge runtime's daemon in MuJoCo sim, closer to the real robot.

    python tools/sim/sim_daemon.py --sim --headless --no-media ... (daemon args)

Two things the controller relies on behave differently in stock MuJoCo sim
than on a Reachy Mini Lite (``docs/daemon-contract.md``), which hides the
bugs the sim tests exist to catch. This launcher patches the sim backend, in
this process only, then runs ``reachy-bridge-runtime`` as usual:

- **Motor mode.** ``MujocoBackend`` always reports ``enabled`` and ignores
  ``set_motor_control_mode``. Here it keeps the mode it is set to and starts
  ``disabled``, like the real robot after a cold start (torque off). So
  ``daemon/start?wake_up=false`` comes up limp, and the idle reset's final
  ``disabled`` shows as asleep.
- **Idle reset.** The daemon schedules its idle reset (sleep after an app
  exits) on a backend event loop that only the media server creates, so a
  ``--no-media`` daemon never runs it. Here a backend without that loop gets
  one of its own, and the daemon's unmodified idle-reset code runs on it.

Only for sim tests: never run the real robot through this.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading


def _patch_motor_mode() -> None:
    from reachy_mini.daemon.backend.mujoco.backend import MujocoBackend
    from reachy_mini.io.protocol import MotorControlMode

    original_init = MujocoBackend.__init__

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        original_init(self, *args, **kwargs)
        self._sim_motor_mode = MotorControlMode.Disabled
        if getattr(self, "_log_loop", None) is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="sim-backend-loop", daemon=True).start()
            self._log_loop = loop

    def get_motor_control_mode(self) -> MotorControlMode:
        return self._sim_motor_mode

    def set_motor_control_mode(self, mode: MotorControlMode) -> None:
        self._sim_motor_mode = MotorControlMode(mode)

    MujocoBackend.__init__ = __init__
    MujocoBackend.get_motor_control_mode = get_motor_control_mode
    MujocoBackend.set_motor_control_mode = set_motor_control_mode


def main() -> int:
    # No usage telemetry from sim daemons (reachy-mini 1.13.0 sends it by
    # default), including ones started by hand.
    os.environ.setdefault("REACHY_MINI_TELEMETRY", "0")
    _patch_motor_mode()
    from reachy_bridge_runtime.main import main as runtime_main

    return runtime_main()


if __name__ == "__main__":
    sys.exit(main())
