"""Local app supervisor for managing Reachy workspace apps via subprocess."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

logger = logging.getLogger(__name__)


def _get_workspace_root() -> Path:
    """Resolve workspace root robustly: walk up from __file__ or env WORKSPACE_ROOT."""
    if workspace_env := os.getenv("WORKSPACE_ROOT"):
        return Path(workspace_env)
    # Walk up from this file until we find the workspace root (contains apps/ folder)
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / "apps").is_dir():
            return current
        current = current.parent
    raise RuntimeError("Could not locate workspace root (apps/ directory)")


@dataclass
class LocalApp:
    """State of a managed local application."""

    name: str
    module: str
    uses_camera: bool = True
    process: subprocess.Popen[str] | None = None
    pid: int | None = None
    status: str = "stopped"  # "stopped", "running", "stopping", "error"
    error: str | None = None
    start_time: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to serializable dict."""
        return {
            "name": self.name,
            "module": self.module,
            "uses_camera": self.uses_camera,
            "pid": self.pid,
            "status": self.status,
            "error": self.error,
            "start_time": self.start_time,
        }


class AppSupervisor:
    """Supervise local Python apps launched via uv run."""

    APPS: dict[str, dict[str, Any]] = {
        "reachy_chat": {"module": "reachy_chat", "uses_camera": True},
        "reachy_dance": {"module": "reachy_dance", "uses_camera": False},
        "reachy_lookat": {"module": "reachy_lookat", "uses_camera": True},
        "reachy_simon": {"module": "reachy_simon", "uses_camera": False},
    }

    def __init__(self, daemon_proxy: Any | None = None) -> None:
        """Initialize supervisor with empty app state.

        Args:
            daemon_proxy: Optional DaemonProxy instance for lock checks.
        """
        self.daemon_proxy = daemon_proxy
        self._lock = asyncio.Lock()
        self._apps: dict[str, LocalApp] = {
            name: LocalApp(
                name=name,
                module=config["module"],
                uses_camera=config.get("uses_camera", True),
            )
            for name, config in self.APPS.items()
        }
        self._reaper_tasks: dict[str, asyncio.Task[None]] = {}
        self._camera_release_hook: Callable[[], Awaitable[None]] | None = None
        self._workspace_root = _get_workspace_root()

    def set_camera_release_hook(
        self, callback: Callable[[], Awaitable[None]] | None
    ) -> None:
        """Register a callback that releases dashboard-owned camera resources."""
        self._camera_release_hook = callback

    def camera_available(self) -> bool:
        """Return whether no running app currently owns the camera."""
        return self.camera_holder() is None

    def camera_holder(self) -> str | None:
        """Return the running camera-using app name, if any."""
        for app in self._apps.values():
            if app.status == "running" and app.uses_camera:
                return app.name
        return None

    async def _release_dashboard_camera(self) -> None:
        """Ask the dashboard camera reader to yield ownership if registered."""
        if self._camera_release_hook is None:
            return
        try:
            await self._camera_release_hook()
        except Exception as e:
            logger.error(f"Dashboard camera release hook failed: {e}")

    async def start_app(self, name: str) -> dict[str, Any]:
        """Start an app. Returns status dict or error dict."""
        if name not in self.APPS:
            return {"error": f"Unknown app: {name}", "status": 404}

        async with self._lock:
            app = self._apps[name]

            # Refuse if any app is already running
            any_running = any(a.status == "running" for a in self._apps.values())
            if any_running:
                return {
                    "error": "Another app is already running",
                    "name": name,
                    "status": "refused",
                }

            # Check daemon lock status (if proxy available)
            if self.daemon_proxy is not None:
                lock_status = await self.daemon_proxy.get_robot_app_lock_status()
                if lock_status is not None and lock_status.get("state") != "free":
                    return {
                        "error": f"Robot held by {lock_status.get('state')}: {lock_status.get('holder_name')}",
                        "name": name,
                        "status": "refused",
                    }

            if app.uses_camera:
                await self._release_dashboard_camera()

            # Launch app
            app_dir = self._workspace_root / "apps" / name
            argv = ["uv", "run", "python", "-m", f"{name}.main"]

            try:
                app.process = subprocess.Popen(
                    argv,
                    cwd=app_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                app.pid = app.process.pid
                app.status = "running"
                app.error = None
                app.start_time = time.time()
                logger.info(f"Started app {name} (pid {app.pid})")

                # Spawn reaper task
                task = asyncio.create_task(self._reap_app(name))
                self._reaper_tasks[name] = task

                return {
                    "name": name,
                    "status": "running",
                    "pid": app.pid,
                }
            except Exception as e:
                app.status = "error"
                app.error = str(e)
                logger.error(f"Failed to start app {name}: {e}")
                return {"error": str(e), "name": name, "status": "error"}

    async def stop_app(self, name: str) -> dict[str, Any]:
        """Stop an app gracefully. Returns status dict."""
        if name not in self.APPS:
            return {"error": f"Unknown app: {name}", "status": 404}

        async with self._lock:
            app = self._apps[name]

            if app.status == "stopped":
                return {"name": name, "status": "stopped"}

            try:
                # Handle subprocess.Popen handle (started by this supervisor)
                if app.process is not None:
                    # Try SIGINT first (Ctrl-C)
                    app.process.send_signal(signal.SIGINT)
                    try:
                        await asyncio.to_thread(app.process.wait, 3.0)
                    except subprocess.TimeoutExpired:
                        # Escalate to SIGTERM
                        app.process.terminate()
                        try:
                            await asyncio.to_thread(app.process.wait, 2.0)
                        except subprocess.TimeoutExpired:
                            # Final escalation: SIGKILL
                            app.process.kill()
                            await asyncio.to_thread(app.process.wait, 1.0)
                    logger.info(f"Stopped app {name} (via subprocess handle)")
                elif app.pid is not None:
                    # Handle adopted orphaned process (reconcile_orphans set pid, process=None)
                    try:
                        proc = psutil.Process(app.pid)
                        # Verify it's still the right process
                        cmdline = " ".join(proc.cmdline() or [])
                        if f"{name}.main" in cmdline:
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
                            logger.info(f"Stopped adopted app {name} (pid {app.pid})")
                        else:
                            logger.warning(
                                f"PID {app.pid} no longer matches {name}.main (cmdline: {cmdline})"
                            )
                    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                        logger.info(f"Adopted process {name} (pid {app.pid}) already exited: {e}")
            except Exception as e:
                logger.error(f"Error stopping app {name}: {e}")

            app.status = "stopped"
            app.error = None
            app.pid = None
            app.process = None
            if app.uses_camera:
                logger.info(f"Camera available after stopping app {name}")
            return {"name": name, "status": "stopped"}

    def stop_all(self) -> None:
        """Stop all running apps synchronously (for shutdown)."""
        for app in self._apps.values():
            if app.process is not None and app.status == "running":
                try:
                    app.process.terminate()
                    app.process.wait(timeout=2.0)
                except Exception:
                    app.process.kill()
                finally:
                    app.status = "stopped"
                    app.process = None

    async def _reap_app(self, name: str) -> None:
        """Monitor a child process and update status when it exits."""
        app = self._apps[name]
        if app.process is None:
            return

        try:
            exit_code = await asyncio.to_thread(app.process.wait)
            async with self._lock:
                if app.status == "running":
                    if exit_code == 0:
                        app.status = "stopped"
                        app.error = None
                    else:
                        app.status = "error"
                        app.error = f"Exited with code {exit_code}"
                    logger.info(
                        f"App {name} exited (code {exit_code}, status {app.status})"
                    )
        except Exception as e:
            logger.error(f"Reaper error for {name}: {e}")
        finally:
            self._reaper_tasks.pop(name, None)

    def reconcile_orphans(self) -> None:
        """Adopt orphaned app processes on startup."""
        for name in self.APPS:
            app = self._apps[name]
            # Scan for processes matching "uv run python -m <name>.main"
            for proc in psutil.process_iter(["pid", "cmdline"]):
                try:
                    cmdline = proc.info.get("cmdline") or []
                    if (
                        len(cmdline) >= 5
                        and cmdline[0] == "uv"
                        and cmdline[1] == "run"
                        and cmdline[2] == "python"
                        and cmdline[3] == "-m"
                        and f"{name}.main" in " ".join(cmdline)
                    ):
                        app.process = None  # Don't keep subprocess.Popen handle
                        app.pid = proc.info.get("pid")
                        app.status = "running"
                        logger.info(f"Reconciled orphaned app {name} (pid {app.pid})")
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

    def get_all_status(self) -> list[dict[str, Any]]:
        """Get status for all apps. Reconciles dead processes to stopped state."""
        # Liveness check: if a process is marked running but is dead, transition it to stopped
        for app in self._apps.values():
            if app.status == "running" and app.pid is not None:
                try:
                    proc = psutil.Process(app.pid)
                    # Verify it's still the right process
                    cmdline = " ".join(proc.cmdline() or [])
                    if f"{app.module}.main" not in cmdline:
                        # PID was reused by a different process; treat as dead
                        logger.warning(
                            f"PID {app.pid} no longer runs {app.module}.main; transition to stopped"
                        )
                        app.status = "stopped"
                        app.pid = None
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    # Process is dead; transition to stopped
                    logger.info(f"App {app.name} (pid {app.pid}) is dead; transition to stopped")
                    app.status = "stopped"
                    app.pid = None
        return [app.to_dict() for app in self._apps.values()]

    def get_status(self, name: str) -> dict[str, Any] | None:
        """Get status for one app, or None if unknown."""
        if name not in self._apps:
            return None
        return self._apps[name].to_dict()
