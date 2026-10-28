"""Keep app UIs and WebRTC signalling off the network (#186).

Apps serve their UIs on their own ports and bind every interface
(``custom_app_url`` is usually ``http://0.0.0.0:8042``; Gradio apps pass
``server_name="0.0.0.0"``), and the daemon's webrtcsink runs its signalling
server on ``0.0.0.0:8443``. Anyone on the LAN could open a running app and
drive the robot without the controller's login.

App processes run apps_venv's own Python, so an in-process patch can't
reach them. Before each app start, the bridge writes a small stdlib-only
module and a ``.pth`` that imports it into the app's venv
(``app_bind_shim.py``): every Python process in that venv binds its TCP
servers on loopback, whatever address it asks for. The controller's app proxy
is then the only way in, behind the login on the LAN. An app is refused, not
started open, if the shim can't be written. The bridge's own venv is never
touched. Native listeners (C extensions, non-Python children) are not
covered; see docs/daemon-contract.md.

The signalling server is in the daemon's process, so it is patched in
process: webrtcsink gets ``signalling-server-host=127.0.0.1``. Its only
client on this setup is the daemon's central relay, on ``127.0.0.1:8443``.
A webrtcsink without that property fails the media server rather than
serving signalling on every interface.

This feature fails closed: if reachy-mini no longer matches what it patches,
the runtime refuses to start (main.FATAL_FEATURES) instead of running apps
open on the LAN. install.sh runs ``--check`` first, so a mismatched SDK is
refused before it is installed.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from reachy_bridge_runtime import upstream

logger = logging.getLogger(__name__)

FEATURE = "app_bind"

# Read as text and written into the apps venv, never imported here: it
# patches every socket of the process that imports it.
SHIM = Path(__file__).with_name("app_bind_shim.py")
MODULE = "reachy_bridge_app_bind"
PTH = f"{MODULE}.pth"
SIGNALLING_HOST = "127.0.0.1"


def shim_source() -> str:
    return SHIM.read_text(encoding="utf-8")


def _venv_version(venv: Path) -> str | None:
    """``3.12`` from the venv's pyvenv.cfg, if it says."""
    try:
        lines = (venv / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, _, value = line.partition("=")
        if key.strip() in ("version_info", "version"):
            return ".".join(value.strip().split(".")[:2])
    return None


def site_packages(python: Path) -> Path:
    """The site-packages of the venv that ``python`` (``<venv>/bin/python``) belongs to."""
    venv = Path(python).parent.parent
    version = _venv_version(venv)
    if version:
        target = venv / "lib" / f"python{version}" / "site-packages"
        if target.is_dir():
            return target
    found = sorted(venv.glob("lib/python3.*/site-packages"))
    if len(found) != 1:
        raise RuntimeError(
            f"cannot tell the site-packages of {venv}: pyvenv.cfg names {version or 'no version'}, "
            f"and {len(found)} lib/python3.*/site-packages exist"
        )
    return found[0]


def _write(path: Path, text: str) -> None:
    if path.is_file() and not path.is_symlink() and path.read_text(encoding="utf-8") == text:
        return
    # A fresh, exclusively created temp file, then an atomic rename over the
    # target (which replaces a symlink rather than writing through it).
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def install_shim(python: Path) -> Path:
    """Write the shim and its ``.pth`` next to the app venv's packages."""
    target = site_packages(python)
    _write(target / f"{MODULE}.py", shim_source())
    _write(target / PTH, f"import {MODULE}\n")
    return target


async def _start_app(original: Any, manager: Any, app_name: str, *args: Any, **kwargs: Any) -> Any:
    python = upstream.app_python(manager, app_name)
    if Path(python).parent.parent == Path(sys.prefix):
        raise RuntimeError(
            f"Not starting {app_name}: it would run in the bridge's own venv, where its UI "
            "can't be kept off the network; start the daemon with --desktop-app-daemon"
        )
    try:
        install_shim(python)
    except Exception as exc:
        logger.exception("Not starting %s: could not keep its UI on loopback", app_name)
        raise RuntimeError(f"Not starting {app_name}: could not keep its UI on loopback ({exc})") from exc
    return await original(manager, app_name, *args, **kwargs)


def _configure_webrtc(original: Any, media_server: Any, *args: Any, **kwargs: Any) -> Any:
    webrtcsink = original(media_server, *args, **kwargs)
    if webrtcsink.find_property("signalling-server-host") is None:
        raise RuntimeError("webrtcsink has no signalling-server-host; not serving signalling on every interface")
    webrtcsink.set_property("signalling-server-host", SIGNALLING_HOST)
    return webrtcsink


def apply() -> None:
    upstream.wrap_start_app(_start_app)
    upstream.wrap_configure_webrtc(_configure_webrtc)
