"""Tests for AppSupervisor."""

from __future__ import annotations

import asyncio
import subprocess
import signal
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import psutil
import pytest

from reachy_controller.app_supervisor import (
    _APP_READINESS_POLL_S,
    _APP_READINESS_TIMEOUT_S,
    _STOP_SIGINT_GRACE_S,
    AppSupervisor,
    LocalApp,
    _signal_process_tree,
)


@pytest.mark.asyncio
async def test_start_app_success(tmp_path: Path) -> None:
    """Test successful app start."""
    supervisor = AppSupervisor()
    supervisor._workspace_root = tmp_path

    # Mock subprocess.Popen
    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process):
        result = await supervisor.start_app("reachy_chat")

    assert result["status"] == "loading"
    assert result["name"] == "reachy_chat"
    assert result["pid"] == 12345
    assert supervisor._apps["reachy_chat"].status == "loading"


@pytest.mark.asyncio
async def test_start_app_argv_direct_interpreter(tmp_path: Path) -> None:
    """start_app launches the app module with the shared interpreter directly."""
    supervisor = AppSupervisor()
    supervisor._workspace_root = tmp_path

    mock_process = MagicMock()
    mock_process.pid = 12345

    with (
        patch("reachy_controller.app_supervisor.psutil.process_iter", return_value=[]),
        patch(
            "reachy_controller.app_supervisor.subprocess.Popen",
            return_value=mock_process,
        ) as mock_popen,
    ):
        result = await supervisor.start_app("reachy_chat")

    assert result["status"] == "loading"
    argv = mock_popen.call_args.args[0]
    assert argv == [sys.executable, "-m", "reachy_chat.main"]
    assert "uv" not in argv
    assert "run" not in argv


@pytest.mark.asyncio
async def test_start_app_creates_log_file(tmp_path: Path) -> None:
    """Test that start_app creates a file and starts a merged-pipe pump."""
    supervisor = AppSupervisor()
    # Monkeypatch workspace root to temp directory
    supervisor._workspace_root = tmp_path

    # Mock subprocess.Popen
    mock_process = MagicMock()
    mock_process.pid = 12345
    popen_kwargs = {}

    def capture_popen(*args: object, **kwargs: object) -> MagicMock:
        """Capture Popen kwargs to inspect stdout."""
        popen_kwargs.update(kwargs)
        return mock_process

    with patch("reachy_controller.app_supervisor.subprocess.Popen", side_effect=capture_popen):
        result = await supervisor.start_app("reachy_chat")

    # Verify start succeeded (app begins in the loading state; the readiness
    # monitor promotes it to running later — see test_start_app_success)
    assert result["status"] == "loading"

    # Verify log file exists
    log_file = tmp_path / ".reachy-logs" / "reachy_chat.log"
    assert log_file.exists(), f"Log file not created at {log_file}"

    # The child output is drained from a merged line-buffered pipe.
    assert popen_kwargs.get("stdout") == subprocess.PIPE
    assert popen_kwargs["stderr"] == subprocess.STDOUT
    assert popen_kwargs["text"] is True
    assert popen_kwargs["bufsize"] == 1

    # The pump owns the file lifecycle; fake processes may finish immediately.
    app = supervisor._apps["reachy_chat"]
    assert app.log_pump is not None
    if app.log_pump:
        app.log_pump.stop()
    # Close the handle to avoid resource leak in test
    if app.log_handle:
        app.log_handle.close()


@pytest.mark.asyncio
async def test_stop_app_closes_log_pump(tmp_path: Path) -> None:
    """Test that stop_app joins and clears the output pump."""
    supervisor = AppSupervisor()
    supervisor._workspace_root = tmp_path

    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process):
        await supervisor.start_app("reachy_chat")

    app = supervisor._apps["reachy_chat"]
    assert app.log_pump is not None

    with patch("reachy_controller.app_supervisor._signal_process_tree"):
        result = await supervisor.stop_app("reachy_chat")

    assert result["status"] == "stopped"
    assert app.log_pump is None
    assert app.log_handle is None


@pytest.mark.asyncio
async def test_start_app_unknown_app() -> None:
    """Test start with unknown app name."""
    supervisor = AppSupervisor()
    result = await supervisor.start_app("unknown_app")
    assert result.get("status") == 404
    assert "Unknown app" in result.get("error", "")


@pytest.mark.asyncio
async def test_single_app_lock(tmp_path: Path) -> None:
    """Test that only one app can run at a time."""
    supervisor = AppSupervisor()
    supervisor._workspace_root = tmp_path

    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process):
        # Start first app
        result1 = await supervisor.start_app("reachy_chat")
        assert result1["status"] == "loading"

        # Try to start second app
        result2 = await supervisor.start_app("reachy_dance")
        assert result2["status"] == "refused"
        assert "already running" in result2.get("error", "")


@pytest.mark.asyncio
async def test_stop_app() -> None:
    """Test stopping an app."""
    supervisor = AppSupervisor()

    mock_process = MagicMock()
    mock_process.pid = 12345

    with (
        patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process),
        patch("reachy_controller.app_supervisor._signal_process_tree"),
    ):
        result_start = await supervisor.start_app("reachy_chat")
        assert result_start["status"] == "loading"
        result = await supervisor.stop_app("reachy_chat")

    assert result["status"] == "stopped"
    assert supervisor._apps["reachy_chat"].status == "stopped"


@pytest.mark.asyncio
async def test_stop_app_signals_process_tree() -> None:
    """stop_app signals the whole process tree, not just the Popen handle.

    Regression for #26: launched apps may spawn children during operation, and
    those descendants must receive shutdown signals along with the direct app
    process so graceful return-home + sleep cues can complete.
    """
    supervisor = AppSupervisor()

    mock_process = MagicMock()
    mock_process.pid = 12345
    mock_process.wait.return_value = 0  # exits cleanly on SIGINT (no escalation)

    supervisor._apps["reachy_chat"].process = mock_process
    supervisor._apps["reachy_chat"].status = "running"

    with patch("reachy_controller.app_supervisor._signal_process_tree") as mock_sig:
        result = await supervisor.stop_app("reachy_chat")

    assert result["status"] == "stopped"
    mock_sig.assert_called_once_with(12345, signal.SIGINT)


