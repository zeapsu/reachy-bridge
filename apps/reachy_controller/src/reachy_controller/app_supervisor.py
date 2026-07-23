"""Local app supervisor for managing Reachy workspace apps via subprocess."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, TypeGuard

import psutil

from reachy_controller.log_stream import LogBroker, ProcessLogPump

logger = logging.getLogger(__name__)


_STOP_SIGINT_GRACE_S = 8.0  # generous: the app's graceful shutdown (return-home + sleep cue)
# takes ~4-5s; wait() returns as soon as the process exits, so a larger ceiling never slows a
# clean stop — it only gives a slow app room before we escalate.
_STOP_SIGTERM_GRACE_S = 3.0
_APP_READINESS_POLL_S = 0.5  # Polling interval for readiness monitor
_APP_READINESS_TIMEOUT_S = (
    60.0  # Ceiling timeout for app startup before promoting loading → running
)
_HERMES_STARTUP_BUDGET_S = 300.0


def _hermes_startup_budget() -> float:
    """Read the per-launch Hermes startup budget, falling back safely."""
    try:
        budget = float(os.getenv("REACHY_HERMES_STARTUP_BUDGET_S", ""))
    except ValueError:
        return _HERMES_STARTUP_BUDGET_S
    return budget if math.isfinite(budget) and budget > 0 else _HERMES_STARTUP_BUDGET_S


def _signal_process_tree(pid: int, sig: int) -> None:
    """Send ``sig`` to ``pid`` and all of its descendant processes.

    The launched process is the Python app directly, but it may spawn child subprocesses
    (e.g. audio handlers). Signalling the whole tree ensures the signal reaches child
    processes spawned by the app. Best-effort: processes that exit mid-iteration are ignored.
    """
    try:
        parent = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return
    try:
        targets = parent.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        targets = []
    targets.append(parent)
    for proc in targets:
        try:
            proc.send_signal(sig)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


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
    display: str = ""
    blurb: str = ""
    process: subprocess.Popen[str] | None = None
    pid: int | None = None
    status: str = "stopped"  # "stopped", "loading", "running", "stopping", "error"
    error: str | None = None
    start_time: float | None = None
    external: bool = False
    log_handle: TextIO | None = None
    log_pump: ProcessLogPump | None = None
    ready_file: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to serializable dict."""
        return {
            "name": self.name,
            "module": self.module,
            "uses_camera": self.uses_camera,
            "display": self.display,
            "blurb": self.blurb,
            "pid": self.pid,
            "status": self.status,
            "error": self.error,
            "start_time": self.start_time,
            "external": self.external,
        }


