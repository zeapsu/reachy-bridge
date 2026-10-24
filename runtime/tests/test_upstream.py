"""The upstream seam: the installed reachy-mini matches it, and mismatches are
reported per feature instead of half-applied."""

from __future__ import annotations

import sys
import types

import pytest

from reachy_bridge_runtime import main as runtime_main
from reachy_bridge_runtime import platforms, upstream


def test_installed_reachy_mini_matches_the_seam() -> None:
    # The same check CI and the SDK update pipeline (#155) run via --check.
    assert upstream.check() == {}


def test_check_reports_a_missing_symbol_against_its_feature(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = types.ModuleType("reachy_mini.apps.manager")
    manager.AppManager = type("AppManager", (), {})  # stop_current_app is gone
    monkeypatch.setitem(sys.modules, "reachy_mini.apps.manager", manager)
    problems = upstream.check()
    assert list(problems) == ["keep_awake"]
    assert "no attribute 'stop_current_app'" in problems["keep_awake"][0]


def test_check_reports_a_changed_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    class Daemon:
        robot_app_lock = backend = _media_server = media_released = None

        async def start(self, sim: bool = False) -> None:  # kinematics_engine dropped
            self.robot_app_lock = self.backend = None

    module = types.ModuleType("reachy_mini.daemon.daemon")
    module.Daemon = Daemon
    monkeypatch.setitem(sys.modules, "reachy_mini.daemon.daemon", module)
    problems = upstream.check({"kinematics"})
    assert any("kinematics_engine" in p for p in problems["kinematics"])


def test_startup_skips_only_the_mismatched_feature(monkeypatch: pytest.MonkeyPatch) -> None:
    applied: list[str] = []
    monkeypatch.setattr(
        upstream, "check", lambda features=None: {"keep_awake": ["AppManager.stop_current_app: gone"]}
    )
    monkeypatch.setattr(upstream, "installed_version", lambda: "9.9.9")
    monkeypatch.setattr(
        runtime_main,
        "FEATURES",
        {name: (lambda name=name: applied.append(name)) for name in runtime_main.FEATURES},
    )
    skipped = runtime_main.apply(platforms.Platform(name="test", tier="tested"))
    assert skipped == ["keep_awake"]
    assert applied == ["kinematics"]


def test_a_daemon_mismatch_stops_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upstream, "check", lambda features=None: {"daemon": ["main: gone"]})
    with pytest.raises(upstream.UpstreamMismatch):
        runtime_main.apply(platforms.Platform(name="test", tier="tested"))


def test_wrapping_twice_with_the_same_function_wraps_once() -> None:
    class Target:
        async def start(self) -> list[str]:
            return ["original"]

    async def wrapper(original, self):
        return ["wrapped", *await original(self)]

    upstream._wrap(Target, "start", wrapper)
    upstream._wrap(Target, "start", wrapper)
    import asyncio

    assert asyncio.run(Target().start()) == ["wrapped", "original"]


def test_platform_detection() -> None:
    from reachy_bridge_runtime.platforms import jetson

    assert jetson.matches("NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super")
    assert not jetson.matches("Raspberry Pi 5 Model B Rev 1.0")


def test_a_feature_that_fails_to_apply_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upstream, "check", lambda features=None: {})

    def broken() -> None:
        raise RuntimeError("boom")

    features = {name: (lambda: None) for name in runtime_main.FEATURES}
    features["kinematics"] = broken
    monkeypatch.setattr(runtime_main, "FEATURES", features)
    assert runtime_main.apply(platforms.Platform(name="test", tier="tested")) == ["kinematics"]


def test_a_routes_mismatch_spares_features_without_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    applied: list[str] = []
    monkeypatch.setattr(upstream, "check", lambda features=None: {"routes": ["create_app: gone"]})
    monkeypatch.setattr(upstream, "installed_version", lambda: "9.9.9")
    monkeypatch.setattr(
        runtime_main,
        "FEATURES",
        {name: (lambda name=name: applied.append(name)) for name in runtime_main.FEATURES},
    )
    assert runtime_main.apply(platforms.Platform(name="test", tier="tested")) == ["kinematics"]
    assert applied == ["keep_awake"]
