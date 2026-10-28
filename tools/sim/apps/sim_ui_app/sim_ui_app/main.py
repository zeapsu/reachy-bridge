"""Sim test app with a web UI whose server starts late.

Real apps bring their web server up 1-3 s after the process starts
(``docs/daemon-contract.md``, "Apps"), so the controller has to wait for the
UI to answer before sending the user there. This app holds its server back
for ``SIM_UI_DELAY`` seconds (default 3) to make that window easy to see.
The daemon reads the UI address from ``custom_app_url`` below without
importing this module. Like real apps, it asks for every interface; the
bridge runtime's app_bind shim keeps it on loopback (#186).
"""

import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

custom_app_url = "http://0.0.0.0:18742"
PORT = 18742


class App:
    """Entry-point target; the daemon runs this module, not the class."""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"<!doctype html><title>sim_ui_app</title><p>sim_ui_app is up</p>\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    delay = float(os.getenv("SIM_UI_DELAY", "3"))
    print(f"sim_ui_app: UI server starts in {delay:g} s", flush=True)
    time.sleep(delay)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"sim_ui_app: serving on {custom_app_url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass  # the daemon stops apps with SIGINT
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
