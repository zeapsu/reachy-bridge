"""Keep controller tests off the real state directory and system journal."""

from __future__ import annotations

from pathlib import Path

import pytest

import reachy_controller.journal_logs as journal_logs


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REACHY_BRIDGE_STATE_DIR", str(tmp_path))
    # The app lifespan would otherwise follow the host's real journal.
    monkeypatch.setattr(journal_logs.JournalFollower, "start", lambda _self: None)