def test_signal_process_tree_signals_parent_and_children() -> None:
    """_signal_process_tree signals the parent AND every descendant.

    This is the core of the #26 stop behavior: a launched app can create child
    processes, and both the parent app process and descendants must receive the signal.
    """
    child_a = MagicMock()
    child_b = MagicMock()
    parent = MagicMock()
    parent.children.return_value = [child_a, child_b]

    with patch("reachy_controller.app_supervisor.psutil.Process", return_value=parent):
        _signal_process_tree(4242, signal.SIGINT)

    parent.children.assert_called_once_with(recursive=True)
    parent.send_signal.assert_called_once_with(signal.SIGINT)
    child_a.send_signal.assert_called_once_with(signal.SIGINT)
    child_b.send_signal.assert_called_once_with(signal.SIGINT)


def test_signal_process_tree_missing_process_is_noop() -> None:
    """_signal_process_tree is a no-op (no raise) when the pid is already gone."""
    with patch(
        "reachy_controller.app_supervisor.psutil.Process",
        side_effect=psutil.NoSuchProcess(4242),
    ):
        _signal_process_tree(4242, signal.SIGINT)  # must not raise


@pytest.mark.asyncio
async def test_start_app_sets_unbuffered_env(tmp_path: Path) -> None:
    """Launched apps get PYTHONUNBUFFERED=1 so their log file flushes line-by-line.

    Without this, buffered child stdout is lost on a non-clean exit, making the
    app log untrustworthy when diagnosing #26.
    """
    supervisor = AppSupervisor()
    supervisor._workspace_root = tmp_path

    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch(
        "reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process
    ) as mock_popen:
        await supervisor.start_app("reachy_chat")

    env = mock_popen.call_args.kwargs["env"]
    assert env["PYTHONUNBUFFERED"] == "1"


@pytest.mark.asyncio
async def test_stop_app_unknown() -> None:
    """Test stop with unknown app name."""
    supervisor = AppSupervisor()
    result = await supervisor.stop_app("unknown_app")
    assert result.get("status") == 404


@pytest.mark.asyncio
async def test_daemon_lock_check(tmp_path: Path) -> None:
    """Test that start is refused if daemon reports lock held."""
    mock_proxy = AsyncMock()
    mock_proxy.get_robot_app_lock_status = AsyncMock(
        return_value={"state": "local_app", "holder_name": "other_app"}
    )
    supervisor = AppSupervisor(daemon_proxy=mock_proxy)
    supervisor._workspace_root = tmp_path

    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process):
        result = await supervisor.start_app("reachy_chat")

    assert result["status"] == "refused"
    assert "Robot held by" in result.get("error", "")
    mock_process.assert_not_called()


@pytest.mark.asyncio
async def test_start_refused_when_daemon_unreachable(tmp_path: Path) -> None:
    """Start is refused (not spawned into a guaranteed error) when the daemon is offline."""
    mock_proxy = AsyncMock()
    mock_proxy.get_robot_app_lock_status = AsyncMock(return_value=None)
    supervisor = AppSupervisor(daemon_proxy=mock_proxy)
    supervisor._workspace_root = tmp_path

    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process):
        result = await supervisor.start_app("reachy_chat")

    assert result["status"] == "refused"
    assert "daemon is offline" in result.get("error", "")
    mock_process.assert_not_called()


@pytest.mark.asyncio
async def test_get_all_status() -> None:
    """Test getting status for all apps."""
    supervisor = AppSupervisor()
    status = supervisor.get_all_status()

    assert len(status) == 4
    assert all(s["status"] == "stopped" for s in status)
    assert set(s["name"] for s in status) == {
        "reachy_chat",
        "reachy_dance",
        "reachy_lookat",
        "reachy_simon",
    }


@pytest.mark.asyncio
async def test_get_status_single() -> None:
    """Test getting status for one app."""
    supervisor = AppSupervisor()
    status = supervisor.get_status("reachy_chat")

    assert status is not None
    assert status["name"] == "reachy_chat"
    assert status["status"] == "stopped"


def test_get_status_unknown() -> None:
    """Test getting status for unknown app."""
    supervisor = AppSupervisor()
    status = supervisor.get_status("unknown_app")
    assert status is None


def test_stop_all() -> None:
    """Test stopping all apps."""
    supervisor = AppSupervisor()

    # Manually set up mock processes for two apps
    mock_process1 = MagicMock()
    mock_process2 = MagicMock()
    supervisor._apps["reachy_chat"].process = mock_process1
    supervisor._apps["reachy_chat"].status = "running"
    supervisor._apps["reachy_dance"].process = mock_process2
    supervisor._apps["reachy_dance"].status = "running"

    supervisor.stop_all()

    assert supervisor._apps["reachy_chat"].status == "stopped"
    assert supervisor._apps["reachy_dance"].status == "stopped"
    mock_process1.terminate.assert_called_once()
    mock_process2.terminate.assert_called_once()


def test_local_app_to_dict() -> None:
    """Test LocalApp.to_dict() serialization."""
    app = LocalApp(
        name="reachy_chat",
        module="reachy_chat",
        pid=12345,
        status="running",
        error=None,
        start_time=1234567890.0,
    )
    data = app.to_dict()

    assert data["name"] == "reachy_chat"
    assert data["pid"] == 12345
    assert data["status"] == "running"
    assert data["error"] is None


