"""Daemon journal follower: line handling and failure visibility."""

from __future__ import annotations

import asyncio
import sys

import pytest

import reachy_controller.journal_logs as journal_logs
from reachy_controller.journal_logs import SOURCE, JournalFollower
from reachy_controller.log_stream import LogBroker


def _stream(data: bytes, limit: int = 16) -> asyncio.StreamReader:
    stream = asyncio.StreamReader(limit=limit)
    stream.feed_data(data)
    stream.feed_eof()
    return stream


@pytest.mark.asyncio
async def test_overlong_line_is_dropped_whole_and_following_continues() -> None:
    broker = LogBroker()
    follower = JournalFollower(broker)
    await follower._pump(_stream(b"short\n" + b"x" * 40 + b"\nnext\ntail"), "")
    assert broker.ring(SOURCE) == [
        "short",
        f"[line over {journal_logs.MAX_LINE_BYTES} bytes dropped]",
        "next",
        "tail",
    ]


@pytest.mark.asyncio
async def test_journalctl_errors_are_shown_and_mark_the_source_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "import sys; print('No journal files were opened', file=sys.stderr); sys.exit(1)"

    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(*_args, **kwargs):
        return await real_exec(sys.executable, "-c", script, **kwargs)

    monkeypatch.setattr(journal_logs.shutil, "which", lambda _name: "/usr/bin/journalctl")
    monkeypatch.setattr(journal_logs.asyncio, "create_subprocess_exec", fake_exec)
    broker = LogBroker()
    follower = JournalFollower(broker)
    await follower._run()
    assert broker.ring(SOURCE) == ["journalctl: No journal files were opened"]
    assert broker.source_status(SOURCE) == "unavailable"
