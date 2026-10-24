"""Any other Linux board: the stock daemon with stock GStreamer plugins."""

from reachy_bridge_runtime.platforms import Platform

PLATFORM = Platform(name="generic-linux", tier="experimental")
