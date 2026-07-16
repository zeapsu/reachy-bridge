"""Tests for DaemonSupervisor."""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest

from reachy_controller.app_supervisor import _get_workspace_root
from reachy_controller.daemon_supervisor import DaemonSupervisor
from reachy_controller.log_stream import LogBroker, ProcessLogPump


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
        assert call_args.kwargs["stdout"] == subprocess.PIPE
        assert call_args.kwargs["stderr"] == subprocess.STDOUT
        assert call_args.kwargs["text"] is True
        assert call_args.kwargs["bufsize"] == 1
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
async def test_stop_wait_serializes_concurrent_start_and_preserves_new_ownership() -> None:
    """A start waiting behind stop must not be cleared by the old stop."""
    with patch.dict("os.environ", {"REACHY_MANAGE_DAEMON": "1"}):
        supervisor = DaemonSupervisor()
        old_process = MagicMock(pid=111)
        wait_started = threading.Event()
        release_wait = threading.Event()

        def paused_wait(timeout: float) -> int:
            wait_started.set()
            assert release_wait.wait(timeout=1.0)
            return 0

        old_process.wait.side_effect = paused_wait
        old_pump = MagicMock()
        old_pump.thread.is_alive.return_value = False
        supervisor._daemon.process = old_process
        supervisor._daemon.pid = old_process.pid
        supervisor._daemon.status = "running"
        supervisor._daemon.log_pump = old_pump

        new_process = MagicMock(pid=222)
        new_pump = MagicMock()
        new_pump.thread.is_alive.return_value = False
        with (
            patch(
                "reachy_controller.daemon_supervisor.subprocess.Popen",
                return_value=new_process,
            ) as popen,
            patch(
                "reachy_controller.daemon_supervisor.ProcessLogPump",
                return_value=new_pump,
            ),
        ):
            stop_task = asyncio.create_task(supervisor.stop())
            await asyncio.wait_for(asyncio.to_thread(wait_started.wait, 1.0), 1.0)
            start_task = asyncio.create_task(supervisor.start())
            await asyncio.sleep(0)
            assert not start_task.done()

            release_wait.set()
            assert await stop_task == {"status": "stopped"}
            assert await start_task == {
                "status": "starting",
                "pid": new_process.pid,
                "start_time": supervisor._daemon.start_time,
            }

        popen.assert_called_once()
        old_pump.stop.assert_called_once_with(parent_exited=True)
        new_pump.stop.assert_not_called()
        assert supervisor._daemon.process is new_process
        assert supervisor._daemon.pid == new_process.pid
        assert supervisor._daemon.log_pump is new_pump


@pytest.mark.asyncio
async def test_failed_daemon_pump_cleanup_retains_ownership_and_refuses_restart() -> None:
    """A failed/live old pump blocks replacement until explicit cleanup succeeds."""
    with patch.dict("os.environ", {"REACHY_MANAGE_DAEMON": "1"}):
        supervisor = DaemonSupervisor()
        process = MagicMock(pid=12345)
        pump = MagicMock()
        pump.stop.side_effect = RuntimeError("pump join failed")
        pump.thread.is_alive.return_value = True
        supervisor._daemon.process = process
        supervisor._daemon.pid = process.pid
        supervisor._daemon.status = "running"
        supervisor._daemon.log_pump = pump

        result = await supervisor.stop()

        assert result == {"error": "pump join failed", "status": "stopping"}
        assert supervisor._daemon.status == "stopping"
        assert supervisor._daemon.process is process
        assert supervisor._daemon.pid == process.pid
        assert supervisor._daemon.log_pump is pump

        with patch("reachy_controller.daemon_supervisor.subprocess.Popen") as popen:
            restart = await supervisor.start()

        assert restart["status"] == "refused"
        assert "log pump" in restart["error"]
        popen.assert_not_called()
        assert supervisor._daemon.process is process
        assert supervisor._daemon.pid == process.pid
        assert supervisor._daemon.log_pump is pump


@pytest.mark.asyncio
async def test_stop_passes_parent_exited_keyword_and_drains_buffered_tail(
    tmp_path: Path,
) -> None:
    with patch.dict("os.environ", {"REACHY_MANAGE_DAEMON": "1"}):
        broker = LogBroker()
        supervisor = DaemonSupervisor(log_broker=broker)
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import signal,sys\n"
                    "def on_sigint(signum, frame):\n"
                    "    sys.stdout.write('buffered-exit-tail\\n')\n"
                    "    sys.stdout.flush()\n"
                    "    raise SystemExit(0)\n"
                    "signal.signal(signal.SIGINT, on_sigint)\n"
                    "print('ready', flush=True)\n"
                    "signal.pause()\n"
                ),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        pump = ProcessLogPump(process, "daemon", broker, tmp_path / "daemon.log")
        calls: dict[str, bool] = {}
        original_stop = pump.stop

        def strict_stop(*, parent_exited: bool) -> None:
            calls["parent_exited"] = parent_exited
            original_stop(parent_exited=parent_exited)

        pump.stop = strict_stop  # type: ignore[method-assign]
        supervisor._daemon.process = process
        supervisor._daemon.log_pump = pump
        supervisor._daemon.pid = process.pid
        for _ in range(100):
            if broker.ring("daemon") == ["ready"]:
                break
            await asyncio.sleep(0.01)
        assert broker.ring("daemon") == ["ready"]
        supervisor._daemon.status = "running"
        try:
            result = await supervisor.stop()
            assert result["status"] == "stopped"
            assert calls == {"parent_exited": True}
            assert (tmp_path / "daemon.log").read_text().splitlines() == [
                "ready",
                "buffered-exit-tail",
            ]
            assert pump.thread is not None and not pump.thread.is_alive()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            await broker.aclose()


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
