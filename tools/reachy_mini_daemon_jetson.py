"""Launch the Reachy Mini daemon on NVIDIA Jetson with the unixfd element shim.

The Reachy Mini daemon's local-camera IPC branch creates a ``unixfdsink``
(``reachy_mini/media/media_server.py``). On NVIDIA Jetson (L4T/Tegra) that
element is missing — only NVIDIA's ``nvunixfdsink`` exists — so the camera IPC
socket (``/tmp/reachymini_camera_socket``) is never created and on-device apps
get no camera frames.

This wrapper installs a process-wide alias over ``Gst.ElementFactory.make`` so
the daemon transparently uses the NVIDIA variants, then hands off to the normal
daemon entry point (all CLI args are forwarded).

Usage (from the repo root, uses the root venv that has reachy-mini installed):

    uv run python tools/reachy_mini_daemon_jetson.py            # Lite over USB
    uv run python tools/reachy_mini_daemon_jetson.py --sim --headless

This is reinstall-proof: it patches at runtime and never edits the venv, so it
keeps working after ``uv sync`` / SDK upgrades. On non-Jetson hosts it is a
transparent pass-through to the daemon.

Mirrors the app-side shim in
``apps/reachy_chat/src/reachy_chat/gstreamer_nvidia_patch.py``.
"""

import logging
import platform
import sys

_NV_ALIASES = {"unixfdsrc": "nvunixfdsrc", "unixfdsink": "nvunixfdsink"}

logger = logging.getLogger("reachy_mini_daemon_jetson")


def _is_jetson() -> bool:
    if platform.system() != "Linux":
        return False
    try:
        with open("/proc/device-tree/model", "r") as f:
            model = f.read()
    except OSError:
        return False
    return "Tegra" in model or "NVIDIA" in model


def _apply_nvidia_unixfd_patch() -> None:
    if not _is_jetson():
        return
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError) as e:
        logger.warning("GStreamer unavailable; skipping Jetson unixfd patch: %s", e)
        return

    if not Gst.is_initialized():
        Gst.init(None)

    original_make = Gst.ElementFactory.make
    if getattr(original_make, "_nv_unixfd_patched", False):
        return

    def make(factory_name, name=None):
        nv_name = _NV_ALIASES.get(factory_name)
        if nv_name is not None:
            element = original_make(nv_name, name)
            if element is not None:
                return element
        return original_make(factory_name, name)

    make._nv_unixfd_patched = True
    Gst.ElementFactory.make = make
    logger.info("Jetson detected: aliased unixfd GStreamer elements to NVIDIA variants")


def _apply_webrtcsink_compat_patch() -> None:
    """Tolerate the older ``webrtcsink`` built for GStreamer 1.20 (gst-plugins-rs 0.10).

    The daemon's ``GstMediaServer._configure_webrtc`` sets ``run-signalling-server``
    on ``webrtcsink`` — a property that only exists on newer gst-plugins-rs
    (>= 0.12, which needs GStreamer >= 1.24). On this Jetson (GStreamer 1.20) the
    available ``webrtcsink`` lacks it, which aborts the whole media server and so
    the local camera IPC socket is never created.

    The on-device app consumes the camera via the IPC branch (nvunixfdsink), not
    WebRTC streaming, so we re-implement ``_configure_webrtc`` to skip that single
    unsupported property when it is absent. Everything else (the ``meta`` property
    and the consumer-added / consumer-removed / encoder-setup signals) is present
    in 0.10 and kept identical to upstream.
    """
    if not _is_jetson():
        return
    try:
        from reachy_mini.media.media_server import GstMediaServer
    except ImportError as e:
        logger.warning("Could not import GstMediaServer for webrtcsink compat patch: %s", e)
        return

    from gi.repository import Gst

    def _configure_webrtc(self, pipeline):
        webrtcsink = Gst.ElementFactory.make("webrtcsink")
        if not webrtcsink:
            raise RuntimeError(
                "Failed to create webrtcsink element. "
                "Is the GStreamer webrtc rust plugin installed?"
            )

        meta_structure = Gst.Structure.new_empty("meta")
        meta_structure.set_value("name", "reachymini")
        webrtcsink.set_property("meta", meta_structure)
        # `run-signalling-server` only exists on gst-plugins-rs >= 0.12 (GStreamer
        # >= 1.24). On GStreamer 1.20 (gst-plugins-rs 0.10) it is absent; skip it.
        if webrtcsink.find_property("run-signalling-server") is not None:
            webrtcsink.set_property("run-signalling-server", True)

        webrtcsink.connect("consumer-added", self._consumer_added)
        webrtcsink.connect("consumer-removed", self._consumer_removed)
        webrtcsink.connect("encoder-setup", self._encoder_setup)

        pipeline.add(webrtcsink)
        return webrtcsink

    GstMediaServer._configure_webrtc = _configure_webrtc
    logger.info("Patched GstMediaServer._configure_webrtc for gst-plugins-rs 0.10 compatibility")


