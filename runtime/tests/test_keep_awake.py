"""Keep-awake on app stop: the motor mode matches the torque afterwards."""

from __future__ import annotations

import asyncio
import types
from typing import Any

from reachy_mini.daemon.backend.abstract import MotorControlMode

from reachy_bridge_runtime.features import keep_awake


class Backend:
    """The real robot backend's torque and mode bookkeeping."""

    def __init__(self, torque: bool | None, mode: MotorControlMode) -> None:
        if torque is not None:  # the simulators don't track torque
            self._torque_enabled = torque
        self.motor_control_mode = mode
        self.is_shutting_down = False
        self.modes: list[MotorControlMode] = []

    def set_motor_control_mode(self, mode: MotorControlMode) -> None:
        self.modes.append(mode)
        self.motor_control_mode = mode


def stop(backend: Backend, *, torque_after: bool | None) -> Backend:
    daemon = types.SimpleNamespace(backend=backend, robot_app_lock=None)
    manager = types.SimpleNamespace(daemon=daemon)

    async def original(manager: Any) -> None:
        # The app manager's return home: enable_motors() or limp in the sleep pose.
        if torque_after is not None:
            backend._torque_enabled = torque_after

    asyncio.run(keep_awake._stop_current_app(original, manager))
    return backend


def test_a_limp_app_returned_home_reports_the_motors_on() -> None:
    backend = stop(Backend(False, MotorControlMode.Disabled), torque_after=True)
    assert backend.modes == [MotorControlMode.Enabled]


def test_a_robot_left_limp_in_the_sleep_pose_stays_limp() -> None:
    backend = stop(Backend(False, MotorControlMode.Disabled), torque_after=False)
    assert backend.modes == []


def test_a_robot_that_was_on_is_left_alone() -> None:
    backend = stop(Backend(True, MotorControlMode.Enabled), torque_after=True)
    assert backend.modes == []


def test_the_simulators_are_left_alone() -> None:
    backend = stop(Backend(None, MotorControlMode.Disabled), torque_after=None)
    assert backend.modes == []
