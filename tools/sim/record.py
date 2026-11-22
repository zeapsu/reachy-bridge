"""A loopback proxy in front of the sim daemon that records what it answers.

``run.py --record FILE`` puts it between the daemon and both the controller
and ``smoke.py``, so the recording holds every daemon answer the checks and
the controller relied on during the run (``differential.py`` compares two).
Bodies are read whole, so it suits the smoke run, not streams.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from differential import Recorder

# Not forwarded: per-connection headers, and the body's length and encoding,
# which httpx has already undone.
_SKIP = {
    "connection",
    "keep-alive",
    "proxy-connection",
    "transfer-encoding",
    "upgrade",
    "te",
    "trailer",
    "host",
    "content-length",
    "content-encoding",
}


class RecordingProxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, upstream: str) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        # The controller sets its own timeouts; the proxy mustn't cut it short.
        self.client = httpx.Client(base_url=upstream, timeout=120)
        self.recorder = Recorder()
        self.lock = threading.Lock()
        self._thread = threading.Thread(target=self.serve_forever, name="record-proxy", daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self.shutdown()
        self.server_close()
        self.client.close()

    def save(self, path: Path) -> None:
        with self.lock:
            recording = self.recorder.dump()
        path.write_text(json.dumps(recording, indent=1, sort_keys=True) + "\n")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: RecordingProxy

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in _SKIP}
        try:
            answer = self.server.client.request(self.command, self.path, content=body, headers=headers)
        except httpx.HTTPError:
            # As if the daemon were down; not recorded.
            self.send_error(502)
            return
        with self.server.lock:
            self.server.recorder.add(self.command, self.path, answer.status_code, answer.content)
        self.send_response(answer.status_code)
        for key, value in answer.headers.multi_items():
            if key.lower() not in _SKIP:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(answer.content)))
        self.end_headers()
        self.wfile.write(answer.content)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _forward

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the base class's name
        pass
