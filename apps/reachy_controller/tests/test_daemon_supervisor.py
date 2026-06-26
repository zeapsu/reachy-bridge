"""Tests for DaemonSupervisor."""

from __future__ import annotations

import signal
import subprocess
from unittest.mock import MagicMock, patch

import psutil
import pytest

from reachy_controller.app_supervisor import _get_workspace_root
from reachy_controller.daemon_supervisor import DaemonSupervisor


@pytest.mark.asyncio
async def test_start_refuses_when_not_manageable() -> None:
    """Test that start() is refused when REACHY_MANAGE_DAEMON is not set."""
    supervisor = DaemonSupervisor()

    result = await supervisor.start()

    assert result["status"] == "refused"
    assert "not locally manageable" in result.get("error", "")


@pytest.mark.asyncio
async def test_start_when_manageable() -> None:
    """Test successful daemon start when manageable."""
    with patch.dict("os.environ", {"REACHY_MANAGE_DAEMON": "1"}):
        supervisor = DaemonSupervisor()

        mock_process = MagicMock()
        mock_process.pid = 12345

        with patch(
            "reachy_controller.daemon_supervisor.subprocess.Popen",
            return_value=mock_process,
        ) as mock_popen:
            result = await supervisor.start()

        # Assert the Popen call arguments
        mock_popen.assert_called_once()
        call_args = mock_popen.call_args

        # Check argv (first positional argument)
        assert call_args.args[0] == ["uv", "run", "python", "tools/reachy_mini_daemon_jetson.py"]

        # Check keyword arguments
        assert call_args.kwargs["cwd"] == _get_workspace_root()
        assert call_args.kwargs["stdout"] == subprocess.DEVNULL
        assert call_args.kwargs["stderr"] == subprocess.DEVNULL
        assert call_args.kwargs["start_new_session"] is True

    assert result["status"] == "starting"
    assert result["pid"] == 12345
    assert supervisor._daemon.status == "starting"
    assert supervisor._daemon.pid == 12345


@pytest.mark.asyncio
async def test_start_when_already_running() -> None:
    """Test that start() returns running when daemon already has a pid."""
    with patch.dict("os.environ", {"REACHY_MANAGE_DAEMON": "1"}):
        supervisor = DaemonSupervisor()

        # Manually set up a "running" state
        supervisor._daemon.pid = 12345
        supervisor._daemon.status = "running"

        # Mock psutil.Process to indicate it's still alive
        mock_psutil_proc = MagicMock()
        mock_psutil_proc.is_running.return_value = True

        with patch(
            "reachy_controller.daemon_supervisor.psutil.Process",
            return_value=mock_psutil_proc,
        ):
            result = await supervisor.start()

    assert result["status"] == "running"
    assert result["pid"] == 12345


@pytest.mark.asyncio
async def test_stop_refuses_when_not_manageable() -> None:
    """Test that stop() is refused when REACHY_MANAGE_DAEMON is not set."""
    supervisor = DaemonSupervisor()

    result = await supervisor.stop()

    assert result["status"] == "refused"
    assert "not locally manageable" in result.get("error", "")


@pytest.mark.asyncio
async def test_stop_via_subprocess_handle() -> None:
    """Test stopping a daemon via subprocess.Popen handle."""
    with patch.dict("os.environ", {"REACHY_MANAGE_DAEMON": "1"}):
        supervisor = DaemonSupervisor()

        mock_process = MagicMock()
        mock_process.pid = 12345

        supervisor._daemon.process = mock_process
        supervisor._daemon.status = "running"
        supervisor._daemon.pid = 12345

        result = await supervisor.stop()

    assert result["status"] == "stopped"
    assert supervisor._daemon.status == "stopped"
    mock_process.send_signal.assert_called_once_with(signal.SIGINT)