class AppSupervisor:
    """Supervise local Python apps launched with the shared interpreter."""

    DEFAULT_FAVORITES = ["reachy_chat", "reachy_dance", "reachy_lookat"]
    APPS: dict[str, dict[str, Any]] = {
        "reachy_chat": {
            "module": "reachy_chat",
            "uses_camera": True,
            "display": "Reachy Chat",
            "blurb": "A voice conversation app for Reachy Mini.",
        },
        "reachy_dance": {
            "module": "reachy_dance",
            "uses_camera": False,
            "display": "Reachy Dance",
            "blurb": "Beat-synced head and antenna choreography.",
        },
        "reachy_lookat": {
            "module": "reachy_lookat",
            "uses_camera": True,
            "display": "Reachy Lookat",
            "blurb": "OpenCV face-tracking head control.",
        },
        "reachy_simon": {
            "module": "reachy_simon",
            "uses_camera": False,
            "display": "Reachy Simon",
            "blurb": "A Simon-style memory game with Reachy.",
        },
        "reachy_hermes": {
            "module": "reachy_hermes",
            "uses_camera": False,
            "display": "Hermes",
            "blurb": "Voice-driven agent for research and longer tasks.",
        },
    }

    def __init__(
        self, daemon_proxy: Any | None = None, log_broker: LogBroker | None = None
    ) -> None:
        """Initialize supervisor with empty app state.

        Args:
            daemon_proxy: Optional DaemonProxy instance for lock checks.
            log_broker: Optional app-scoped broker shared with the controller.
        """
        self.daemon_proxy = daemon_proxy
        self.log_broker = log_broker or LogBroker()
        self._lock = asyncio.Lock()
        self._apps: dict[str, LocalApp] = {
            name: LocalApp(
                name=name,
                module=config["module"],
                uses_camera=config.get("uses_camera", True),
                display=config["display"],
                blurb=config["blurb"],
            )
            for name, config in self.APPS.items()
        }
        self._reaper_tasks: dict[str, asyncio.Task[None]] = {}
        self._readiness_tasks: dict[str, asyncio.Task[None]] = {}
        self._workspace_root = _get_workspace_root()
        for name in self.APPS:
            self.log_broker.bind_source(
                name,
                status="empty",
                file_path=self._workspace_root / ".reachy-logs" / f"{name}.log",
            )

    def _favorites_path(self) -> Path:
        """Return the robot-local favorites file beneath the controller app root."""
        root = self._workspace_root
        if root.name == "reachy_controller" and root.parent.name == "apps":
            controller_root = root
        else:
            candidate = root / "apps" / "reachy_controller"
            controller_root = candidate if candidate.is_dir() else root
        return controller_root / ".favorites.json"

    @classmethod
    def _valid_favorites(cls, value: object) -> TypeGuard[list[str]]:
        """Validate and narrow decoded favorites before copying them."""
        return (
            isinstance(value, list)
            and len(value) <= 3
            and all(isinstance(name, str) and name in cls.APPS for name in value)
            and len(value) == len(set(value))
        )

    def get_favorites(self) -> list[str]:
        """Load favorites, falling back safely when storage is missing or invalid."""
        try:
            with self._favorites_path().open(encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError, TypeError):
            return self.DEFAULT_FAVORITES.copy()
        favorites = payload.get("favorites") if isinstance(payload, dict) else None
        if not self._valid_favorites(favorites):
            return self.DEFAULT_FAVORITES.copy()
        return list(favorites)

    def set_favorites(self, favorites: list[str]) -> list[str]:
        """Persist favorites atomically and return the saved list."""
        if not self._valid_favorites(favorites):
            raise ValueError("favorites must contain at most three distinct known app names")
        path = self._favorites_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f"{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump({"favorites": favorites}, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return list(favorites)

    def any_app_active(self) -> bool:
        """True when any managed app is running or loading (robot actuating)."""
        return any(a.status in ("running", "loading") for a in self._apps.values())

    def _has_retained_log_ownership(self) -> bool:
        """Return whether any app still owns a pump or is finishing shutdown."""
        return any(
            app.log_pump is not None or app.status == "stopping" for app in self._apps.values()
        )

    @staticmethod
    def _cmdline_matches_module(cmdline: list[str], module: str) -> bool:
        """True when ``cmdline`` is a direct ``<python> -m <module>.main`` launch.

        Exact token match (not substring): trailing args are allowed (e.g. ``--debug``),
        but a confusingly-named module like ``my_<module>.main`` must NOT match.
        """
        return len(cmdline) >= 3 and cmdline[1] == "-m" and cmdline[2] == f"{module}.main"

    def _close_log_handle(self, app: LocalApp, parent_exited: bool = False) -> None:
        """Stop a pump before releasing its app ownership."""
        pump = app.log_pump
        if pump is not None:
            pump.stop(parent_exited=parent_exited)
            thread = getattr(pump, "thread", None)
            is_alive = getattr(thread, "is_alive", None)
            if callable(is_alive) and is_alive() is True:
                raise RuntimeError(f"log pump for {app.name} did not stop")
            app.log_pump = None
            # ProcessLogPump owns this file; avoid closing the same descriptor
            # through the legacy alias a second time.
            app.log_handle = None
            return
        if app.log_handle is not None:
            try:
                app.log_handle.close()
            except Exception:
                pass
            app.log_handle = None

    async def _escalate_stop(
        self, pid: int, wait_fn: Callable, timeout_exc: type[BaseException]
    ) -> None:
        """Signal SIGINT, then escalate to SIGTERM then SIGKILL across the process tree,
        waiting (via a worker thread) for ``wait_fn(timeout)`` between escalations.

        ``wait_fn`` is the blocking wait for the target (``Popen.wait`` or ``psutil.Process.wait``);
        ``timeout_exc`` is the timeout it raises (``subprocess.TimeoutExpired`` /
        ``psutil.TimeoutExpired``).
        """
        _signal_process_tree(pid, signal.SIGINT)
        try:
            await asyncio.to_thread(wait_fn, _STOP_SIGINT_GRACE_S)
        except timeout_exc:
            _signal_process_tree(pid, signal.SIGTERM)
            try:
                await asyncio.to_thread(wait_fn, _STOP_SIGTERM_GRACE_S)
            except timeout_exc:
                _signal_process_tree(pid, signal.SIGKILL)
                await asyncio.to_thread(wait_fn, 1.0)

    async def start_app(self, name: str) -> dict[str, Any]:
        """Start an app. Returns status dict or error dict."""
        if name not in self.APPS:
            return {"error": f"Unknown app: {name}", "status": 404}

        async with self._lock:
            app = self._apps[name]

            # Check for external live app (scan-based)
            found = self._scan_for_live_apps()
            if name in found:
                return {
                    "error": f"App {name} is already running (external)",
                    "name": name,
                    "status": "refused",
                }

            # Refuse if any app is already running or loading
            if self.any_app_active():
                return {
                    "error": "Another app is already running",
                    "name": name,
                    "status": "refused",
                }

            # A failed pump shutdown retains ownership of its process and file.
            # Never launch or overwrite another pump until that ownership clears.
            if self._has_retained_log_ownership():
                return {
                    "error": "An app log pump is still shutting down",
                    "name": name,
                    "status": "refused",
                }

            # Check daemon lock status (if proxy available). An unreachable
            # daemon (None) is a refusal too: the app's ReachyMini() cannot
            # connect, so spawning it just manufactures an error card.
            if self.daemon_proxy is not None:
                lock_status = await self.daemon_proxy.get_robot_app_lock_status()
                if lock_status is None:
                    return {
                        "error": "Reachy daemon is offline — start it first.",
                        "name": name,
                        "status": "refused",
                    }
                if lock_status.get("state") != "free":
                    return {
                        "error": f"Robot held by {lock_status.get('state')}: {lock_status.get('holder_name')}",
                        "name": name,
                        "status": "refused",
                    }

            # Launch the app module directly with the shared interpreter. Keep
            # cwd=app_dir because apps load resources relative to their own directory.
            app_dir = self._workspace_root / "apps" / name
            argv = [sys.executable, "-m", f"{name}.main"]

            # Generate unique ready file path for this app launch
            ready_path = Path(tempfile.gettempdir()) / f"reachy_ready_{name}_{uuid.uuid4().hex}"
            app.ready_file = ready_path

            # Every launch truncates its file, clears the prior ring, and immediately
            # starts a dedicated drain for the merged stdout/stderr pipe.
            log_file_path = self._workspace_root / ".reachy-logs" / f"{name}.log"
            self.log_broker.set_file(name, log_file_path)
            self.log_broker.reset(name, status="empty")

            try:
                app.process = subprocess.Popen(
                    argv,
                    cwd=app_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env={
                        **os.environ,
                        "REACHY_READY_FILE": str(ready_path),
                        # Unbuffered so the pump receives each child line promptly.
                        "PYTHONUNBUFFERED": "1",
                    },
                )
                app.log_pump = ProcessLogPump(app.process, name, self.log_broker, log_file_path)
                app.log_handle = app.log_pump.file_handle
                app.pid = app.process.pid
                app.status = "loading"
                app.error = None
                app.start_time = time.time()
                logger.info(f"Started app {name} (pid {app.pid}) in loading state")

                # Spawn reaper task
                reaper_task = asyncio.create_task(self._reap_app(name))
                self._reaper_tasks[name] = reaper_task

                # Spawn readiness monitor task
                if name == "reachy_hermes":
                    monitor_task = asyncio.create_task(
                        self._monitor_readiness(name, _hermes_startup_budget())
                    )
                else:
                    monitor_task = asyncio.create_task(self._monitor_readiness(name))
                self._readiness_tasks[name] = monitor_task

                return {
                    "name": name,
                    "status": "loading",
                    "pid": app.pid,
                }
            except asyncio.CancelledError:
                raise
            except Exception as e:
                app.status = "error"
                app.error = str(e)
                logger.error(f"Failed to start app {name}: {e}")
                await asyncio.to_thread(self._close_log_handle, app)
                if app.process is not None:
                    try:
                        app.process.kill()
                        await asyncio.to_thread(app.process.wait, 1.0)
                    except Exception:
                        pass
                    app.process = None
                    app.pid = None
                return {"error": str(e), "name": name, "status": "error"}

    async def stop_app(self, name: str) -> dict[str, Any]:
        """Stop an app gracefully. Returns status dict."""
        if name not in self.APPS:
            return {"error": f"Unknown app: {name}", "status": 404}

        async with self._lock:
            app = self._apps[name]

            if app.status == "stopped":
                return {"name": name, "status": "stopped"}

            app.status = "stopping"

            # Cancel pending readiness monitor if present
            if name in self._readiness_tasks:
                task = self._readiness_tasks.pop(name)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            parent_exited = False
            try:
                # Handle subprocess.Popen handle (started by this supervisor)
                if app.process is not None:
                    # The app receives SIGINT directly; signal the whole tree too so any
                    # child subprocesses participate in graceful shutdown (#26).
                    await self._escalate_stop(
                        app.process.pid, app.process.wait, subprocess.TimeoutExpired
                    )
                    parent_exited = True
                    logger.info(f"Stopped app {name} (via subprocess handle)")
                elif app.pid is not None:
                    # Handle adopted orphaned process (reconcile_orphans set pid, process=None)
                    try:
                        proc = psutil.Process(app.pid)
                        # Verify it's still the right process
                        cmdline = proc.cmdline() or []
                        if self._cmdline_matches_module(cmdline, name):
                            # Same process-tree rationale as above: adopted apps may have children.
                            await self._escalate_stop(app.pid, proc.wait, psutil.TimeoutExpired)
                            logger.info(f"Stopped adopted app {name} (pid {app.pid})")
                        else:
                            logger.warning(
                                f"PID {app.pid} no longer matches {name}.main (cmdline: {' '.join(cmdline)})"
                            )
                    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                        logger.info(f"Adopted process {name} (pid {app.pid}) already exited: {e}")
            except Exception as e:
                logger.error(f"Error stopping app {name}: {e}")

            try:
                # Do not release process ownership until the pump has fully
                # stopped and closed its file. A failed join is retryable.
                await asyncio.to_thread(self._close_log_handle, app, parent_exited)
            except Exception as e:
                app.error = str(e)
                logger.error(f"Error closing log pump for {name}: {e}")
                return {"error": str(e), "name": name, "status": "stopping"}

            app.status = "stopped"
            app.error = None
            app.pid = None
            app.process = None
            app.external = False
            self.log_broker.set_status(name, "stopped")

            # Clean up ready file
            if app.ready_file is not None:
                stage_file = Path(f"{app.ready_file}.stage")
                try:
                    app.ready_file.unlink(missing_ok=True)
                except Exception as e:
                    logger.debug(f"Failed to clean up ready file for {name}: {e}")
                try:
                    stage_file.unlink(missing_ok=True)
                except Exception as e:
                    logger.debug(f"Failed to clean up stage file for {name}: {e}")
                app.ready_file = None

            return {"name": name, "status": "stopped"}

    def stop_all(self) -> None:
        """Stop all launched apps synchronously (call via ``asyncio.to_thread``)."""
        for app in self._apps.values():
            if (app.process is not None or app.log_pump is not None) and app.status in (
                "loading",
                "running",
                "stopping",
                "error",
            ):
                parent_exited = False
                if app.process is not None:
                    try:
                        app.process.terminate()
                        app.process.wait(timeout=2.0)
                        parent_exited = True
                    except Exception:
                        try:
                            app.process.kill()
                            app.process.wait(timeout=1.0)
                            parent_exited = True
                        except Exception:
                            pass
                try:
                    self._close_log_handle(app, parent_exited)
                except Exception as e:
                    app.status = "stopping"
                    app.error = str(e)
                    logger.error(f"Error closing log pump for {app.name}: {e}")
                    continue

                app.status = "stopped"
                app.error = None
                app.process = None
                app.pid = None
                app.external = False
                self.log_broker.set_status(app.name, "stopped")
                if app.ready_file is not None:
                    stage_file = Path(f"{app.ready_file}.stage")
                    try:
                        app.ready_file.unlink(missing_ok=True)
                    except OSError:
                        pass
                    try:
                        stage_file.unlink(missing_ok=True)
                    except OSError:
                        pass
                    app.ready_file = None

    async def aclose(self) -> None:
        """Stop launched children without blocking the controller event loop."""
        await asyncio.to_thread(self.stop_all)

    async def _monitor_readiness(
        self, name: str, hermes_startup_budget_s: float | None = None
    ) -> None:
        """Monitor app readiness and promote from loading to running.

        Uses a multi-strategy approach:
        1. Primary: ready file signal. Apps write to REACHY_READY_FILE when truly ready.
        2. Secondary: daemon lock (best-effort early-promote, won't fire for direct launches).
        3. Ceiling: timeout (_APP_READINESS_TIMEOUT_S) as fallback, logs WARNING if hit.
        """
        app = self._apps[name]
        is_hermes = name == "reachy_hermes"
        if is_hermes and hermes_startup_budget_s is None:
            hermes_startup_budget_s = _HERMES_STARTUP_BUDGET_S
        if app.start_time is None:
            logger.error(f"_monitor_readiness for {name}: start_time is None")
            return

        # Measure the readiness window with a monotonic clock, not wall-clock
        # start_time: an NTP step / DST change mid-boot must not distort `elapsed`.
        # (app.start_time stays wall-clock — the frontend reads it as a timestamp.)
        start_mono = time.monotonic()

        # Identity of the launch this monitor watches. The Hermes budget-timeout
        # branch re-checks it after stop_app to avoid clobbering a concurrent relaunch.
        launch_process = app.process
        # Captured while the loop is running; asyncio.current_task() in the finally
        # would raise if the coroutine is garbage-collected without a running loop.
        current_task = asyncio.current_task()

        try:
            while True:
                elapsed = time.monotonic() - start_mono

                # Check primary: ready file signal
                if app.ready_file is not None and app.ready_file.exists():
                    logger.info(f"App {name} signalled ready")
                    break

                # Try secondary: daemon lock (best-effort, won't fire for direct launches)
                if not is_hermes and self.daemon_proxy is not None:
                    try:
                        lock_status = await self.daemon_proxy.get_robot_app_lock_status()
                        if (
                            lock_status is not None
                            and lock_status.get("state") == "local_app"
                            and lock_status.get("holder_name") == name
                        ):
                            logger.info(f"App {name} acquired lock early, promoting to running")
                            break
                    except Exception as e:
                        # Best-effort; swallow exceptions
                        logger.debug(f"Lock check for {name} failed (best-effort): {e}")

                # Check ceiling timeout
                if is_hermes and elapsed >= hermes_startup_budget_s:
                    stage = self._read_startup_stage(app)
                    error = (
                        f"Hermes startup timed out after {hermes_startup_budget_s:g}s "
                        f"(stage: {stage}, marker: {app.ready_file})"
                    )
                    logger.error(error)
                    # De-register before stop_app so its readiness-task cancel does
                    # not abort this task mid-error-write. The finally below re-checks
                    # identity, so it won't evict a relaunch's fresh monitor.
                    self._readiness_tasks.pop(name, None)
                    await self.stop_app(name)
                    async with self._lock:
                        # stop_app released the lock; a relaunch may have installed a
                        # fresh process and flipped status back to loading/running.
                        # Only record the timeout error if this launch still owns the
                        # app — never clobber the newer launch.
                        if app.status not in ("loading", "running") and (
                            app.process is None or app.process is launch_process
                        ):
                            app.status = "error"
                            app.error = error
                            self.log_broker.set_status(name, "error")
                    return

                if not is_hermes and elapsed >= _APP_READINESS_TIMEOUT_S:
                    logger.warning(
                        f"App {name} readiness signal never arrived within {_APP_READINESS_TIMEOUT_S}s, promoting by fallback ceiling"
                    )
                    break

                # Sleep before polling again
                await asyncio.sleep(_APP_READINESS_POLL_S)

            # Promote: re-check status under lock before modifying
            async with self._lock:
                if app.status == "loading":
                    app.status = "running"
                    # Reset start_time to measure running duration, not boot duration (issue #15 requirement)
                    app.start_time = time.time()
                    logger.info(f"App {name} promoted to running")

            # Clean up ready file after promotion (served its purpose)
            if app.ready_file is not None:
                stage_file = Path(f"{app.ready_file}.stage")
                try:
                    app.ready_file.unlink(missing_ok=True)
                except Exception as e:
                    logger.debug(f"Failed to clean up ready file for {name}: {e}")
                try:
                    stage_file.unlink(missing_ok=True)
                except Exception as e:
                    logger.debug(f"Failed to clean up stage file for {name}: {e}")
                app.ready_file = None
        except asyncio.CancelledError:
            # Task was cancelled during stop_app; expected
            logger.debug(f"Readiness monitor for {name} cancelled")
            raise
        except Exception as e:
            logger.error(f"Readiness monitor error for {name}: {e}")
        finally:
            # Only evict our own registration. The budget-timeout branch already
            # popped this task, so after a relaunch the registry holds the new
            # launch's monitor — identity-guard prevents evicting it.
            if self._readiness_tasks.get(name) is current_task:
                self._readiness_tasks.pop(name, None)

    @staticmethod
    def _read_startup_stage(app: LocalApp) -> str:
        """Read an app's best-effort startup stage marker."""
        if app.ready_file is None:
            return "unknown"
        try:
            stage = Path(f"{app.ready_file}.stage").read_text(encoding="utf-8").strip()
            return stage or "unknown"
        except OSError:
            return "unknown"

    async def _reap_app(self, name: str) -> None:
        """Monitor a child process and update status when it exits."""
        app = self._apps[name]
        process = app.process
        current_task = asyncio.current_task()
        if process is None:
            if self._reaper_tasks.get(name) is current_task:
                self._reaper_tasks.pop(name, None)
            return

        parent_exited = False
        exit_code: int | None = None
        wait_error: Exception | None = None
        try:
            exit_code = await asyncio.to_thread(process.wait)
            parent_exited = True
        except Exception as e:
            wait_error = e
            logger.error(f"Reaper error for {name}: {e}")

        async with self._lock:
            if self._reaper_tasks.get(name) is current_task:
                self._reaper_tasks.pop(name, None)
            # A stop/restart may have replaced this process while its wait
            # completed. Never mutate the replacement's state.
            if app.process is not process:
                return
            # stop_app owns cleanup while it holds the lifecycle lock. If its
            # pump join fails, leave all ownership intact for a retry.
            if app.status == "stopping":
                return
            exited_while_loading = app.status == "loading"

            readiness_task = self._readiness_tasks.pop(name, None)
            if readiness_task is not None and readiness_task is not current_task:
                readiness_task.cancel()
                try:
                    await readiness_task
                except asyncio.CancelledError:
                    pass

            try:
                await asyncio.to_thread(self._close_log_handle, app, parent_exited)
            except Exception as e:
                app.status = "error"
                app.error = str(e)
                logger.error(f"Error closing log pump for {name}: {e}")
                return

            app.process = None
            app.pid = None
            app.external = False
            # Only the hermes-exited-while-loading branch consumes the stage, and
            # it must be read before the cleanup below deletes the .stage file.
            startup_stage = (
                self._read_startup_stage(app)
                if name == "reachy_hermes" and exited_while_loading
                else "unknown"
            )
            if app.ready_file is not None:
                stage_file = Path(f"{app.ready_file}.stage")
                try:
                    app.ready_file.unlink(missing_ok=True)
                except Exception as e:
                    logger.debug(f"Failed to clean up ready file for {name}: {e}")
                try:
                    stage_file.unlink(missing_ok=True)
                except Exception as e:
                    logger.debug(f"Failed to clean up stage file for {name}: {e}")
                app.ready_file = None

            if wait_error is not None:
                app.status = "error"
                app.error = str(wait_error)
            elif name == "reachy_hermes" and exited_while_loading:
                app.status = "error"
                if exit_code == 0:
                    app.error = (
                        f"Exited cleanly (code 0) before signalling ready "
                        f"(stage: {startup_stage})"
                    )
                else:
                    app.error = (
                        f"Exited with code {exit_code} during startup "
                        f"(stage: {startup_stage})"
                    )
            elif exit_code == 0:
                app.status = "stopped"
                app.error = None
            else:
                app.status = "error"
                app.error = f"Exited with code {exit_code}"
            # Keep the log panel status in step with the app card.
            self.log_broker.set_status(name, "error" if app.status == "error" else "stopped")
            logger.info(f"App {name} exited (code {exit_code}, status {app.status})")

    def _scan_for_live_apps(self) -> dict[str, tuple[int, float]]:
        """Scan process table for live apps matching known patterns.

        Returns dict mapping app_name to (pid, create_time).
        Single-pass O(procs) iteration; first matching process per app wins.
        Catches psutil.NoSuchProcess/AccessDenied per-process.
        """
        found: dict[str, tuple[int, float]] = {}
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                # Match direct interpreter launches by exact module token via _cmdline_matches_module
                for app_name in self.APPS:
                    if self._cmdline_matches_module(cmdline, app_name):
                        if app_name not in found:
                            found[app_name] = (proc.info.get("pid"), proc.create_time())
                        break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # Process disappeared or we can't access it; continue scanning
                pass
        return found

    def reconcile_orphans(self) -> None:
        """Adopt orphaned app processes on startup without claiming their pipes."""
        found = self._scan_for_live_apps()
        for name, (pid, create_time) in found.items():
            app = self._apps[name]
            app.process = None
            app.pid = pid
            app.status = "running"
            app.start_time = create_time
            app.external = True
            self.log_broker.set_status(name, "unavailable")
            logger.info(f"Reconciled orphaned app {name} (pid {pid})")

    async def aget_all_status(self) -> list[dict[str, Any]]:
        """Reconcile process discovery without mutating app state off-loop.

        Process-table discovery is read-only and may be slow, so it runs in a
        worker while holding the lifecycle lock. The resulting snapshot is
        reconciled under that same lock, serializing it with start/stop.
        """
        async with self._lock:
            found = await asyncio.to_thread(self._scan_for_live_apps)
            for app in self._apps.values():
                if app.status != "running" or app.pid is None:
                    continue
                match = found.get(app.name)
                if match is not None and match[0] == app.pid:
                    continue
                logger.info(f"App {app.name} (pid {app.pid}) is dead; transition to stopped")
                app.status = "stopped"
                self.log_broker.set_status(app.name, "stopped")
                app.external = False
                app.pid = None

            for name, (pid, create_time) in found.items():
                app = self._apps[name]
                if app.status != "stopped" and not (app.status == "running" and app.pid is None):
                    continue
                app.pid = pid
                app.status = "running"
                app.process = None
                app.start_time = create_time
                self.log_broker.set_status(name, "unavailable")
                app.external = True
                logger.info(f"Adopted external app {name} (pid {pid})")
            return [app.to_dict() for app in self._apps.values()]

    def get_all_status(self) -> list[dict[str, Any]]:
        """Get status for all apps. Reconciles dead processes and adopts live external apps."""
        # Liveness check: if a process is marked running but is dead, transition it to stopped
        for app in self._apps.values():
            if app.status in ("running", "stopping") and app.pid is not None:
                try:
                    proc = psutil.Process(app.pid)
                    # Verify it's still the right process
                    cmdline = proc.cmdline() or []
                    if not self._cmdline_matches_module(cmdline, app.module):
                        # PID was reused by a different process; treat as dead
                        logger.warning(
                            f"PID {app.pid} no longer runs {app.module}.main; transition to stopped"
                        )
                        app.status = "stopped"
                        self.log_broker.set_status(app.name, "stopped")
                        app.external = False
                        app.pid = None
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    # Process is dead; transition to stopped
                    logger.info(f"App {app.name} (pid {app.pid}) is dead; transition to stopped")
                    app.status = "stopped"
                    self.log_broker.set_status(app.name, "stopped")
                    app.external = False
                    app.pid = None

        # Adoption pass: scan for any stopped/pid-less app that is live in process table
        found = self._scan_for_live_apps()
        for name, (pid, create_time) in found.items():
            app = self._apps[name]
            # Adopt when stopped (the common case, after the liveness pass above) OR
            # when running-but-pid-less (a reconcile_orphans handle that lost its pid).
            # Keep both conditions: the `pid is None` arm is the recovery path for that
            # second case and must not be dropped as "redundant".
            if app.status == "stopped" or app.pid is None:
                app.pid = pid
                app.status = "running"
                app.process = None
                app.start_time = create_time
                self.log_broker.set_status(name, "unavailable")
                app.external = True
                logger.info(f"Adopted external app {name} (pid {pid})")

        return [app.to_dict() for app in self._apps.values()]

    def get_status(self, name: str) -> dict[str, Any] | None:
        """Get status for one app, or None if unknown."""
        if name not in self._apps:
            return None
        return self._apps[name].to_dict()
