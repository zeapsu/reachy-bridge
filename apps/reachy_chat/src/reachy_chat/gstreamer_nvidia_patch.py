"""Alias standard unixfd GStreamer elements to NVIDIA Jetson variants.

On NVIDIA Jetson (L4T/Tegra) the upstream ``unixfdsrc`` / ``unixfdsink``
elements (from ``gstreamer1.0-plugins-bad``) are not available. NVIDIA instead
ships drop-in replacements ``nvunixfdsrc`` / ``nvunixfdsink`` that expose the
same ``socket-path`` property.

The reachy_mini SDK hardcodes the standard element names in two places:
  * the **daemon** producer side (``media_server.py`` -> ``unixfdsink``), and
  * the **app** client side (``camera_gstreamer.py`` -> ``unixfdsrc``).

On Jetson both ``Gst.ElementFactory.make(...)`` calls return ``None``, so the
local camera IPC socket is never created and no frames flow.

This module installs a process-wide shim over ``Gst.ElementFactory.make`` that
transparently substitutes the NVIDIA variants. It must be applied **before** the
relevant GStreamer pipeline is built:
  * in the app, call ``apply_nvidia_unixfd_patch()`` at startup (see ``main.py``);
  * for the daemon, launch it via the Jetson wrapper which applies the same shim
    (``tools/reachy_mini_daemon_jetson.py`` at the repo root).

It is idempotent and a safe no-op on non-Jetson platforms.
"""

import logging
import platform

_NV_ALIASES = {"unixfdsrc": "nvunixfdsrc", "unixfdsink": "nvunixfdsink"}

_logger = logging.getLogger(__name__)


def _is_jetson() -> bool:
    """Return True when running on an NVIDIA Jetson (L4T/Tegra) board."""
    if platform.system() != "Linux":
        return False
    try:
        with open("/proc/device-tree/model", "r") as f:
            model = f.read()
    except OSError:
        return False
    return "Tegra" in model or "NVIDIA" in model


def apply_nvidia_unixfd_patch() -> None:
    """Alias ``unixfd*`` GStreamer elements to ``nvunixfd*`` on Jetson.

    Idempotent; no-op on non-Jetson platforms or if GStreamer is unavailable.
    """
    if not _is_jetson():
        return

    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError) as e:
        _logger.warning("GStreamer unavailable; skipping Jetson unixfd patch: %s", e)
        return

    if not Gst.is_initialized():
        Gst.init(None)

    original_make = Gst.ElementFactory.make
    if getattr(original_make, "_nv_unixfd_patched", False):
        return  # already patched in this process

    def make(factory_name, name=None):
        nv_name = _NV_ALIASES.get(factory_name)
        if nv_name is not None:
            element = original_make(nv_name, name)
            if element is not None:
                return element
            # NVIDIA variant unexpectedly missing — fall back to the standard name.
        return original_make(factory_name, name)

    make._nv_unixfd_patched = True
    Gst.ElementFactory.make = make
    _logger.info(
        "Detected NVIDIA Jetson: aliased unixfd GStreamer elements to NVIDIA variants "
        "(unixfdsrc->nvunixfdsrc, unixfdsink->nvunixfdsink)"
    )

    _patch_camera_ipc_source(Gst)


def _patch_camera_ipc_source(Gst) -> None:
    """Rebuild the camera IPC *source* for Jetson's NVMM transport.

    ``nvunixfdsrc`` delivers NVMM (dmabuf-FD) buffers, which the upstream
    pipeline (``unixfdsrc -> queue -> videoconvert -> appsink``) cannot consume
    because ``videoconvert`` only handles system memory. We re-implement
    ``GStreamerCamera._build_ipc_source`` to insert an ``nvvidconv`` that copies
    NVMM -> system memory. ``nvvidconv`` cannot output packed ``BGR`` directly,
    so we go NVMM -> ``BGRx`` (system) -> ``videoconvert`` -> ``BGR`` to satisfy
    the appsink's ``video/x-raw,format=BGR`` caps.
    """
    try:
        from reachy_mini.media.camera_gstreamer import GStreamerCamera
    except ImportError as e:
        _logger.warning("Could not import GStreamerCamera for Jetson camera patch: %s", e)
        return

    def _build_ipc_source(self) -> None:
        from reachy_mini.daemon.utils import CAMERA_SOCKET_PATH

        camsrc = Gst.ElementFactory.make("nvunixfdsrc")
        if camsrc is None:
            raise RuntimeError(
                "Failed to create nvunixfdsrc (NVIDIA Jetson unixfd plugin)."
            )
        camsrc.set_property("socket-path", CAMERA_SOCKET_PATH)

        queue = Gst.ElementFactory.make("queue")
        nvconv = Gst.ElementFactory.make("nvvidconv")
        capsfilter = Gst.ElementFactory.make("capsfilter")
        videoconvert = Gst.ElementFactory.make("videoconvert")
        if None in (queue, nvconv, capsfilter, videoconvert):
            raise RuntimeError("Failed to create Jetson camera IPC source elements")

        # nvvidconv: NVMM -> system memory BGRx (it cannot emit packed BGR).
        capsfilter.set_property("caps", Gst.Caps.from_string("video/x-raw,format=BGRx"))

        for elem in (camsrc, queue, nvconv, capsfilter, videoconvert):
            self.pipeline.add(elem)

        camsrc.link(queue)
        queue.link(nvconv)
        nvconv.link(capsfilter)
        capsfilter.link(videoconvert)
        # videoconvert: BGRx -> BGR to satisfy the appsink caps.
        videoconvert.link(self._appsink_video)

    GStreamerCamera._build_ipc_source = _build_ipc_source
    _logger.info("Patched GStreamerCamera._build_ipc_source for Jetson NVMM camera transport")