@pytest.mark.asyncio
async def test_reap_app_uses_thread_for_wait() -> None:
    """Test that _reap_app uses asyncio.to_thread for wait() call.

    This ensures the event loop is not blocked while waiting for the child.
    We verify this by checking that the reaper task completes quickly even
    with a mocked slow wait() when wrapped in asyncio.to_thread.
    """
    supervisor = AppSupervisor()

    # Create a mock process with a wait that records it was called
    mock_process = MagicMock()
    mock_process.pid = 12345
    wait_called = False

    def slow_wait():
        """Mock wait that returns quickly when called in a thread."""
        nonlocal wait_called
        wait_called = True
        return 0  # Exit code

    mock_process.wait = slow_wait

    # Manually set up the app state
    supervisor._apps["reachy_chat"].process = mock_process
    supervisor._apps["reachy_chat"].status = "running"

    # Run the reaper - should complete without blocking
    reaper_task = asyncio.create_task(supervisor._reap_app("reachy_chat"))

    # Give the reaper time to complete
    await asyncio.wait_for(reaper_task, timeout=5.0)

    # Verify wait was called and app transitioned to stopped
    assert wait_called, "Process.wait() was not called"
    assert supervisor._apps["reachy_chat"].status == "stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code,expected_status", [(0, "stopped"), (7, "error")])
async def test_reaper_finally_cleans_all_state_before_restart(
    tmp_path: Path, exit_code: int, expected_status: str
) -> None:
    supervisor = AppSupervisor()
    app = supervisor._apps["reachy_chat"]

    async def reap_once(process: MagicMock, code: int, suffix: str) -> None:
        process.wait.return_value = code
        app.process = process
        app.pid = process.pid
        app.status = "loading"
        app.error = None
        app.external = False
        pump = MagicMock()
        app.log_pump = pump
        app.log_handle = MagicMock()
        app.ready_file = tmp_path / f"{suffix}.ready"
        app.ready_file.write_text("ready")
        task = asyncio.create_task(supervisor._reap_app(app.name))
        supervisor._reaper_tasks[app.name] = task
        await asyncio.wait_for(task, timeout=1)
        assert app.process is None
        assert app.pid is None
        assert app.log_pump is None
        assert app.log_handle is None
        assert app.ready_file is None
        assert not (tmp_path / f"{suffix}.ready").exists()
        assert app.name not in supervisor._reaper_tasks
        pump.stop.assert_called_once()

    await reap_once(MagicMock(pid=12345), exit_code, "first")
    assert app.status == expected_status

    await reap_once(MagicMock(pid=12346), 0, "replacement")
    assert app.status == "stopped"


@pytest.mark.asyncio
async def test_reaper_finally_cleans_after_wait_exception(tmp_path: Path) -> None:
    supervisor = AppSupervisor()
    app = supervisor._apps["reachy_chat"]
    process = MagicMock(pid=12347)
    process.wait.side_effect = RuntimeError("wait failed")
    pump = MagicMock()
    ready_file = tmp_path / "exception.ready"
    ready_file.write_text("ready")
    app.process = process
    app.pid = process.pid
    app.status = "loading"
    app.log_pump = pump
    app.log_handle = MagicMock()
    app.ready_file = ready_file

    task = asyncio.create_task(supervisor._reap_app(app.name))
    supervisor._reaper_tasks[app.name] = task
    await asyncio.wait_for(task, timeout=1)

    assert app.status == "error"
    assert app.process is None
    assert app.pid is None
    assert app.log_pump is None
    assert app.log_handle is None
    assert app.ready_file is None
    assert not ready_file.exists()
    assert app.name not in supervisor._reaper_tasks
    pump.stop.assert_called_once()


@pytest.mark.asyncio
async def test_stop_app_uses_thread_for_wait() -> None:
    """Test that stop_app uses asyncio.to_thread for wait() calls.

    This ensures the event loop is not blocked during shutdown sequence.
    """
    supervisor = AppSupervisor()

    # Create a mock process with signal/wait tracking
    mock_process = MagicMock()
    mock_process.pid = 12345
    wait_called = False
    wait_timeouts = []

    def quick_wait(timeout):
        """Mock wait that succeeds immediately."""
        nonlocal wait_called
        wait_called = True
        wait_timeouts.append(timeout)
        return 0  # Exit code

    mock_process.wait = quick_wait

    # Manually set up the app state
    supervisor._apps["reachy_chat"].process = mock_process
    supervisor._apps["reachy_chat"].status = "running"

    # Call stop_app - should complete without blocking the event loop
    with patch("reachy_controller.app_supervisor._signal_process_tree") as mock_sig:
        result = await supervisor.stop_app("reachy_chat")

    # Verify the stop completed and signals were sent
    assert result["status"] == "stopped"
    assert wait_called, "Process.wait() was not called"
    assert wait_timeouts == [_STOP_SIGINT_GRACE_S]
    assert supervisor._apps["reachy_chat"].status == "stopped"
    mock_sig.assert_called_once_with(12345, signal.SIGINT)


@pytest.mark.asyncio
async def test_stop_adopted_orphan() -> None:
    """Test stopping an adopted orphaned process (pid set, process=None).

    This simulates a process that survived from a previous run and was
    adopted by reconcile_orphans(). The supervisor must kill it even though
    it has no subprocess.Popen handle.
    """
    supervisor = AppSupervisor()

    # Mock psutil.Process for the orphaned process
    mock_psutil_proc = MagicMock()
    mock_psutil_proc.pid = 99999
    mock_psutil_proc.cmdline.return_value = [
        "/path/to/.venv/bin/python",
        "-m",
        "reachy_lookat.main",
    ]
    wait_timeouts = []

    def quick_wait(timeout):
        """Mock wait that succeeds immediately."""
        wait_timeouts.append(timeout)
        return 0  # Exit code

    mock_psutil_proc.wait = quick_wait

    # Manually set up the adopted app state (process=None, pid set, status=running)
    supervisor._apps["reachy_lookat"].process = None
    supervisor._apps["reachy_lookat"].pid = 99999
    supervisor._apps["reachy_lookat"].status = "running"

    with (
        patch("reachy_controller.app_supervisor.psutil.Process", return_value=mock_psutil_proc),
        patch("reachy_controller.app_supervisor._signal_process_tree") as mock_sig,
    ):
        result = await supervisor.stop_app("reachy_lookat")

    assert result["status"] == "stopped"
    assert supervisor._apps["reachy_lookat"].status == "stopped"
    assert supervisor._apps["reachy_lookat"].pid is None
    assert wait_timeouts == [_STOP_SIGINT_GRACE_S]
    # Signal delivered to the whole tree, not just app.pid
    mock_sig.assert_called_once_with(99999, signal.SIGINT)


