"""Run Pollen's daemon with the bridge's features and platform adapter.

    reachy-bridge-runtime [daemon args...]   start the daemon (args go to it)
    reachy-bridge-runtime --check            verify the upstream seam and exit

Patches are applied in-process and never edit the venv. At startup a feature
whose upstream requirements don't match the installed ``reachy-mini`` is
skipped with an error, so the robot stays usable without it; ``--check``
treats any mismatch as a failure (CI, and the SDK update pipeline in #155).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sys

from reachy_bridge_runtime import platforms, upstream
from reachy_bridge_runtime.features import keep_awake, kinematics, media, platform_info

logger = logging.getLogger("reachy_bridge_runtime")

# Feature name -> how to apply it. Order matters only in that every route
# installer is registered before the daemon builds its app.
FEATURES = {
    keep_awake.FEATURE: keep_awake.apply,
    kinematics.FEATURE: kinematics.apply,
    "audio_levels": media.apply_audio_spectrum,
    "camera": media.apply_camera_stream,
}
# On every platform; the rest only where the platform adapter enables them.
CORE_FEATURES = frozenset({keep_awake.FEATURE, kinematics.FEATURE})
# Features that add routes to the daemon app, so need the "routes" hooks.
ROUTE_FEATURES = frozenset({kinematics.FEATURE, "audio_levels", "camera"})
# Features that read the daemon's media; off under --no-media.
MEDIA_FEATURES = frozenset({"audio_levels", "camera"})


def for_daemon_args(platform: platforms.Platform, args: list[str]) -> platforms.Platform:
    """The platform as this daemon uses it. A sim daemon (sim mode, #149)
    runs beside the robot's, so it leaves the host's audio setup alone, and a
    --no-media daemon has no media for the media features to read."""
    if "--sim" in args or "--mockup-sim" in args:
        platform = dataclasses.replace(platform, host_setup=())
    if "--no-media" in args:
        platform = dataclasses.replace(platform, features=platform.features - MEDIA_FEATURES)
    return platform


def check() -> int:
    """Print the seam report as JSON; non-zero if anything doesn't match."""
    problems = upstream.check()
    report = {"reachy_mini": upstream.installed_version(), "ok": not problems, "problems": problems}
    print(json.dumps(report, indent=2))
    return 0 if not problems else 1


def apply(platform: platforms.Platform) -> list[str]:
    """Apply what this platform supports; return the features skipped."""
    wanted = CORE_FEATURES | platform.features
    problems = upstream.check(set(wanted) | {"daemon", "routes"})
    if "daemon" in problems:
        raise upstream.UpstreamMismatch("; ".join(problems["daemon"]))
    skipped = []
    for name, apply_feature in FEATURES.items():
        if name not in wanted:
            continue
        blocked = problems.get(name, []) + (problems.get("routes", []) if name in ROUTE_FEATURES else [])
        if blocked:
            logger.error(
                "Skipping %s: reachy-mini %s doesn't match what it patches: %s",
                name,
                upstream.installed_version(),
                "; ".join(blocked),
            )
            skipped.append(name)
            continue
        try:
            apply_feature()
        except Exception:
            # One broken feature mustn't keep the daemon (and the robot) down.
            logger.exception("Skipping %s: it failed to apply", name)
            skipped.append(name)
    return skipped


def report_platform(platform: platforms.Platform, skipped: list[str], routes_ok: bool) -> None:
    """Serve what startup did at ``/api/bridge/platform`` (#140). It adds a
    route, so it needs the "routes" hooks; without them it's left out.
    ``routes_ok`` must be checked before ``apply``: once a feature has wrapped
    the app factory, the wrapper's signature no longer matches the seam."""
    if not routes_ok:
        logger.error("Not reporting the platform: reachy-mini's app factory doesn't match")
        return
    active = [name for name in FEATURES if name in CORE_FEATURES | platform.features and name not in skipped]
    try:
        platform_info.apply(platform, active, skipped)
    except Exception:
        logger.exception("Not reporting the platform: it failed to apply")


def main() -> int:
    if "--check" in sys.argv[1:]:
        return check()
    logging.basicConfig(level=logging.INFO)
    platform = for_daemon_args(platforms.detect(), sys.argv[1:])
    logger.info(
        "reachy-bridge runtime on %s (%s), reachy-mini %s",
        platform.name,
        platform.tier,
        upstream.installed_version(),
    )
    for setup in platform.host_setup:
        setup()
    routes_ok = not upstream.check({"routes"})
    skipped = apply(platform)
    report_platform(platform, skipped, routes_ok)
    return upstream.run_daemon()


if __name__ == "__main__":
    sys.exit(main())
