"""Any other Linux board: the stock daemon with stock GStreamer plugins."""

from reachy_bridge_runtime import audio_tee
from reachy_bridge_runtime.platforms import Platform

# The speaker tee needs upstream's ~/.asoundrc and a loopback card; it does
# nothing without them.
PLATFORM = Platform(name="generic-linux", tier="experimental", host_setup=(audio_tee.ensure,))
