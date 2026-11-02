"""A sim daemon's sounds, recorded and served to the dashboard (#219)."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from reachy_bridge_runtime import upstream
from reachy_bridge_runtime.features import sim_sounds


@pytest.fixture(autouse=True)
def _log(monkeypatch: pytest.MonkeyPatch) -> sim_sounds.SoundLog:
    log = sim_sounds.SoundLog()
    monkeypatch.setattr(sim_sounds, "LOG", log)
    monkeypatch.setattr(upstream, "_app_installers", [])
    return log


@pytest.fixture
def assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "assets"
    folder.mkdir()
    (folder / "wake_up.wav").write_bytes(b"RIFF....WAVE")
    constants = types.ModuleType("reachy_mini.utils.constants")
    constants.ASSETS_ROOT_PATH = str(folder)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "reachy_mini.utils.constants", constants)
    # The daemon's sound folders: its assets and a dataset cache.
    monkeypatch.setattr(upstream, "sound_roots", lambda: (folder.resolve(), (tmp_path / "dataset").resolve()))
    return folder


def _app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    daemon_main = types.ModuleType("reachy_mini.daemon.app.main")
    daemon_main.create_app = lambda *_a, **_k: FastAPI()  # type: ignore[attr-defined]
    package = types.ModuleType("reachy_mini.daemon.app")
    package.main = daemon_main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "reachy_mini.daemon.app", package)
    monkeypatch.setitem(sys.modules, "reachy_mini.daemon.app.main", daemon_main)
    upstream.on_create_app(sim_sounds.install_route)
    return daemon_main.create_app()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://d")


def _play(name: str) -> list[str]:
    calls: list[str] = []
    sim_sounds._play_sound(lambda _backend, f: calls.append(f), object(), name)
    return calls


def test_a_bare_name_resolves_under_the_assets_folder(assets: Path, _log: sim_sounds.SoundLog) -> None:
    assert _play("wake_up.wav") == ["wake_up.wav"]
    [event] = _log.snapshot()["events"]
    assert event["kind"] == "play" and event["name"] == "wake_up.wav"
    assert _log.file(event["token"]) == ((assets / "wake_up.wav").resolve(), "audio/wav")


def test_an_absolute_path_is_kept(tmp_path: Path, assets: Path, _log: sim_sounds.SoundLog) -> None:
    move_sound = tmp_path / "dataset" / "cheerful1.wav"
    move_sound.parent.mkdir()
    move_sound.write_bytes(b"RIFF")
    _play(str(move_sound))
    [event] = _log.snapshot()["events"]
    assert _log.file(event["token"]) == (move_sound.resolve(), "audio/wav")


def test_a_cached_move_sound_keeps_the_type_of_its_name(
    tmp_path: Path, assets: Path, _log: sim_sounds.SoundLog
) -> None:
    # The Hugging Face cache links snapshots/<rev>/x.wav to blobs/<sha>.
    blob = tmp_path / "dataset" / "blobs" / "3f2a"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"RIFF")
    link = tmp_path / "dataset" / "snapshots" / "rev" / "cheerful1.wav"
    link.parent.mkdir(parents=True)
    link.symlink_to(blob)
    _play(str(link))
    [event] = _log.snapshot()["events"]
    assert event["name"] == "cheerful1.wav"
    assert _log.file(event["token"]) == (blob.resolve(), "audio/wav")


def test_missing_and_non_audio_files_are_not_recorded(tmp_path: Path, assets: Path, _log: sim_sounds.SoundLog) -> None:
    secret = tmp_path / "notes.txt"
    secret.write_text("x")
    # The original still runs: the daemon decides what a bad name means.
    assert _play("nope.wav") == ["nope.wav"]
    _play(str(secret))
    assert _log.snapshot()["seq"] == 0 and _log.snapshot()["events"] == []


def test_files_outside_the_daemons_sound_folders_are_not_recorded(
    tmp_path: Path, assets: Path, _log: sim_sounds.SoundLog
) -> None:
    private = tmp_path / "home" / "voicemail.m4a"
    private.parent.mkdir()
    private.write_bytes(b"x")
    # Absolute, or climbing out of the assets folder: both refused.
    _play(str(private))
    _play("../home/voicemail.m4a")
    assert _log.snapshot()["seq"] == 0 and _log.snapshot()["events"] == []


def test_a_failing_lookup_never_breaks_the_caller(assets: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upstream, "sound_path", lambda _f: (_ for _ in ()).throw(OSError("disk")))
    assert _play("wake_up.wav") == ["wake_up.wav"]


def test_stop_is_an_event(_log: sim_sounds.SoundLog) -> None:
    stopped: list[bool] = []
    sim_sounds._stop_sound(lambda _backend: stopped.append(True), object())
    assert stopped == [True]
    assert _log.snapshot()["events"] == [{"seq": 1, "kind": "stop"}]


def test_only_recent_events_and_files_are_kept(assets: Path, _log: sim_sounds.SoundLog) -> None:
    for _ in range(sim_sounds.FILES_KEPT + 1):
        _play("wake_up.wav")
    snapshot = _log.snapshot()
    assert snapshot["seq"] == sim_sounds.FILES_KEPT + 1
    assert len(snapshot["events"]) == sim_sounds.EVENTS_KEPT
    assert snapshot["events"][-1]["seq"] == snapshot["seq"]
    # The oldest file fell out; the newest is still served.
    assert len(_log._files) == sim_sounds.FILES_KEPT


@pytest.mark.asyncio
async def test_routes_list_events_and_serve_only_recorded_files(
    monkeypatch: pytest.MonkeyPatch, assets: Path, _log: sim_sounds.SoundLog
) -> None:
    app = _app(monkeypatch)
    _play("wake_up.wav")
    async with _client(app) as client:
        listing = (await client.get("/api/bridge/sounds")).json()
        token = listing["events"][0]["token"]
        served = await client.get(f"/api/bridge/sounds/{token}")
        unknown = await client.get("/api/bridge/sounds/" + "0" * 32)
        traversal = await client.get("/api/bridge/sounds/..%2F..%2Fetc%2Fpasswd")
    assert listing["seq"] == 1
    assert listing["boot"] != sim_sounds.SoundLog().snapshot()["boot"]
    assert served.status_code == 200 and served.content == b"RIFF....WAVE"
    assert served.headers["content-type"] == "audio/wav"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert unknown.status_code == 404
    assert traversal.status_code == 404


@pytest.mark.asyncio
async def test_a_file_swapped_for_a_link_out_after_playing_is_not_served(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, assets: Path, _log: sim_sounds.SoundLog
) -> None:
    app = _app(monkeypatch)
    _play("wake_up.wav")
    token = _log.snapshot()["events"][0]["token"]
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    sound = assets / "wake_up.wav"
    sound.unlink()
    sound.symlink_to(secret)
    async with _client(app) as client:
        response = await client.get(f"/api/bridge/sounds/{token}")
    assert response.status_code == 404
