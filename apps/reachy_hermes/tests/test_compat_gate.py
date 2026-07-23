from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from reachy_hermes.compat_gate import (
    _delivery_mode,
    assemble_result,
    make_check,
    parse_build_identity,
    scan_for_probe_payload,
)


VERSION_FIXTURE = """Hermes Agent v0.19.0 (2026.7.20) · upstream e1367b44 · local 15dc65ee (+10221 carried commits)
Install directory: /Users/example/.hermes/hermes-agent
Install method: git
Python: 3.11.15
OpenAI SDK: 2.24.0
"""


def _load_probe_plugin() -> ModuleType:
    path = Path(__file__).parents[1] / ".hermes/plugins/reachy_probe/__init__.py"
    spec = importlib.util.spec_from_file_location("reachy_probe_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_version_parser_accepts_real_local_build_suffix() -> None:
    assert parse_build_identity(VERSION_FIXTURE) == {
        "version": "0.19.0",
        "build_date": "2026.7.20",
        "upstream_sha": "e1367b44",
        "local_sha": "15dc65ee",
        "install_dir": "/Users/example/.hermes/hermes-agent",
    }


def test_version_parser_accepts_plain_upstream_build() -> None:
    fixture = VERSION_FIXTURE.replace(" · local 15dc65ee (+10221 carried commits)", "")
    assert "local_sha" not in parse_build_identity(fixture)


def test_json_result_assembly_ignores_skips_for_go() -> None:
    checks = [
        make_check("static", "pass", "ok"),
        make_check("live", "skip", "not run without --live"),
    ]
    result = assemble_result({"version": "0.19.0"}, checks)
    assert result["go"] is True
    assert json.loads(json.dumps(result)) == result
    assert assemble_result({}, [make_check("x", "fail", "bad")])["go"] is False


def test_delivery_mode_records_native_and_text_downgrade_evidence() -> None:
    assert _delivery_mode("The image is blue and says REACHY.") == "native"
    assert (
        _delivery_mode(
            "The tool returned only a text confirmation and no actual image."
        )
        == "text-downgraded"
    )
    assert _delivery_mode("Snapshot captured.") == "unverified"


def test_raw_payload_scanner_finds_jpeg_and_any_long_base64_window(
    tmp_path: Path,
) -> None:
    image_base64 = "A" * 64 + "B" * 64
    (tmp_path / "state.db").write_bytes(b"prefix\xff\xd8\xffsuffix")
    hits = scan_for_probe_payload(
        [tmp_path],
        [("stderr", b"noise" + image_base64[17:81].encode("ascii"))],
        image_base64,
    )
    assert any("JPEG magic bytes" in hit for hit in hits)
    assert any("64-character probe image base64 substring" in hit for hit in hits)


def test_raw_payload_scanner_negative(tmp_path: Path) -> None:
    (tmp_path / "safe.log").write_text("ordinary diagnostic output", encoding="utf-8")
    assert scan_for_probe_payload([tmp_path], [("stdout", b"safe")], "A" * 128) == []


def test_probe_plugin_registers_expected_multimodal_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = _load_probe_plugin()
    nonce = tmp_path / "nonce"
    audit = tmp_path / "audit"
    nonce.write_text("once", encoding="utf-8")
    monkeypatch.setenv("REACHY_PROBE_NONCE_FILE", str(nonce))
    monkeypatch.setenv("REACHY_PROBE_AUDIT_FILE", str(audit))

    context = MagicMock()
    plugin.register(context)
    registration = context.register_tool.call_args.kwargs
    assert registration["name"] == "reachy_probe_snapshot"
    assert registration["toolset"] == "reachy_probe"

    envelope = registration["handler"]({"dispatcher": "argument"})
    decoded = base64.b64decode(plugin.IMAGE_BASE64, validate=True)
    assert decoded.startswith(b"\xff\xd8\xff")
    assert envelope["_multimodal"] is True
    assert envelope["content"] == [
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{plugin.IMAGE_BASE64}",
            },
        }
    ]
    assert envelope["text_summary"]
    assert not nonce.exists()
    assert audit.read_text(encoding="utf-8") == "image\n"


def test_probe_plugin_one_shot_token_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = _load_probe_plugin()
    nonce = tmp_path / "nonce"
    audit = tmp_path / "audit"
    nonce.write_text("once", encoding="utf-8")
    monkeypatch.setenv("REACHY_PROBE_NONCE_FILE", str(nonce))
    monkeypatch.setenv("REACHY_PROBE_AUDIT_FILE", str(audit))

    assert isinstance(plugin.reachy_probe_snapshot(), dict)
    assert plugin.reachy_probe_snapshot() == plugin.FAIL_CLOSED
    assert audit.read_text(encoding="utf-8").splitlines() == ["image", "denied"]
