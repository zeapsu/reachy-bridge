"""Tests for subprocess isolation from Console provider credentials."""

from __future__ import annotations

from types import SimpleNamespace

from reachy_controller import jetson_stats


def test_audio_metric_subprocesses_do_not_inherit_openai_key(monkeypatch) -> None:
    """Host metric helpers must not leak a Console-only provider credential."""
    captured_environments: list[dict[str, str]] = []

    def run(*_args, **kwargs):
        captured_environments.append(kwargs["env"])
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setenv("OPENAI_API_KEY", "durable-parent-key")
    monkeypatch.setattr("subprocess.run", run)

    assert jetson_stats._parse_alsa_pcm_volume(0) is None
    assert jetson_stats._parse_pipewire_sink_volume() is None
    assert len(captured_environments) == 2
    assert all("OPENAI_API_KEY" not in environment for environment in captured_environments)