def test_get_all_status_detects_dead_process() -> None:
    """Test that get_all_status() detects and transitions dead processes to stopped."""
    supervisor = AppSupervisor()

    # Manually set up a "running" app with a PID that doesn't exist
    supervisor._apps["reachy_dance"].pid = 99999
    supervisor._apps["reachy_dance"].status = "running"

    # Mock psutil.Process to raise NoSuchProcess
    with patch(
        "reachy_controller.app_supervisor.psutil.Process",
        side_effect=psutil.NoSuchProcess(99999),
    ):
        status = supervisor.get_all_status()

    # Find the reachy_dance entry in the returned status
    dance_status = next(s for s in status if s["name"] == "reachy_dance")

    # Verify it transitioned to stopped
    assert dance_status["status"] == "stopped"
    assert dance_status["pid"] is None
    assert supervisor._apps["reachy_dance"].status == "stopped"


@pytest.mark.asyncio
async def test_stop_adopted_orphan_with_escalation() -> None:
    """Test escalation path for adopted orphan that ignores SIGINT and SIGTERM.

    Regression for two bugs: (1) psutil.TimeoutExpired must be caught (not
    subprocess.TimeoutExpired) so escalation actually runs; (2) #26 — the signal
    must reach the whole process tree because apps can spawn children.
    When the process ignores SIGINT/SIGTERM, proc.wait() raises psutil.TimeoutExpired,
    escalating SIGINT -> SIGTERM -> SIGKILL, all delivered via _signal_process_tree.
    """
    supervisor = AppSupervisor()

    # Mock psutil.Process for the orphaned process
    mock_psutil_proc = MagicMock()
    mock_psutil_proc.pid = 88888
    mock_psutil_proc.cmdline.return_value = [
        "/path/to/.venv/bin/python",
        "-m",
        "reachy_simon.main",
    ]

    wait_call_count = [0]  # Use list to allow modification in nested function

    def wait_with_timeout(timeout):
        """Simulate process ignoring SIGINT and SIGTERM, then dying on SIGKILL."""
        # First two calls (after SIGINT and SIGTERM) raise TimeoutExpired;
        # third call (after SIGKILL) succeeds.
        wait_call_count[0] += 1
        if wait_call_count[0] <= 2:
            raise psutil.TimeoutExpired(88888)
        return 0  # Exit code after SIGKILL

    mock_psutil_proc.wait = wait_with_timeout

    # Manually set up the adopted app state
    supervisor._apps["reachy_simon"].process = None
    supervisor._apps["reachy_simon"].pid = 88888
    supervisor._apps["reachy_simon"].status = "running"

    with (
        patch("reachy_controller.app_supervisor.psutil.Process", return_value=mock_psutil_proc),
        patch("reachy_controller.app_supervisor._signal_process_tree") as mock_sig,
    ):
        result = await supervisor.stop_app("reachy_simon")

    # Verify the stop completed successfully
    assert result["status"] == "stopped"
    assert supervisor._apps["reachy_simon"].status == "stopped"
    assert supervisor._apps["reachy_simon"].pid is None

    # Full escalation, all via the process-tree helper, in order:
    # SIGINT -> (timeout) -> SIGTERM -> (timeout) -> SIGKILL -> (dead)
    sent = [call.args for call in mock_sig.call_args_list]
    assert sent == [
        (88888, signal.SIGINT),
        (88888, signal.SIGTERM),
        (88888, signal.SIGKILL),
    ], f"Escalation sequence wrong: {sent}"


def test_local_app_to_dict_includes_external() -> None:
    """Test that to_dict() includes external field."""
    app = LocalApp(
        name="reachy_chat",
        module="reachy_chat",
        external=True,
    )
    data = app.to_dict()

    assert "external" in data
    assert data["external"] is True

    # Test default value
    app2 = LocalApp(
        name="reachy_dance",
        module="reachy_dance",
    )
    data2 = app2.to_dict()
    assert data2["external"] is False


def test_scan_for_live_apps_empty() -> None:
    """Test _scan_for_live_apps with no matching processes."""
    supervisor = AppSupervisor()

    with patch("reachy_controller.app_supervisor.psutil.process_iter") as mock_iter:
        mock_iter.return_value = []
        result = supervisor._scan_for_live_apps()

    assert result == {}


def test_scan_for_live_apps_finds_multiple() -> None:
    """Test _scan_for_live_apps finds multiple matching processes."""
    supervisor = AppSupervisor()

    # Mock two processes matching the pattern
    mock_proc1 = MagicMock()
    mock_proc1.info = {
        "pid": 1001,
        "cmdline": ["/path/to/.venv/bin/python", "-m", "reachy_chat.main"],
    }
    mock_proc1.create_time.return_value = 1000.0

    mock_proc2 = MagicMock()
    mock_proc2.info = {
        "pid": 1002,
        "cmdline": ["/path/to/.venv/bin/python", "-m", "reachy_dance.main"],
    }
    mock_proc2.create_time.return_value = 1001.0

    with patch("reachy_controller.app_supervisor.psutil.process_iter") as mock_iter:
        mock_iter.return_value = [mock_proc1, mock_proc2]
        result = supervisor._scan_for_live_apps()

    assert len(result) == 2
    assert "reachy_chat" in result
    assert "reachy_dance" in result
    assert result["reachy_chat"] == (1001, 1000.0)
    assert result["reachy_dance"] == (1002, 1001.0)


def test_scan_for_live_apps_ignores_non_matching() -> None:
    """Test that _scan_for_live_apps ignores non-matching cmdlines."""
    supervisor = AppSupervisor()

    # Mock a non-matching process
    mock_proc = MagicMock()
    mock_proc.info = {
        "pid": 999,
        "cmdline": ["python", "some_script.py"],
    }

    with patch("reachy_controller.app_supervisor.psutil.process_iter") as mock_iter:
        mock_iter.return_value = [mock_proc]
        result = supervisor._scan_for_live_apps()

    assert result == {}