@pytest.mark.asyncio
async def test_stop_via_adopted_pid() -> None:
    """Test stopping a daemon via adopted pid (process=None)."""
    with patch.dict("os.environ", {"REACHY_MANAGE_DAEMON": "1"}):
        supervisor = DaemonSupervisor()

        mock_psutil_proc = MagicMock()
        mock_psutil_proc.pid = 12345
        mock_psutil_proc.cmdline.return_value = [
            "uv",
            "run",
            "python",
            "tools/reachy_mini_daemon_jetson.py",
        ]

        def quick_wait(timeout):
            return 0

        mock_psutil_proc.wait = quick_wait

        supervisor._daemon.process = None
        supervisor._daemon.pid = 12345
        supervisor._daemon.status = "running"

        with patch(
            "reachy_controller.daemon_supervisor.psutil.Process",
            return_value=mock_psutil_proc,
        ):
            result = await supervisor.stop()

    assert result["status"] == "stopped"
    assert supervisor._daemon.status == "stopped"
    assert supervisor._daemon.pid is None
    mock_psutil_proc.send_signal.assert_called_once_with(signal.SIGINT)


@pytest.mark.asyncio
async def test_stop_via_adopted_stopped_pid() -> None:
    """Test stopping an adopted daemon whose transient status is stopped."""
    with patch.dict("os.environ", {"REACHY_MANAGE_DAEMON": "1"}):
        supervisor = DaemonSupervisor()

        mock_psutil_proc = MagicMock()
        mock_psutil_proc.pid = 12345
        mock_psutil_proc.cmdline.return_value = [
            "uv",
            "run",
            "python",
            "tools/reachy_mini_daemon_jetson.py",
        ]

        def quick_wait(timeout):
            return 0

        mock_psutil_proc.wait = quick_wait

        supervisor._daemon.process = None
        supervisor._daemon.pid = 12345
        supervisor._daemon.status = "stopped"

        with patch(
            "reachy_controller.daemon_supervisor.psutil.Process",
            return_value=mock_psutil_proc,
        ):
            result = await supervisor.stop()

    assert result["status"] == "stopped"
    assert supervisor._daemon.status == "stopped"
    assert supervisor._daemon.pid is None
    mock_psutil_proc.send_signal.assert_called_once_with(signal.SIGINT)


@pytest.mark.asyncio
async def test_stop_escalation_sigint_to_sigterm_to_sigkill() -> None:
    """Test SIGINT -> SIGTERM -> SIGKILL escalation."""
    with patch.dict("os.environ", {"REACHY_MANAGE_DAEMON": "1"}):
        supervisor = DaemonSupervisor()

        mock_psutil_proc = MagicMock()
        mock_psutil_proc.pid = 12345
        mock_psutil_proc.cmdline.return_value = [
            "uv",
            "run",
            "python",
            "tools/reachy_mini_daemon_jetson.py",
        ]

        call_sequence = []
        wait_call_count = [0]

        def wait_with_timeout(timeout):
            wait_call_count[0] += 1
            if wait_call_count[0] <= 2:
                raise psutil.TimeoutExpired(12345)
            return 0

        def send_signal(sig):
            call_sequence.append(("signal", sig))

        def terminate():
            call_sequence.append(("terminate",))

        def kill():
            call_sequence.append(("kill",))

        mock_psutil_proc.wait = wait_with_timeout
        mock_psutil_proc.send_signal = send_signal
        mock_psutil_proc.terminate = terminate
        mock_psutil_proc.kill = kill

        supervisor._daemon.process = None
        supervisor._daemon.pid = 12345
        supervisor._daemon.status = "running"

        with patch(
            "reachy_controller.daemon_supervisor.psutil.Process",
            return_value=mock_psutil_proc,
        ):
            result = await supervisor.stop()

    assert result["status"] == "stopped"
    assert supervisor._daemon.status == "stopped"

    # Verify escalation sequence
    assert ("signal", signal.SIGINT) in call_sequence
    assert ("terminate",) in call_sequence
    assert ("kill",) in call_sequence

    signal_idx = call_sequence.index(("signal", signal.SIGINT))
    terminate_idx = call_sequence.index(("terminate",))
    kill_idx = call_sequence.index(("kill",))
    assert signal_idx < terminate_idx < kill_idx


