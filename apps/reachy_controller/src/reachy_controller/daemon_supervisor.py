"""Supervisor for managing the Reachy daemon subprocess."""

from __future__ import annotations

import asyncio
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
from reachy_controller.log_stream import LogBroker, ProcessLogPump

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
    external: bool = False
    log_pump: ProcessLogPump | None = None

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

    def __init__(self, daemon_proxy: Any | None = None, log_broker: LogBroker | None = None) -> None:
        """Initialize supervisor with empty daemon state."""
        self.daemon_proxy = daemon_proxy
        self.log_broker = log_broker or LogBroker()
        self._lock = asyncio.Lock()

        self._daemon = DaemonState()
        self._manageable = _truthy(os.getenv("REACHY_MANAGE_DAEMON"))
        self._workspace_root = _get_workspace_root()
        self._log_path = self._workspace_root / ".reachy-logs" / "daemon.log"
        self.log_broker.bind_source(
            "daemon",
            status="empty" if self._manageable else "unavailable",
            file_path=self._log_path,
        )

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

        async with self._lock:
            if self._daemon.status == "stopping":
                return {
                    "status": "refused",
                    "error": "An existing daemon log pump is still shutting down",
                }

            pump = self._daemon.log_pump
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

                # A stale PID is safe to discard only when its old pump has
                # already released the log file and reader ownership.
                if pump is not None:
                    return {
                        "status": "refused",
                        "error": "An existing daemon log pump is still shutting down",
                    }
                self._daemon.pid = None
                self._daemon.process = None
            elif self._daemon.process is not None or pump is not None:
                if pump is not None:
                    return {
                        "status": "refused",
                        "error": "An existing daemon log pump is still shutting down",
                    }
                self._daemon.process = None

            # Launch daemon with an immediate merged-pipe pump; DEVNULL would
            # lose diagnostics and leave no honest source status for the panel.
            process: subprocess.Popen[str] | None = None
            log_pump: ProcessLogPump | None = None
            try:
                self.log_broker.reset("daemon", status="empty")
                process = subprocess.Popen(
                    self._cmd,
                    cwd=self._workspace_root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
                log_pump = ProcessLogPump(
                    process,
                    "daemon",
                    self.log_broker,
                    self._log_path,
                )
                self._daemon.process = process
                self._daemon.log_pump = log_pump
                self._daemon.pid = process.pid
                self._daemon.status = "starting"
                self._daemon.error = None
                self._daemon.external = False
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
                if log_pump is not None:
                    try:
                        await asyncio.to_thread(self._close_log_pump, log_pump)
                    except Exception:
                        # Keep the failed launch owned if its reader cannot
                        # be stopped; a later explicit stop can retry it.
                        self._daemon.process = process
                        self._daemon.log_pump = log_pump
                        self._daemon.pid = process.pid if process is not None else None
                        self._daemon.status = "stopping"
                        return {"error": str(e), "status": "stopping"}
                if process is not None:
                    try:
                        process.kill()
                        await asyncio.to_thread(process.wait, 1.0)
                    except Exception:
                        pass
                if self._daemon.process is process:
                    self._daemon.process = None
                if self._daemon.pid == (process.pid if process is not None else None):
                    self._daemon.pid = None
                if self._daemon.log_pump is log_pump:
                    self._daemon.log_pump = None
                return {"error": str(e), "status": "error"}

    @staticmethod
    def _close_log_pump(
        pump: ProcessLogPump,
        parent_exited: bool = False,
    ) -> None:
        """Stop a pump and retain ownership if its reader remains alive."""
        pump.stop(parent_exited=parent_exited)
        thread = getattr(pump, "thread", None)
        is_alive = getattr(thread, "is_alive", None)
        if callable(is_alive) and is_alive() is True:
            raise RuntimeError("log pump for daemon did not stop")


    async def stop(self) -> dict[str, Any]:
        """Stop the daemon process gracefully.

        Returns:
            Status dict.
        """
        if not self._manageable:
            return {"status": "refused", "error": "Daemon is not locally manageable"}

        async with self._lock:
            return await self._stop_locked()

    async def _stop_locked(self) -> dict[str, Any]:
        """Stop the captured daemon ownership while holding ``self._lock``."""
        process = self._daemon.process
        pid = self._daemon.pid
        pump = self._daemon.log_pump
        if process is None and pid is None and pump is None:
            if self._daemon.status != "stopped" or self._daemon.error is not None:
                self.log_broker.set_status("daemon", "stopped")
                self._daemon.status = "stopped"
                self._daemon.error = None
            return {"status": "stopped"}

        self._daemon.status = "stopping"
        parent_exited = False
        try:
            # Handle subprocess.Popen handle (started by this supervisor).
            if process is not None:
                process.send_signal(signal.SIGINT)
                try:
                    await asyncio.to_thread(process.wait, 3.0)
                    parent_exited = True
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        await asyncio.to_thread(process.wait, 2.0)
                        parent_exited = True
                    except subprocess.TimeoutExpired:
                        process.kill()
                        await asyncio.to_thread(process.wait, 1.0)
                        parent_exited = True
                logger.info("Stopped daemon (via subprocess handle)")
            elif pid is not None:
                # Handle adopted orphaned process.
                try:
                    proc = psutil.Process(pid)
                    cmdline = " ".join(proc.cmdline() or [])
                    if self._is_daemon_process(cmdline):
                        proc.send_signal(signal.SIGINT)
                        try:
                            await asyncio.to_thread(proc.wait, 3.0)
                        except psutil.TimeoutExpired:
                            proc.terminate()
                            try:
                                await asyncio.to_thread(proc.wait, 2.0)
                            except psutil.TimeoutExpired:
                                proc.kill()
                                await asyncio.to_thread(proc.wait, 1.0)
                        logger.info(f"Stopped adopted daemon (pid {pid})")
                    else:
                        logger.warning(
                            f"PID {pid} no longer runs daemon (cmdline: {cmdline})"
                        )
                except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                    logger.info(f"Adopted daemon (pid {pid}) already exited: {e}")
        except Exception as e:
            logger.error(f"Error stopping daemon: {e}")

        # Never release the captured pump's ownership until it has stopped.
        # The lifecycle lock prevents a managed start from replacing it while
        # this await is in progress.
        if pump is not None:
            if self._daemon.log_pump is not pump:
                return {
                    "status": "stopping",
                    "error": "Daemon log pump ownership changed during stop",
                }
            try:
                await asyncio.to_thread(self._close_log_pump, pump, parent_exited)
            except Exception as e:
                self._daemon.status = "stopping"
                self._daemon.error = str(e)
                logger.error(f"Error closing daemon log pump: {e}")
                return {"error": str(e), "status": "stopping"}

        # Do not clear a process/pid installed by any other owner.
        if (
            self._daemon.process is not process
            or self._daemon.pid != pid
            or self._daemon.log_pump is not pump
        ):
            return {
                "status": "stopping",
                "error": "Daemon ownership changed during stop",
            }

        if pump is not None:
            self._daemon.log_pump = None
        self.log_broker.set_status("daemon", "stopped")
        self._daemon.status = "stopped"
        self._daemon.error = None
        self._daemon.pid = None
        self._daemon.process = None
        return {"status": "stopped"}



    async def aclose(self) -> None:
        """Close a managed daemon and its pump during lifespan teardown."""
        async with self._lock:
            if self._daemon.process is not None:
                await self._stop_locked()
                return
            pump = self._daemon.log_pump
            if pump is None:
                return
            try:
                await asyncio.to_thread(self._close_log_pump, pump)
            except Exception as e:
                self._daemon.status = "stopping"
                self._daemon.error = str(e)
                logger.error(f"Error closing daemon log pump: {e}")
                return
            if self._daemon.log_pump is pump:
                self._daemon.log_pump = None
    def reconcile_orphans(self) -> None:
        """Adopt an orphaned daemon process without claiming its existing pipes."""
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                if self._is_daemon_process(" ".join(cmdline)):
                    self._daemon.process = None
                    self._daemon.log_pump = None
                    self._daemon.pid = proc.info.get("pid")
                    self._daemon.status = "stopped"
                    self._daemon.external = True
                    self.log_broker.set_status("daemon", "unavailable")
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
        if not self._manageable or self._daemon.external:
            self.log_broker.set_status("daemon", "unavailable")
        elif running:
            self.log_broker.set_status("daemon", "live")
        else:
            self.log_broker.set_status("daemon", "offline")
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