def _apply_camera_ipc_patch() -> None:
    """Rebuild the camera IPC *producer* branch for Jetson's NVMM transport.

    Upstream ``_build_ipc_branch`` (non-RPi) feeds system-memory BGR buffers to
    ``unixfdsink`` via an ``identity drop-allocation`` + ``videoconvert`` memfd
    workaround. ``nvunixfdsink`` instead consumes NVMM (dmabuf-FD) buffers, so
    that workaround makes the camera source error out (``reason error (-5)``).

    We re-implement the branch to convert ``tee -> nvvidconv -> NVMM ->
    nvunixfdsink``. The matching consumer side lives in the app's
    ``gstreamer_nvidia_patch._patch_camera_ipc_source``.
    """
    if not _is_jetson():
        return
    try:
        from reachy_mini.media.media_server import GstMediaServer
        from reachy_mini.daemon.utils import CAMERA_SOCKET_PATH, is_local_camera_available
    except ImportError as e:
        logger.warning("Could not import for camera IPC patch: %s", e)
        return

    import os

    from gi.repository import Gst

    def _build_ipc_branch(self, tee, pipeline, *, is_rpi: bool = False):
        queue_ipc = Gst.ElementFactory.make("queue", "queue_ipc")
        pipeline.add(queue_ipc)
        tee.link(queue_ipc)

        ipc_sink = Gst.ElementFactory.make("nvunixfdsink")
        if ipc_sink is None:
            self._logger.warning(
                "nvunixfdsink not available. Local camera IPC will not work."
            )
            return
        if is_local_camera_available():
            os.remove(CAMERA_SOCKET_PATH)
        ipc_sink.set_property("socket-path", CAMERA_SOCKET_PATH)

        # nvunixfdsink consumes NVMM (dmabuf-FD) buffers; nvvidconv copies the
        # tee's system-memory frames into NVMM memory for the FD transport.
        nvconv = Gst.ElementFactory.make("nvvidconv", "ipc_nvvidconv")
        capsfilter_ipc = Gst.ElementFactory.make("capsfilter", "ipc_capsfilter")
        if nvconv is None or capsfilter_ipc is None:
            raise RuntimeError("Failed to create Jetson IPC branch elements")
        capsfilter_ipc.set_property(
            "caps", Gst.Caps.from_string("video/x-raw(memory:NVMM)")
        )

        for elem in (nvconv, capsfilter_ipc, ipc_sink):
            pipeline.add(elem)
        queue_ipc.link(nvconv)
        nvconv.link(capsfilter_ipc)
        capsfilter_ipc.link(ipc_sink)

    GstMediaServer._build_ipc_branch = _build_ipc_branch
    logger.info("Patched GstMediaServer._build_ipc_branch for Jetson NVMM camera transport")


