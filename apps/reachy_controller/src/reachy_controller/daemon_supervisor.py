"""Supervisor for managing the Reachy daemon subprocess."""

from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import psutil

from reachy_controller.app_supervisor import _get_workspace_root

logger = logging.getLogger(__name__)


def _truthy(value: str | None) -> bool:
    """Convert environment variable to boolean."""
    return value is not None and value.lower() in ("1", "true", "yes", "on")


@dataclass
class DaemonState:
    """State of the managed daemon process."""

    process: subprocess.Popen[str] | None = None
    pid: int | None = None
    status: str = "stopped"  # "stopped", "starting", "running", "stopping", "error"
    error: str | None = None
    start_time: float | None = None

    def to_dict(self, running: bool) -> dict[str, Any]:
        """Convert to serializable dict.

        running is passed in from the daemon's HTTP reachability.
        """
        return {
            "running": running,
            "manageable": False,  # Will be overridden by DaemonSupervisor.status()
            "status": self.status,
            "pid": self.pid,
            "start_time": self.start_time,
        }


class DaemonSupervisor:
    """Supervise the Reachy daemon subprocess."""

    def __init__(self, daemon_proxy: Any | None = None) -> None:
        """Initialize supervisor with empty daemon state.

        Args:
            daemon_proxy: Optional DaemonProxy instance for reachability checks.
        """
        self.daemon_proxy = daemon_proxy
        self._daemon = DaemonState()
        self._manageable = _truthy(os.getenv("REACHY_MANAGE_DAEMON"))
        self._workspace_root = _get_workspace_root()

        # Build the launch command
        if cmd_env := os.getenv("REACHY_DAEMON_CMD"):
            self._cmd = shlex.split(cmd_env)
        else:
            self._cmd = ["uv", "run", "python", "tools/reachy_mini_daemon_jetson.py"]

    async def start(self) -> dict[str, Any]:
        """Start the daemon process if not already running.

        Returns:
            Status dict or error dict.
        """
        if not self._manageable:
            return {"status": "refused", "error": "Daemon is not locally manageable"}

        # If already running, return running status
        if self._daemon.pid is not None:
            try:
                proc = psutil.Process(self._daemon.pid)
                if proc.is_running():
                    return {
                        "status": "running",
                        "pid": self._daemon.pid,
                        "start_time": self._daemon.start_time,
                    }
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            # PID is stale; clear it
            self._daemon.pid = None
            self._daemon.process = None

        # Launch daemon
        try:
            self._daemon.process = subprocess.Popen(
                self._cmd,
                cwd=self._workspace_root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._daemon.pid = self._daemon.process.pid
            self._daemon.status = "starting"
            self._daemon.error = None
            self._daemon.start_time = time.time()
            logger.info(f"Started daemon (pid {self._daemon.pid})")
            return {
                "status": "starting",
                "pid": self._daemon.pid,
                "start_time": self._daemon.start_time,
            }
        except Exception as e:
            self._daemon.status = "error"
            self._daemon.error = str(e)
            logger.error(f"Failed to start daemon: {e}")
            return {"error": str(e), "status": "error"}

    async def stop(self) -> dict[str, Any]:
        """Stop the daemon process gracefully.

        Returns:
            Status dict.
        """
        if not self._manageable:
            return {"status": "refused", "error": "Daemon is not locally manageable"}

        if self._daemon.status == "stopped" and self._daemon.pid is None:
            return {"status": "stopped"}

        try:
            # Handle subprocess.Popen handle (started by this supervisor)
            if self._daemon.process is not None:
                # Try SIGINT first (Ctrl-C)
                self._daemon.process.send_signal(signal.SIGINT)
                try:
                    self._daemon.process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    # Escalate to SIGTERM
                    self._daemon.process.terminate()
                    try:
                        self._daemon.process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        # Final escalation: SIGKILL
                        self._daemon.process.kill()
                        self._daemon.process.wait(timeout=1.0)
                logger.info("Stopped daemon (via subprocess handle)")
            elif self._daemon.pid is not None:
                # Handle adopted orphaned process
                try:
                    proc = psutil.Process(self._daemon.pid)
                    # Verify it's still the daemon by checking the command line
                    cmdline = " ".join(proc.cmdline() or [])
                    if self._is_daemon_process(cmdline):
                        proc.send_signal(signal.SIGINT)
                        try:
                            proc.wait(timeout=3.0)
                        except psutil.TimeoutExpired:
                            proc.terminate()
                            try:
                                proc.wait(timeout=2.0)
                            except psutil.TimeoutExpired:
                                proc.kill()
                                proc.wait(timeout=1.0)
                        logger.info(f"Stopped adopted daemon (pid {self._daemon.pid})")
                    else:
                        logger.warning(
                            f"PID {self._daemon.pid} no longer runs daemon (cmdline: {cmdline})"
                        )
                except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                    logger.info(f"Adopted daemon (pid {self._daemon.pid}) already exited: {e}")
        except Exception as e:
            logger.error(f"Error stopping daemon: {e}")

        self._daemon.status = "stopped"
        self._daemon.error = None
        self._daemon.pid = None
        self._daemon.process = None
        return {"status": "stopped"}

    def reconcile_orphans(self) -> None:
        """Adopt an orphaned daemon process on startup."""
        # Scan for processes matching the daemon launcher
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                if self._is_daemon_process(" ".join(cmdline)):
                    # Don't keep subprocess.Popen handle
                    self._daemon.process = None
                    self._daemon.pid = proc.info.get("pid")
                    # Transient state; actual running comes from reachability
                    self._daemon.status = "stopped"
                    logger.info(f"Reconciled orphaned daemon (pid {self._daemon.pid})")
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    def _is_daemon_process(self, cmdline: str) -> bool:
        """Check if cmdline matches the daemon signature."""
        # Match against the configured command or the default daemon script name
        if "reachy_mini_daemon_jetson.py" in cmdline or "reachy_mini.daemon" in cmdline:
            return True
        # Also match against the configured command if it's custom
        if self._cmd:
            # Check if the last element of the command (the script/module name) is in the cmdline
            cmd_name = self._cmd[-1]
            return cmd_name in cmdline
        return False

    def status(self, running: bool) -> dict[str, Any]:
        """Return daemon status dict.

        Args:
            running: Authoritative running state from daemon's HTTP reachability.

        Returns:
            Status dict with running, manageable, status, pid, start_time.
        """
        # Refresh transient status from running flag. A reachable daemon with a
        # live pid is genuinely up — promote it whether it's a freshly-started
        # ("starting") or an adopted ("stopped") process (#27).
        if (
            running
            and self._daemon.pid is not None
            and self._daemon.status in ("starting", "stopped")
        ):
            self._daemon.status = "running"
        elif not running and self._daemon.status == "running":
            self._daemon.status = "stopped"
            self._daemon.pid = None

        return {
            "running": running,
            "manageable": self._manageable,
            "status": self._daemon.status,
            "pid": self._daemon.pid,
            "start_time": self._daemon.start_time,
        }