def test_scan_for_live_apps_matches_direct_interpreter() -> None:
    """_scan_for_live_apps matches direct interpreter launches only."""
    supervisor = AppSupervisor()

    matching_proc = MagicMock()
    matching_proc.info = {
        "pid": 1001,
        "cmdline": ["/some/python", "-m", "reachy_chat.main"],
    }
    matching_proc.create_time.return_value = 1000.0

    non_matching_proc = MagicMock()
    non_matching_proc.info = {
        "pid": 999,
        "cmdline": ["/some/python", "-c", "reachy_chat.main"],
    }
    non_matching_proc.create_time.return_value = 900.0

    with patch("reachy_controller.app_supervisor.psutil.process_iter") as mock_iter:
        mock_iter.return_value = [non_matching_proc, matching_proc]
        result = supervisor._scan_for_live_apps()

    assert result == {"reachy_chat": (1001, 1000.0)}


def test_scan_for_live_apps_handles_no_such_process() -> None:
    """Test that _scan_for_live_apps handles NoSuchProcess exception."""
    supervisor = AppSupervisor()

    # Mock a process that raises NoSuchProcess when accessed
    mock_proc = MagicMock()
    mock_proc.info = {
        "pid": 888,
        "cmdline": ["/path/to/.venv/bin/python", "-m", "reachy_chat.main"],
    }
    mock_proc.create_time.side_effect = psutil.NoSuchProcess(888)

    with patch("reachy_controller.app_supervisor.psutil.process_iter") as mock_iter:
        mock_iter.return_value = [mock_proc]
        result = supervisor._scan_for_live_apps()

    # Should catch the exception and return empty dict
    assert result == {}


def test_scan_for_live_apps_handles_access_denied() -> None:
    """Test that _scan_for_live_apps handles AccessDenied exception."""
    supervisor = AppSupervisor()

    # Mock a process that raises AccessDenied when accessed
    mock_proc = MagicMock()
    mock_proc.info = {
        "pid": 777,
        "cmdline": ["/path/to/.venv/bin/python", "-m", "reachy_chat.main"],
    }
    mock_proc.create_time.side_effect = psutil.AccessDenied(777, "test")

    with patch("reachy_controller.app_supervisor.psutil.process_iter") as mock_iter:
        mock_iter.return_value = [mock_proc]
        result = supervisor._scan_for_live_apps()

    # Should catch the exception and return empty dict
    assert result == {}


def test_get_all_status_adopts_external_app() -> None:
    """Test that get_all_status adopts an external running app."""
    supervisor = AppSupervisor()

    # Mock psutil.process_iter to report a live app
    mock_proc = MagicMock()
    mock_proc.info = {
        "pid": 5000,
        "cmdline": ["/path/to/.venv/bin/python", "-m", "reachy_chat.main"],
    }
    mock_proc.create_time.return_value = 2000.0

    with patch("reachy_controller.app_supervisor.psutil.process_iter") as mock_iter:
        mock_iter.return_value = [mock_proc]

        # First call should adopt the app
        status = supervisor.get_all_status()
        chat_status = next(s for s in status if s["name"] == "reachy_chat")

        assert chat_status["status"] == "running"
        assert chat_status["external"] is True
        assert chat_status["pid"] == 5000
        assert chat_status["start_time"] == 2000.0

        # Second call (idempotent) should not change anything
        status2 = supervisor.get_all_status()
        chat_status2 = next(s for s in status2 if s["name"] == "reachy_chat")

        assert chat_status2["status"] == "running"
        assert chat_status2["external"] is True
        assert chat_status2["pid"] == 5000


def test_get_all_status_dead_adopted_app_resets_external() -> None:
    """Test that a dead adopted app is marked stopped with external=False."""
    supervisor = AppSupervisor()

    # First, adopt a live external app
    mock_proc = MagicMock()
    mock_proc.info = {
        "pid": 4000,
        "cmdline": ["/path/to/.venv/bin/python", "-m", "reachy_dance.main"],
    }
    mock_proc.create_time.return_value = 1500.0

    with patch("reachy_controller.app_supervisor.psutil.process_iter") as mock_iter:
        mock_iter.return_value = [mock_proc]
        status1 = supervisor.get_all_status()
        dance_status1 = next(s for s in status1 if s["name"] == "reachy_dance")
        assert dance_status1["status"] == "running"
        assert dance_status1["external"] is True

    # Now simulate the process dying (empty process list)
    with patch("reachy_controller.app_supervisor.psutil.process_iter") as mock_iter:
        # Also mock psutil.Process to handle the liveness check
        mock_iter.return_value = []
        with patch("reachy_controller.app_supervisor.psutil.Process") as mock_psutil_proc:
            # The process lookup should fail, marking it dead
            mock_psutil_proc.side_effect = psutil.NoSuchProcess(4000)
            status2 = supervisor.get_all_status()
            dance_status2 = next(s for s in status2 if s["name"] == "reachy_dance")
            assert dance_status2["status"] == "stopped"
            assert dance_status2["external"] is False


@pytest.mark.asyncio
async def test_start_app_refuses_external_live_app(tmp_path: Path) -> None:
    """Test that start_app refuses to start if an external app is running."""
    supervisor = AppSupervisor()
    supervisor._workspace_root = tmp_path

    # Mock psutil.process_iter to report a live app
    mock_proc = MagicMock()
    mock_proc.info = {
        "pid": 3000,
        "cmdline": ["/path/to/.venv/bin/python", "-m", "reachy_chat.main"],
    }
    mock_proc.create_time.return_value = 1000.0

    mock_popen = MagicMock()
    mock_popen.pid = 9999

    with patch("reachy_controller.app_supervisor.psutil.process_iter") as mock_iter:
        mock_iter.return_value = [mock_proc]
        with patch("reachy_controller.app_supervisor.subprocess.Popen") as mock_popen_class:
            mock_popen_class.return_value = mock_popen
            result = await supervisor.start_app("reachy_chat")

    # Should be refused
    assert result["status"] == "refused"
    assert "already running" in result.get("error", "").lower()
    # Popen should NOT have been called
    mock_popen_class.assert_not_called()


