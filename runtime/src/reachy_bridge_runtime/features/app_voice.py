"""Install the shim that adds a browser's voice to an app's mic read (#219)."""

from __future__ import annotations

from pathlib import Path

SHIM = Path(__file__).with_name("app_voice_shim.py")
MODULE = "reachy_bridge_app_voice"


def install(site_packages: Path) -> None:
    (site_packages / f"{MODULE}.py").write_text(SHIM.read_text())
    (site_packages / f"{MODULE}.pth").write_text(f"import {MODULE}\n")


def remove(site_packages: Path) -> None:
    (site_packages / f"{MODULE}.pth").unlink(missing_ok=True)
    (site_packages / f"{MODULE}.py").unlink(missing_ok=True)
