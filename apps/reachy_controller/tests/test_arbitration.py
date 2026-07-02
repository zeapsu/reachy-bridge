"""Cross-surface arbitration: apps <-> expressions (#66) + motor-enable return-home (#67)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from reachy_controller.app_supervisor import AppSupervisor
from reachy_controller.daemon_proxy import DaemonProxy
from reachy_controller.main import create_app
from reachy_controller.routes.expressions import ExpressionPlayer


def _make_app(handler):
    proxy = DaemonProxy("http://daemon.local:8000", transport=httpx.MockTransport(handler))
    return create_app(proxy)


def test_any_app_active_states() -> None:
    supervisor = AppSupervisor()
    assert supervisor.any_app_active() is False
    for status, expected in (
        ("running", True),
        ("loading", True),
        ("stopped", False),
        ("stopping", False),
        ("error", False),
    ):
        supervisor._apps["reachy_chat"].status = status
        assert supervisor.any_app_active() is expected, status


def test_expression_player_active_property() -> None:
    player = ExpressionPlayer(AsyncMock())
    assert player.active is False
    player._session = {"uuid": None, "dataset": None, "move": None, "phase": "returning_home"}
    assert player.active is True
