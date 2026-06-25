"""Tests for AppSupervisor."""

from __future__ import annotations

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import psutil
import pytest

from reachy_controller.app_supervisor import AppSupervisor, LocalApp


@pytest.mark.asyncio
async def test_start_app_success() -> None:
    """Test successful app start."""
    supervisor = AppSupervisor()

    # Mock subprocess.Popen
    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process):
        result = await supervisor.start_app("reachy_chat")

    assert result["status"] == "running"
    assert result["name"] == "reachy_chat"
    assert result["pid"] == 12345
    assert supervisor._apps["reachy_chat"].status == "running"


@pytest.mark.asyncio
async def test_start_app_unknown_app() -> None:
    """Test start with unknown app name."""
    supervisor = AppSupervisor()
    result = await supervisor.start_app("unknown_app")
    assert result.get("status") == 404
    assert "Unknown app" in result.get("error", "")


@pytest.mark.asyncio
async def test_single_app_lock() -> None:
    """Test that only one app can run at a time."""
    supervisor = AppSupervisor()

    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process):
        # Start first app
        result1 = await supervisor.start_app("reachy_chat")
        assert result1["status"] == "running"

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

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process):
        await supervisor.start_app("reachy_chat")
        result = await supervisor.stop_app("reachy_chat")

    assert result["status"] == "stopped"
    assert supervisor._apps["reachy_chat"].status == "stopped"
    mock_process.send_signal.assert_called_once()


@pytest.mark.asyncio
async def test_stop_app_unknown() -> None:
    """Test stop with unknown app name."""
    supervisor = AppSupervisor()
    result = await supervisor.stop_app("unknown_app")
    assert result.get("status") == 404


