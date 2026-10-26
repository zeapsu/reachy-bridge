"""Sim test app that crashes on its own shortly after it starts.

The daemon marks a non-zero exit as ``error``, frees the robot slot and, 1.5 s
later, puts the robot to sleep (``docs/daemon-contract.md``, "Idle reset").
No UI: there is no ``custom_app_url``.
"""

import sys
import time


class App:
    """Entry-point target; the daemon runs this module, not the class."""


def main() -> None:
    time.sleep(1)
    print("sim_crash_app: simulated crash", file=sys.stderr, flush=True)
    sys.exit(1)


if __name__ == "__main__":
    main()