def _find_reachy_audio_card() -> int | None:
    """Return the ALSA card number for the Reachy Mini Audio device."""
    try:
        with open("/proc/asound/cards", "r") as f:
            lines = f.readlines()
    except OSError:
        return None

    card_number = None
    card_text = []
    for line in lines:
        stripped = line.lstrip()
        number, separator, remainder = stripped.partition(" ")
        if separator and number.isdigit() and remainder.lstrip().startswith("["):
            if card_number is not None:
                description = " ".join(card_text).casefold()
                if "reachy mini audio" in description or "pollen" in description:
                    return card_number
            card_number = int(number)
            card_text = [remainder]
        elif card_number is not None:
            card_text.append(line)

    if card_number is not None:
        description = " ".join(card_text).casefold()
        if "reachy mini audio" in description or "pollen" in description:
            return card_number
    return None


def _set_reachy_audio_output_volume() -> None:
    """Discover the Reachy Mini USB audio card by name and set full volume.

    The Reachy Mini Audio USB device (alsa_output.usb-Pollen_Robotics_Reachy_Mini_Audio_...)
    has two PCM mixer controls:
    - PCM,0 (stereo, typically unused)
    - PCM,1 (mono playback to the speaker)

    The speaker is routed through PCM,1. If this control is below 100% (hardware-level
    attenuation, typically -20dB at 67%), the daemon's sound playback (wake-up, sleep,
    etc.) is inaudible. This patch ensures full volume at startup.

    This is a one-time ALSA mixer adjustment, not a PulseAudio control — it persists
    across daemon restarts and PulseAudio reconnects.
    """
    if not _is_jetson():
        return

    import subprocess

    try:
        card = _find_reachy_audio_card()
        if card is None:
            logger.warning("Reachy Mini Audio ALSA card not found; speaker volume not adjusted")
            return

        # Set PCM,1 (mono playback) to 100% on the Reachy Mini Audio card
        result = subprocess.run(
            ["amixer", "-c", str(card), "sset", "PCM,1", "100%"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            logger.info(
                "Set Reachy Mini Audio ALSA card %d PCM,1 (speaker) to 100%% volume", card
            )
        else:
            logger.warning(
                "amixer failed to set Reachy Audio volume: %s", result.stderr or result.stdout
            )
    except FileNotFoundError:
        logger.warning("amixer not found; Reachy Audio speaker volume may be low")
    except subprocess.TimeoutExpired:
        logger.warning("amixer timeout while setting Reachy Audio volume")
    except Exception as e:
        logger.warning("Error setting Reachy Audio volume: %s", e)


def _ensure_reachymini_asoundrc() -> None:
    """Provision ~/.asoundrc so audio bypasses PipeWire's flaky USB-audio
    profile picker (PipeWire sometimes only exposes a Sink for the "Reachy
    Mini Audio" card, silently starving mic capture — #65). Upstream only
    does this for --wireless-version; Lite hardware needs it here too.
    """
    if not _is_jetson():
        return
    try:
        from reachy_mini.media.audio_utils import (
            check_reachymini_asoundrc,
            write_asoundrc_to_home,
        )

        if check_reachymini_asoundrc():
            logger.info("~/.asoundrc already correctly configured for Reachy Mini Audio.")
            return
        write_asoundrc_to_home()
        logger.info("Wrote ~/.asoundrc for Reachy Mini Audio (ALSA-direct, bypassing PipeWire).")
    except Exception as e:
        logger.warning("Could not provision ~/.asoundrc for Reachy Mini Audio: %s", e)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    _apply_nvidia_unixfd_patch()
    _apply_webrtcsink_compat_patch()
    _apply_camera_ipc_patch()
    if _is_jetson():
        # Importing the extension is lazy so this wrapper remains a transparent
        # daemon pass-through on non-Jetson development machines.
        from reachy_mini_daemon_media import install_camera_stream_route

        install_camera_stream_route()
    _set_reachy_audio_output_volume()
    _ensure_reachymini_asoundrc()
    from reachy_mini.daemon.app.main import main as daemon_main

    return daemon_main()


if __name__ == "__main__":
    sys.exit(main())