@pytest.mark.asyncio
async def test_daemon_lock_check() -> None:
    """Test that start is refused if daemon reports lock held."""
    mock_proxy = AsyncMock()
    mock_proxy.get_robot_app_lock_status = AsyncMock(
        return_value={"state": "local_app", "holder_name": "other_app"}
    )
    supervisor = AppSupervisor(daemon_proxy=mock_proxy)

    mock_process = MagicMock()
    mock_process.pid = 12345

    with patch("reachy_controller.app_supervisor.subprocess.Popen", return_value=mock_process):
        result = await supervisor.start_app("reachy_chat")

    assert result["status"] == "refused"
    assert "Robot held by" in result.get("error", "")
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
async def test_stop_app_uses_thread_for_wait() -> None:
    """Test that stop_app uses asyncio.to_thread for wait() calls.

    This ensures the event loop is not blocked during shutdown sequence.
    """
    supervisor = AppSupervisor()

    # Create a mock process with signal/wait tracking
    mock_process = MagicMock()
    mock_process.pid = 12345
    wait_called = False

    def quick_wait(timeout):
        """Mock wait that succeeds immediately."""
        nonlocal wait_called
        wait_called = True
        return 0  # Exit code

    mock_process.wait = quick_wait

    # Manually set up the app state
    supervisor._apps["reachy_chat"].process = mock_process
    supervisor._apps["reachy_chat"].status = "running"

    # Call stop_app - should complete without blocking the event loop
    result = await supervisor.stop_app("reachy_chat")

    # Verify the stop completed and signals were sent
    assert result["status"] == "stopped"
    assert wait_called, "Process.wait() was not called"
    assert supervisor._apps["reachy_chat"].status == "stopped"
    mock_process.send_signal.assert_called_once()


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
    mock_psutil_proc.cmdline.return_value = ["uv", "run", "python", "-m", "reachy_lookat.main"]

    def quick_wait(timeout):
        """Mock wait that succeeds immediately."""
        return 0  # Exit code

    mock_psutil_proc.wait = quick_wait

    # Manually set up the adopted app state (process=None, pid set, status=running)
    supervisor._apps["reachy_lookat"].process = None
    supervisor._apps["reachy_lookat"].pid = 99999
    supervisor._apps["reachy_lookat"].status = "running"

    with patch("reachy_controller.app_supervisor.psutil.Process", return_value=mock_psutil_proc):
        result = await supervisor.stop_app("reachy_lookat")

    assert result["status"] == "stopped"
    assert supervisor._apps["reachy_lookat"].status == "stopped"
    assert supervisor._apps["reachy_lookat"].pid is None
    # Verify signals were sent via psutil
    mock_psutil_proc.send_signal.assert_called_once()


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

    This is a regression test for the bug where psutil.TimeoutExpired was not
    being caught (the code incorrectly caught subprocess.TimeoutExpired instead).
    When the process ignores SIGINT, proc.wait() should raise psutil.TimeoutExpired,
    triggering SIGTERM, which also fails, triggering SIGKILL.
    """
    supervisor = AppSupervisor()

    # Mock psutil.Process for the orphaned process
    mock_psutil_proc = MagicMock()
    mock_psutil_proc.pid = 88888
    mock_psutil_proc.cmdline.return_value = ["uv", "run", "python", "-m", "reachy_simon.main"]

    # Track which signals were sent and call sequence
    call_sequence = []
    wait_call_count = [0]  # Use list to allow modification in nested function

    def wait_with_timeout(timeout):
        """Simulate process ignoring SIGINT and SIGTERM, then dying on SIGKILL."""
        # First two calls (after SIGINT and SIGTERM) raise TimeoutExpired
        # Third call (after SIGKILL) succeeds
        wait_call_count[0] += 1
        if wait_call_count[0] <= 2:
            raise psutil.TimeoutExpired(88888)
        return 0  # Exit code after SIGKILL

    def send_signal(sig):
        """Track signal sends."""
        call_sequence.append(("signal", sig))

    def terminate():
        """Track terminate calls."""
        call_sequence.append(("terminate",))

    def kill():
        """Track kill calls."""
        call_sequence.append(("kill",))

    mock_psutil_proc.wait = wait_with_timeout
    mock_psutil_proc.send_signal = send_signal
    mock_psutil_proc.terminate = terminate
    mock_psutil_proc.kill = kill

    # Manually set up the adopted app state
    supervisor._apps["reachy_simon"].process = None
    supervisor._apps["reachy_simon"].pid = 88888
    supervisor._apps["reachy_simon"].status = "running"

    with patch("reachy_controller.app_supervisor.psutil.Process", return_value=mock_psutil_proc):
        result = await supervisor.stop_app("reachy_simon")

    # Verify the stop completed successfully
    assert result["status"] == "stopped"
    assert supervisor._apps["reachy_simon"].status == "stopped"
    assert supervisor._apps["reachy_simon"].pid is None

    # Verify the full escalation path ran:
    # 1. SIGINT sent
    # 2. wait() raises TimeoutExpired (process still alive)
    # 3. terminate() called (sends SIGTERM)
    # 4. wait() raises TimeoutExpired again (process still alive)
    # 5. kill() called (sends SIGKILL)
    # 6. wait() succeeds (process dead)
    assert ("signal", signal.SIGINT) in call_sequence, "SIGINT not sent"
    assert ("terminate",) in call_sequence, "SIGTERM not sent"
    assert ("kill",) in call_sequence, "SIGKILL not sent"

    # Verify ordering: signal -> terminate -> kill
    signal_idx = call_sequence.index(("signal", signal.SIGINT))
    terminate_idx = call_sequence.index(("terminate",))
    kill_idx = call_sequence.index(("kill",))
    assert signal_idx < terminate_idx < kill_idx, "Escalation order is wrong"


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
        "cmdline": ["uv", "run", "python", "-m", "reachy_chat.main"],
    }
    mock_proc1.create_time.return_value = 1000.0

    mock_proc2 = MagicMock()
    mock_proc2.info = {
        "pid": 1002,
        "cmdline": ["uv", "run", "python", "-m", "reachy_dance.main"],
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


def test_scan_for_live_apps_handles_no_such_process() -> None:
    """Test that _scan_for_live_apps handles NoSuchProcess exception."""
    supervisor = AppSupervisor()

    # Mock a process that raises NoSuchProcess when accessed
    mock_proc = MagicMock()
    mock_proc.info = {
        "pid": 888,
        "cmdline": ["uv", "run", "python", "-m", "reachy_chat.main"],
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
        "cmdline": ["uv", "run", "python", "-m", "reachy_chat.main"],
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
        "cmdline": ["uv", "run", "python", "-m", "reachy_chat.main"],
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
        "cmdline": ["uv", "run", "python", "-m", "reachy_dance.main"],
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
async def test_start_app_refuses_external_live_app() -> None:
    """Test that start_app refuses to start if an external app is running."""
    supervisor = AppSupervisor()

    # Mock psutil.process_iter to report a live app
    mock_proc = MagicMock()
    mock_proc.info = {
        "pid": 3000,
        "cmdline": ["uv", "run", "python", "-m", "reachy_chat.main"],
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
async def test_start_app_succeeds_when_no_external_app() -> None:
    """Test that start_app succeeds when no external app is running."""
    supervisor = AppSupervisor()

    mock_popen = MagicMock()
    mock_popen.pid = 9999

    with patch("reachy_controller.app_supervisor.psutil.process_iter") as mock_iter:
        mock_iter.return_value = []  # No live apps
        with patch("reachy_controller.app_supervisor.subprocess.Popen") as mock_popen_class:
            mock_popen_class.return_value = mock_popen
            result = await supervisor.start_app("reachy_chat")

    # Should succeed
    assert result["status"] == "running"
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
        "cmdline": ["uv", "run", "python", "-m", "reachy_simon.main"],
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
    with patch("reachy_controller.app_supervisor.psutil.Process") as mock_psutil_proc:
        mock_proc_instance = MagicMock()
        mock_proc_instance.cmdline.return_value = [
            "uv",
            "run",
            "python",
            "-m",
            "reachy_simon.main",
        ]

        def mock_wait(timeout):
            """Mock wait that returns immediately."""
            return 0

        mock_proc_instance.wait = mock_wait
        mock_proc_instance.send_signal = MagicMock()
        mock_psutil_proc.return_value = mock_proc_instance

        result = await supervisor.stop_app("reachy_simon")

    assert result["status"] == "stopped"
    assert simon_app.status == "stopped"
    assert simon_app.external is False
    assert simon_app.pid is None
