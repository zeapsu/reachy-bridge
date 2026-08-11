"""Process-environment boundary for Console-owned durable credentials."""

from __future__ import annotations

import os


def child_process_env() -> dict[str, str]:
    """Copy the host environment without Console-owned provider credentials."""
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    return environment
