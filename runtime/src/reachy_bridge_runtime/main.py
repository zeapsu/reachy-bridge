"""Run Pollen's daemon with the bridge's features and platform adapter.

    reachy-bridge-runtime [daemon args...]   start the daemon (args go to it)
    reachy-bridge-runtime --check            verify the upstream seam and exit

Patches are applied in-process and never edit the venv. At startup a feature
whose upstream requirements don't match the installed ``reachy-mini`` is
skipped with an error, so the robot stays usable without it; ``--check``
treats any mismatch as a failure (CI, and the SDK update pipeline in #155).
"""

from __future__ import annotations

import json
import logging
import sys

from reachy_bridge_runtime import platforms, upstream
from reachy_bridge_runtime.features import keep_awake, kinematics, media

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
        blocked = problems.get(name, []) + problems.get("routes", [])
        if blocked:
            logger.error(
                "Skipping %s: reachy-mini %s doesn't match what it patches: %s",
                name,
                upstream.installed_version(),
                "; ".join(blocked),
            )
            skipped.append(name)
            continue
        apply_feature()
    return skipped


def main() -> int:
    if "--check" in sys.argv[1:]:
        return check()
    logging.basicConfig(level=logging.INFO)
    platform = platforms.detect()
    logger.info(
        "reachy-bridge runtime on %s (%s), reachy-mini %s",
        platform.name,
        platform.tier,
        upstream.installed_version(),
    )
    for setup in platform.host_setup:
        setup()
    apply(platform)
    return upstream.run_daemon()


if __name__ == "__main__":
    sys.exit(main())