@pytest.mark.asyncio
async def test_start_app_succeeds_when_no_external_app(tmp_path: Path) -> None:
    """Test that start_app succeeds when no external app is running."""
    supervisor = AppSupervisor()
    supervisor._workspace_root = tmp_path

    mock_popen = MagicMock()
    mock_popen.pid = 9999

    with patch("reachy_controller.app_supervisor.psutil.process_iter") as mock_iter:
        mock_iter.return_value = []  # No live apps
        with patch("reachy_controller.app_supervisor.subprocess.Popen") as mock_popen_class:
            mock_popen_class.return_value = mock_popen
            result = await supervisor.start_app("reachy_chat")

    # Should succeed
    assert result["status"] == "loading"
    assert result["pid"] == 9999
    # Popen should have been called
    mock_popen_class.assert_called_once()


@pytest.mark.asyncio
async def test_stop_app_can_kill_external_app() -> None:
    """Test that stop_app can kill a scan-adopted external app (no Popen handle)."""
    supervisor = AppSupervisor()

    # First, adopt an external app
    mock_proc = MagicMock()
    mock_proc.info = {
        "pid": 2000,
        "cmdline": ["/path/to/.venv/bin/python", "-m", "reachy_simon.main"],
    }
    mock_proc.create_time.return_value = 900.0

    with patch("reachy_controller.app_supervisor.psutil.process_iter") as mock_iter:
        mock_iter.return_value = [mock_proc]
        supervisor.get_all_status()  # Adopt the app

    # Verify it's adopted
    simon_app = supervisor._apps["reachy_simon"]
    assert simon_app.status == "running"
    assert simon_app.process is None  # No Popen handle
    assert simon_app.pid == 2000
    assert simon_app.external is True

    # Now stop it
    with (
        patch("reachy_controller.app_supervisor.psutil.Process") as mock_psutil_proc,
        patch("reachy_controller.app_supervisor._signal_process_tree") as mock_sig,
    ):
        mock_proc_instance = MagicMock()
        mock_proc_instance.cmdline.return_value = [
            "/path/to/.venv/bin/python",
            "-m",
            "reachy_simon.main",
        ]

        def mock_wait(timeout):
            """Mock wait that returns immediately."""
            return 0

        mock_proc_instance.wait = mock_wait
        mock_psutil_proc.return_value = mock_proc_instance

        result = await supervisor.stop_app("reachy_simon")

    assert result["status"] == "stopped"
    assert simon_app.status == "stopped"
    assert simon_app.external is False
    assert simon_app.pid is None
    mock_sig.assert_called_once_with(2000, signal.SIGINT)


@pytest.mark.asyncio
async def test_readiness_monitor_promotes_after_grace() -> None:
    """Test that readiness monitor promotes app from loading to running after grace timeout."""
    supervisor = AppSupervisor()

    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch(
        "reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process
    ), patch("reachy_controller.app_supervisor._APP_READINESS_TIMEOUT_S", 0.05):
        # Patch asyncio.to_thread to prevent the reaper from consuming the event loop
        async def mock_to_thread(func, *args):
            # Just sleep forever for the reaper's wait call
            if "wait" in str(func):
                await asyncio.sleep(100)
            return 0

        with patch("reachy_controller.app_supervisor.asyncio.to_thread", side_effect=mock_to_thread):
            result = await supervisor.start_app("reachy_chat")
            assert result["status"] == "loading"

            # Wait for the readiness monitor to promote
            monitor_task = supervisor._readiness_tasks.get("reachy_chat")
            assert monitor_task is not None

            # Wait for the monitor task to complete
            await asyncio.wait_for(monitor_task, timeout=5.0)

            # Check that status was promoted to running
            assert supervisor._apps["reachy_chat"].status == "running"

            # Clean up the reaper task
            reaper_task = supervisor._reaper_tasks.get("reachy_chat")
            if reaper_task:
                reaper_task.cancel()
                try:
                    await reaper_task
                except asyncio.CancelledError:
                    pass


@pytest.mark.asyncio
async def test_readiness_monitor_early_promote_via_lock() -> None:
    """Test that readiness monitor promotes early when daemon reports lock held by this app."""
    supervisor = AppSupervisor()

    # Create a mock daemon proxy that reports lock held by reachy_chat
    mock_proxy = AsyncMock()

    # Return free for the start check, then local_app for the monitor check
    async def lock_status_side_effect():
        if not hasattr(lock_status_side_effect, "call_count"):
            lock_status_side_effect.call_count = 0
        lock_status_side_effect.call_count += 1

        if lock_status_side_effect.call_count == 1:
            # First call (during start_app) should return free
            return {"state": "free"}
        else:
            # Subsequent calls (during monitor) should return local_app
            return {"state": "local_app", "holder_name": "reachy_chat"}

    mock_proxy.get_robot_app_lock_status = AsyncMock(
        side_effect=lock_status_side_effect
    )

    supervisor.daemon_proxy = mock_proxy

    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch(
        "reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process
    ), patch("reachy_controller.app_supervisor._APP_READINESS_TIMEOUT_S", 10.0):
        # Patch asyncio.to_thread to prevent the reaper from blocking
        async def mock_to_thread(func, *args):
            if "wait" in str(func):
                await asyncio.sleep(100)
            return 0

        with patch("reachy_controller.app_supervisor.asyncio.to_thread", side_effect=mock_to_thread):
            result = await supervisor.start_app("reachy_chat")
            assert result["status"] == "loading"

            # Wait for the readiness monitor to promote
            monitor_task = supervisor._readiness_tasks.get("reachy_chat")
            assert monitor_task is not None

            # Wait for the monitor task to complete (should complete before grace timeout)
            await asyncio.wait_for(monitor_task, timeout=5.0)

            # Check that status was promoted to running
            assert supervisor._apps["reachy_chat"].status == "running"

            # Clean up the reaper task
            reaper_task = supervisor._reaper_tasks.get("reachy_chat")
            if reaper_task:
                reaper_task.cancel()
                try:
                    await reaper_task
                except asyncio.CancelledError:
                    pass


