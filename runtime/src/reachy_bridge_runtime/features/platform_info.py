"""Report the detected board to the controller and the device check (#140).

- ``GET /api/bridge/platform`` returns the platform adapter that matched, its
  support tier, the board model string, the bridge features that are active,
  and the ones startup skipped (an upstream mismatch or a failed patch).

It's read-only and installed after the other features, so it can say which of
them were skipped.
"""

from __future__ import annotations

from typing import Any

from reachy_bridge_runtime import platforms, upstream

FEATURE = "platform_info"


def payload(platform: platforms.Platform, active: list[str], skipped: list[str]) -> dict[str, Any]:
    return {
        "name": platform.name,
        "tier": platform.tier,
        "model": platforms.device_model().strip("\x00\n "),
        "features": sorted(active),
        "skipped": sorted(skipped),
        "reachy_mini": upstream.installed_version(),
    }


def apply(platform: platforms.Platform, active: list[str], skipped: list[str]) -> None:
    info = payload(platform, active, skipped)

    def install_route(app: Any) -> None:
        @app.get("/api/bridge/platform", include_in_schema=False)
        async def get_platform() -> dict[str, Any]:
            return info

    upstream.on_create_app(install_route)
