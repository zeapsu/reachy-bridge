"""tools/sim/differential.py and record.py: recording the daemon's answers and
comparing two runs against the approved changes (#317)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tools" / "sim"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SIM / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module  # record.py imports differential by name
    spec.loader.exec_module(module)
    return module


differential = _load("differential")
record = _load("record")

STATUS = b'{"state": "running", "backend_status": {"motor_control_mode": "enabled"}, "error": null}'


def _recording(*answers: tuple[str, str, int, bytes]) -> dict:
    recorder = differential.Recorder()
    for answer in answers:
        recorder.add(*answer)
    return recorder.dump()


def test_ids_in_paths_and_keys_fold_and_queries_sort() -> None:
    uuid = "2f1c7a5e-1b2c-4d3e-8f90-0123456789ab"
    assert differential.route("get", f"/api/jobs/{uuid}?b=2&a=1") == "GET /api/jobs/{id}?a=1&b=2"
    assert differential.route("POST", "/api/move/stop/42") == "POST /api/move/stop/{id}"
    assert differential.route("POST", "/api/apps/start-app/sim_ui_app") == "POST /api/apps/start-app/sim_ui_app"
    fields = differential.shape(json.dumps({uuid: {"port": 8042}, "name": "x"}).encode())
    assert fields == {"$": {"object"}, "$.{id}": {"object"}, "$.{id}.port": {"number"}, "$.name": {"str"}}


def test_values_never_differ_only_shapes() -> None:
    a = _recording(("GET", "/api/daemon/status", 200, STATUS))
    b = _recording(("GET", "/api/daemon/status", 200, STATUS.replace(b"running", b"stopped")))
    assert differential.differences(a, b) == []


def test_a_run_merges_every_answer_of_a_route() -> None:
    stopped = b'{"state": "stopped", "backend_status": null, "error": null}'
    recording = _recording(("GET", "/api/daemon/status", 200, stopped), ("GET", "/api/daemon/status", 200, STATUS))
    fields = recording["GET /api/daemon/status"]["fields"]
    assert fields["$.backend_status"] == ["null", "object"]
    assert fields["$.backend_status.motor_control_mode"] == ["str"]


def test_empty_and_non_json_bodies_have_shapes_of_their_own() -> None:
    assert differential.shape(b"") == {"$": {"empty"}}
    assert differential.shape(b"Internal Server Error") == {"$": {"text"}}
    assert differential.shape(b"null") == {"$": {"null"}}
    assert differential.shape(b"[1, 2.5, true]") == {"$": {"array"}, "$[]": {"number", "bool"}}


def test_every_kind_of_difference_is_reported() -> None:
    base = _recording(
        ("GET", "/api/daemon/status", 200, STATUS),
        ("POST", "/api/daemon/start?wake_up=true", 200, b'{"job_id": "x"}'),
        ("GET", "/api/move/running", 200, b"[]"),
    )
    candidate = _recording(
        ("GET", "/api/daemon/status", 200, b'{"state": 1, "backend_status": {}, "error": null, "new": true}'),
        ("GET", "/api/daemon/status", 503, b'{"detail": "busy"}'),
        ("POST", "/api/daemon/start?wake_up=true", 200, b'{"job_id": "x"}'),
        ("GET", "/api/move/active", 200, b"[]"),
    )
    assert differential.differences(base, candidate) == [
        "GET /api/daemon/status: now answers 503",
        "GET /api/daemon/status: $.backend_status.motor_control_mode removed",
        "GET /api/daemon/status: $.detail added (str)",
        "GET /api/daemon/status: $.new added (bool)",
        "GET /api/daemon/status: $.state str -> number",
        "GET /api/move/active: newly called",
        "GET /api/move/running: no longer called",
    ]


def test_approved_differences_pass_and_others_fail(tmp_path: Path) -> None:
    base = _recording(("GET", "/api/daemon/status", 200, STATUS))
    candidate = _recording(("GET", "/api/daemon/status", 200, STATUS[:-1] + b', "extra": 1}'))
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps([{"difference": "GET /api/x: no longer called", "reason": "an old one"}]))
    unapproved, approved, unused = differential.compare(base, candidate, differential.load_allowed(allow))
    assert unapproved == ["GET /api/daemon/status: $.extra added (number)"]
    assert approved == []
    assert unused == ["GET /api/x: no longer called"]

    allow.write_text(json.dumps([{"difference": unapproved[0], "reason": "1.15 reports extra (#999)"}]))
    unapproved, approved, unused = differential.compare(base, candidate, differential.load_allowed(allow))
    assert (unapproved, approved, unused) == ([], ["GET /api/daemon/status: $.extra added (number)"], [])


def test_an_approval_needs_a_reason(tmp_path: Path) -> None:
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps([{"difference": "GET /api/x: no longer called"}]))
    with pytest.raises(ValueError, match="reason"):
        differential.load_allowed(allow)


def test_the_checked_in_approvals_load() -> None:
    differential.load_allowed(differential.ALLOW)


def test_compare_runs_isolated_and_exits_on_unapproved_differences(tmp_path: Path) -> None:
    base, candidate, allow = tmp_path / "base.json", tmp_path / "candidate.json", tmp_path / "allow.json"
    base.write_text(json.dumps(_recording(("GET", "/api/daemon/status", 200, STATUS))))
    candidate.write_text(json.dumps(_recording(("GET", "/api/daemon/status", 500, b"oops"))))
    allow.write_text("[]")
    command = [sys.executable, "-I", str(SIM / "differential.py"), "compare", str(base), str(candidate)]
    result = subprocess.run([*command, "--allow", str(allow)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "- GET /api/daemon/status: now answers 500" in result.stdout
    result = subprocess.run([*command[:-1], str(base), "--allow", str(allow)], capture_output=True, text=True)
    assert result.returncode == 0
    assert "No differences" in result.stdout


class _Daemon(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        sent = self.rfile.read(length) if length else b""
        body = json.dumps({"path": self.path, "method": self.command, "sent": sent.decode()}).encode()
        self.send_response(409 if self.path.startswith("/busy") else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


def test_the_proxy_forwards_and_records_what_the_daemon_answers(tmp_path: Path) -> None:
    daemon = ThreadingHTTPServer(("127.0.0.1", 0), _Daemon)
    threading.Thread(target=daemon.serve_forever, daemon=True).start()
    proxy = record.RecordingProxy(f"http://127.0.0.1:{daemon.server_address[1]}")
    proxy.start()
    try:
        with httpx.Client(base_url=proxy.url) as client:
            answer = client.post("/api/daemon/start?wake_up=true", content=b"hi")
            assert answer.status_code == 200
            assert answer.json() == {"path": "/api/daemon/start?wake_up=true", "method": "POST", "sent": "hi"}
            assert client.get("/busy").status_code == 409
        proxy.save(tmp_path / "rec.json")
    finally:
        proxy.close()
        daemon.shutdown()
        daemon.server_close()
    recording = json.loads((tmp_path / "rec.json").read_text())
    assert recording["POST /api/daemon/start?wake_up=true"]["status"] == [200]
    assert recording["POST /api/daemon/start?wake_up=true"]["fields"]["$.sent"] == ["str"]
    assert recording["GET /busy"]["status"] == [409]


def test_the_locked_version_is_read_from_the_lock() -> None:
    import tomllib

    locked = next(
        p["version"] for p in tomllib.loads((ROOT / "uv.lock").read_text())["package"] if p["name"] == "reachy-mini"
    )
    assert differential.lock_version(ROOT / "uv.lock") == locked


def test_list_elements_skip_only_when_both_runs_saw_the_list_empty() -> None:
    moving = _recording(("GET", "/api/move/running", 200, b'[{"uuid": "a"}]'), ("GET", "/api/move/running", 200, b"[]"))
    idle = _recording(("GET", "/api/move/running", 200, b"[]"))
    assert differential.differences(moving, idle) == []
    assert differential.differences(idle, moving) == []
    renamed = _recording(("GET", "/api/move/running", 200, b'[{"id": "a"}]'), ("GET", "/api/move/running", 200, b"[]"))
    assert differential.differences(moving, renamed) == [
        "GET /api/move/running: $[].id added (str)",
        "GET /api/move/running: $[].uuid removed",
    ]


def test_a_list_that_comes_back_empty_is_a_difference() -> None:
    path = "/api/move/recorded-move-datasets/list/pollen-robotics/reachy-mini-emotions-library"
    full = _recording(("GET", path, 200, b'["cheerful1", "sad1"]'))
    empty = _recording(("GET", path, 200, b"[]"))
    assert differential.differences(full, empty) == [f"GET {path}: $[] str -> none"]
    assert differential.differences(empty, full) == [f"GET {path}: $[] none -> str"]


def test_control_characters_in_routes_are_escaped() -> None:
    base = _recording(("GET", "/api/a", 200, b"{}"))
    candidate = _recording(("GET", "/api/a", 200, b'{"x\\n::error::boom": 1}'))
    [line] = differential.differences(base, candidate)
    assert "\n" not in line and "\\n::error::boom" in line