@pytest.mark.asyncio
async def test_readiness_monitor_process_death_during_loading_errors() -> None:
    """Test that process death during loading transitions to error, monitor doesn't promote."""
    supervisor = AppSupervisor()

    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch(
        "reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process
    ), patch(
        "reachy_controller.app_supervisor._APP_READINESS_TIMEOUT_S", 10.0
    ):
        # Patch asyncio.to_thread to return error code immediately from reaper
        async def mock_to_thread(func, *args):
            if "wait" in str(func):
                # First call is from reaper; return exit code 1
                return 1  # Non-zero exit code
            return 0

        with patch("reachy_controller.app_supervisor.asyncio.to_thread", side_effect=mock_to_thread):
            result = await supervisor.start_app("reachy_chat")
            assert result["status"] == "loading"

            # Give the reaper and monitor tasks a chance to run
            await asyncio.sleep(0.2)

            # Check that status transitioned to error (reaper already ran)
            assert supervisor._apps["reachy_chat"].status == "error"

            # Now wait for the monitor task (should exit without promoting)
            monitor_task = supervisor._readiness_tasks.get("reachy_chat")
            if monitor_task:
                try:
                    await asyncio.wait_for(monitor_task, timeout=5.0)
                except asyncio.TimeoutError:
                    # Monitor should exit relatively quickly once status != loading
                    monitor_task.cancel()
                    try:
                        await monitor_task
                    except asyncio.CancelledError:
                        pass

            # Status should still be error, not promoted to running
            assert supervisor._apps["reachy_chat"].status == "error"


@pytest.mark.asyncio
async def test_stop_app_sets_stopping_status_during_wait() -> None:
    """Test that stop_app sets status to "stopping" while the wait is in progress.

    This verifies that pollers can observe the intermediate "stopping" status
    while the app is shutting down gracefully (during the ~3.5s wait period).
    """
    supervisor = AppSupervisor()

    # Track the status when wait is called
    status_during_wait = {}

    # Create a mock process with signal/wait tracking
    mock_process = MagicMock()
    mock_process.pid = 12345

    def wait_with_status_check(timeout):
        """Mock wait that records app status when called."""
        # Record the status at the moment wait() is called
        status_during_wait['value'] = supervisor._apps["reachy_chat"].status
        return 0  # Exit code

    mock_process.wait = wait_with_status_check

    # Manually set up the app state (running, so stop will proceed)
    supervisor._apps["reachy_chat"].process = mock_process
    supervisor._apps["reachy_chat"].status = "running"

    # Call stop_app
    result = await supervisor.stop_app("reachy_chat")

    # Verify the stop completed successfully
    assert result["status"] == "stopped"
    assert supervisor._apps["reachy_chat"].status == "stopped"

    # Verify the status was "stopping" while wait was in progress
    assert status_during_wait.get('value') == "stopping", \
        f"Expected status to be 'stopping' during wait, but got '{status_during_wait.get('value')}'"


@pytest.mark.asyncio
async def test_start_app_passes_ready_file_env(tmp_path: Path) -> None:
    """Test that start_app passes REACHY_READY_FILE env var to Popen."""
    supervisor = AppSupervisor()
    supervisor._workspace_root = tmp_path

    mock_process = MagicMock()
    mock_process.pid = 12345
    popen_kwargs = {}

    def capture_popen(*args: object, **kwargs: object) -> MagicMock:
        """Capture Popen kwargs to inspect env."""
        popen_kwargs.update(kwargs)
        return mock_process

    with patch("reachy_controller.app_supervisor.subprocess.Popen", side_effect=capture_popen):
        result = await supervisor.start_app("reachy_chat")

    # Verify start succeeded
    assert result["status"] == "loading"

    # Verify REACHY_READY_FILE is in the env
    popen_env = popen_kwargs.get("env")
    assert popen_env is not None, "Popen env should not be None"
    assert "REACHY_READY_FILE" in popen_env, "REACHY_READY_FILE should be in Popen env"

    # Verify the ready file path is valid
    ready_file_path = popen_env["REACHY_READY_FILE"]
    assert isinstance(ready_file_path, str), "REACHY_READY_FILE should be a string"
    assert len(ready_file_path) > 0, "REACHY_READY_FILE should not be empty"

    # Verify it's stored on the app
    app = supervisor._apps["reachy_chat"]
    assert app.ready_file is not None, "App ready_file should not be None"


@pytest.mark.asyncio
async def test_monitor_readiness_promotes_on_ready_file(tmp_path: Path) -> None:
    """Test that readiness monitor promotes app from loading to running when ready file is created."""
    supervisor = AppSupervisor()
    supervisor._workspace_root = tmp_path

    mock_process = MagicMock()
    mock_process.pid = 12345

    # Patch asyncio.to_thread to prevent the reaper from consuming the event loop
    async def mock_to_thread(func, *args):
        if "wait" in str(func):
            await asyncio.sleep(100)
        return 0

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process), \
         patch("reachy_controller.app_supervisor._APP_READINESS_POLL_S", 0.01), \
         patch("reachy_controller.app_supervisor.asyncio.to_thread", side_effect=mock_to_thread):
        result = await supervisor.start_app("reachy_chat")
        assert result["status"] == "loading"

        # Get the ready file path
        app = supervisor._apps["reachy_chat"]
        ready_file = app.ready_file
        assert ready_file is not None, "ready_file should be set"

        # Wait a bit for the monitor to start polling
        await asyncio.sleep(0.05)

        # Create the ready file (simulate the app becoming ready)
        ready_file.touch()

        # Wait for the readiness monitor to detect it and promote
        monitor_task = supervisor._readiness_tasks.get("reachy_chat")
        assert monitor_task is not None
        await asyncio.wait_for(monitor_task, timeout=5.0)

        # Check that status was promoted to running
        assert supervisor._apps["reachy_chat"].status == "running"

        # Clean up the reaper task
        reaper_task = supervisor._reaper_tasks.get("reachy_chat")
        if reaper_task:
            reaper_task.cancel()
            try:
                await reaper_task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_monitor_readiness_timeout_fallback(tmp_path: Path, caplog) -> None:
    """Test that readiness monitor promotes when timeout is reached even if ready file never appears."""
    supervisor = AppSupervisor()
    supervisor._workspace_root = tmp_path

    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch(
        "reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process
    ), patch("reachy_controller.app_supervisor._APP_READINESS_TIMEOUT_S", 0.05):
        # Patch asyncio.to_thread to prevent the reaper from consuming the event loop
        async def mock_to_thread(func, *args):
            if "wait" in str(func):
                await asyncio.sleep(100)
            return 0

        with patch("reachy_controller.app_supervisor.asyncio.to_thread", side_effect=mock_to_thread):
            result = await supervisor.start_app("reachy_chat")
            assert result["status"] == "loading"

            # Wait for the readiness monitor to timeout and promote
            monitor_task = supervisor._readiness_tasks.get("reachy_chat")
            assert monitor_task is not None

            # Wait for the monitor task to complete
            await asyncio.wait_for(monitor_task, timeout=5.0)

            # Check that status was promoted to running
            assert supervisor._apps["reachy_chat"].status == "running"

            # Verify a WARNING was logged about the timeout
            # (not strictly required by spec, but good for visibility)

            # Clean up the reaper task
            reaper_task = supervisor._reaper_tasks.get("reachy_chat")
            if reaper_task:
                reaper_task.cancel()
                try:
                    await reaper_task
                except asyncio.CancelledError:
                    pass