@pytest.mark.asyncio
async def test_reconcile_orphans_adopts_matching_daemon() -> None:
    """Test that reconcile_orphans() adopts a matching daemon process."""
    with patch.dict("os.environ", {"REACHY_MANAGE_DAEMON": "1"}):
        supervisor = DaemonSupervisor()

        mock_proc = MagicMock()
        mock_proc.info = {
            "pid": 99999,
            "cmdline": ["uv", "run", "python", "tools/reachy_mini_daemon_jetson.py"],
        }

        with patch(
            "reachy_controller.daemon_supervisor.psutil.process_iter",
            return_value=[mock_proc],
        ):
            supervisor.reconcile_orphans()

    assert supervisor._daemon.pid == 99999
    assert supervisor._daemon.status == "stopped"
    assert supervisor._daemon.process is None


@pytest.mark.asyncio
async def test_status_returns_manageable_flag() -> None:
    """Test that status() returns the manageable flag."""
    with patch.dict("os.environ", {"REACHY_MANAGE_DAEMON": "1"}):
        supervisor = DaemonSupervisor()

        status = supervisor.status(running=False)

    assert status["manageable"] is True
    assert status["running"] is False


@pytest.mark.asyncio
async def test_status_returns_running_flag() -> None:
    """Test that status() returns the running flag from reachability."""
    with patch.dict("os.environ", {"REACHY_MANAGE_DAEMON": "1"}):
        supervisor = DaemonSupervisor()
        supervisor._daemon.pid = 12345
        supervisor._daemon.status = "running"

        status = supervisor.status(running=False)

    assert status["running"] is False


def test_status_promotes_adopted_daemon_with_live_pid() -> None:
    """Test that reachable adopted daemons are promoted to running."""
    with patch.dict("os.environ", {"REACHY_MANAGE_DAEMON": "1"}):
        supervisor = DaemonSupervisor()
        supervisor._daemon.pid = 12345
        supervisor._daemon.status = "stopped"

        status = supervisor.status(running=True)

    assert status["running"] is True
    assert status["status"] == "running"
    assert status["pid"] == 12345
    assert supervisor._daemon.status == "running"


def test_status_does_not_promote_adopted_daemon_without_pid() -> None:
    """Test that reachable status alone does not promote when no pid is known."""
    with patch.dict("os.environ", {"REACHY_MANAGE_DAEMON": "1"}):
        supervisor = DaemonSupervisor()
        supervisor._daemon.pid = None
        supervisor._daemon.status = "stopped"

        status = supervisor.status(running=True)

    assert status["running"] is True
    assert status["status"] == "stopped"
    assert status["pid"] is None
    assert supervisor._daemon.status == "stopped"


@pytest.mark.asyncio
async def test_status_shape() -> None:
    """Test that status() returns the expected dict shape."""
    with patch.dict("os.environ", {"REACHY_MANAGE_DAEMON": "1"}):
        supervisor = DaemonSupervisor()
        supervisor._daemon.pid = 12345

        status = supervisor.status(running=True)

    assert "running" in status
    assert "manageable" in status
    assert "status" in status
    assert "pid" in status
    assert "start_time" in status


def test_start_command_from_env() -> None:
    """Test that daemon start command is read from REACHY_DAEMON_CMD env."""
    with patch.dict("os.environ", {"REACHY_DAEMON_CMD": "python /custom/daemon.py --flag"}):
        supervisor = DaemonSupervisor()

    assert supervisor._cmd == ["python", "/custom/daemon.py", "--flag"]


def test_start_command_default() -> None:
    """Test default daemon start command."""
    supervisor = DaemonSupervisor()
    assert supervisor._cmd == ["uv", "run", "python", "tools/reachy_mini_daemon_jetson.py"]
