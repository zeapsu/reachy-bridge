"""Keep app UIs and WebRTC signalling off the network (#186).

Apps serve their UIs on their own ports and bind every interface
(``custom_app_url`` is usually ``http://0.0.0.0:8042``; Gradio apps pass
``server_name="0.0.0.0"``), and the daemon's webrtcsink runs its signalling
server on ``0.0.0.0:8443``. Anyone on the LAN could open a running app and
drive the robot without the controller's login.

App processes run apps_venv's own Python, so an in-process patch can't
reach them. Before each app start, the bridge writes a small stdlib-only
module and a ``.pth`` that imports it into the app's venv
(``app_bind_shim.py``), and sets ``REACHY_BRIDGE_APP_BIND=loopback`` in the
environment the app inherits: the app's wildcard TCP binds become loopback
binds. The controller's app proxy is then the only way in, behind the login
on the LAN. An app is refused, not started open, if the shim can't be
written. The bridge's own venv is never touched.

The signalling server is in the daemon's process, so it is patched in
process: webrtcsink gets ``signalling-server-host=127.0.0.1``. Its only
client on this setup is the daemon's central relay, on ``127.0.0.1:8443``.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from reachy_bridge_runtime import upstream

logger = logging.getLogger(__name__)

FEATURE = "app_bind"

# The shim isn't imported here: importing it would patch the daemon's own
# sockets whenever the variable is set.
SHIM = Path(__file__).with_name("app_bind_shim.py")
ENV = "REACHY_BRIDGE_APP_BIND"
MODULE = "reachy_bridge_app_bind"
PTH = f"{MODULE}.pth"
SIGNALLING_HOST = "127.0.0.1"


def shim_source() -> str:
    return SHIM.read_text(encoding="utf-8")


def site_packages(python: Path) -> Path:
    """The site-packages of the venv that ``python`` (``<venv>/bin/python``) belongs to."""
    venv = Path(python).parent.parent
    found = sorted(venv.glob("lib/python3.*/site-packages"))
    if len(found) != 1:
        raise RuntimeError(f"expected one site-packages in {venv}, found {len(found)}")
    return found[0]


def _write(path: Path, text: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
    webrtcsink.set_property("signalling-server-host", SIGNALLING_HOST)
    return webrtcsink


def apply() -> None:
    os.environ[ENV] = "loopback"
    upstream.wrap_start_app(_start_app)
    upstream.wrap_configure_webrtc(_configure_webrtc)