def test_cmdline_matches_module_exact_match() -> None:
    """Test _cmdline_matches_module with exact module token matching."""
    supervisor = AppSupervisor()

    # Exact match: should return True
    assert supervisor._cmdline_matches_module(
        ["/path/to/python", "-m", "reachy_chat.main"], "reachy_chat"
    )

    # With trailing args: should return True
    assert supervisor._cmdline_matches_module(
        ["/path/to/python", "-m", "reachy_chat.main", "--debug"], "reachy_chat"
    )

    # Confusingly-named module: should return False
    assert not supervisor._cmdline_matches_module(
        ["/path/to/python", "-m", "my_reachy_chat.main"], "reachy_chat"
    )
    assert not supervisor._cmdline_matches_module(
        ["/path/to/python", "-m", "reachy_chat.main.bak"], "reachy_chat"
    )

    # Wrong launcher: should return False
    assert not supervisor._cmdline_matches_module(
        ["/path/to/python", "-c", "reachy_chat.main"], "reachy_chat"
    )

    # Too short: should return False
    assert not supervisor._cmdline_matches_module(
        ["/path/to/python", "-m"], "reachy_chat"
    )
    assert not supervisor._cmdline_matches_module(
        ["/path/to/python"], "reachy_chat"
    )


@pytest.mark.asyncio
async def test_stop_app_rejects_confusingly_named_module() -> None:
    """Test that stop_app does not signal an adopted process with a confusingly-named module."""
    supervisor = AppSupervisor()

    # Mock psutil.Process for the orphaned process with a mismatched module
    mock_psutil_proc = MagicMock()
    mock_psutil_proc.pid = 99999
    mock_psutil_proc.cmdline.return_value = [
        "/path/to/.venv/bin/python",
        "-m",
        "not_reachy_chat.main",  # Confusingly-named, should not match
    ]

    # Manually set up the adopted app state
    supervisor._apps["reachy_chat"].process = None
    supervisor._apps["reachy_chat"].pid = 99999
    supervisor._apps["reachy_chat"].status = "running"

    with (
        patch("reachy_controller.app_supervisor.psutil.Process", return_value=mock_psutil_proc),
        patch("reachy_controller.app_supervisor._signal_process_tree") as mock_sig,
    ):
        result = await supervisor.stop_app("reachy_chat")

    # Should stop without signalling
    assert result["status"] == "stopped"
    assert supervisor._apps["reachy_chat"].status == "stopped"
    # _signal_process_tree should NOT have been called
    mock_sig.assert_not_called()


def test_get_all_status_rejects_confusingly_named_module() -> None:
    """Test that get_all_status resets a running adopted app with a confusingly-named module to stopped."""
    supervisor = AppSupervisor()

    # Manually adopt an app with a live PID
    supervisor._apps["reachy_dance"].pid = 7777
    supervisor._apps["reachy_dance"].status = "running"
    supervisor._apps["reachy_dance"].external = True

    # Mock psutil.Process to return a live process with a confusingly-named module
    mock_psutil_proc = MagicMock()
    mock_psutil_proc.cmdline.return_value = [
        "/path/to/.venv/bin/python",
        "-m",
        "my_reachy_dance.main",  # Confusingly-named, should not match
    ]

    with patch("reachy_controller.app_supervisor.psutil.Process", return_value=mock_psutil_proc):
        status = supervisor.get_all_status()

    # Find the dance status
    dance_status = next(s for s in status if s["name"] == "reachy_dance")

    # Should be reset to stopped because the cmdline doesn't match the expected module
    assert dance_status["status"] == "stopped"
    assert supervisor._apps["reachy_dance"].status == "stopped"
    assert supervisor._apps["reachy_dance"].pid is None


@pytest.mark.asyncio
async def test_async_status_reconciliation_does_not_erase_loading_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = AppSupervisor()
    scanned = asyncio.Event()
    release = asyncio.Event()
    found = {"reachy_chat": (9001, 12.5)}
    loop = asyncio.get_running_loop()

    def delayed_scan() -> dict[str, tuple[int, float]]:
        # This function executes in a worker; signal the event-loop task safely.
        loop.call_soon_threadsafe(scanned.set)
        while not release.is_set():
            time.sleep(0.001)
        return found

    monkeypatch.setattr(supervisor, "_scan_for_live_apps", delayed_scan)
    task = asyncio.create_task(supervisor.aget_all_status())
    await scanned.wait()
    async with supervisor._lock:
        app = supervisor._apps["reachy_chat"]
        process = object()
        app.process = process  # type: ignore[assignment]
        app.pid = 42
        app.status = "loading"
        app.external = False
    release.set()
    await task
    assert app.process is process
    assert app.pid == 42
    assert app.status == "loading"
