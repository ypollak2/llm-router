"""A real HTTP server that speaks the two Ollama endpoints the routing verdict uses (shared by the W10 tests).

``GET /api/ps`` lists the resident model; ``POST /v1/systemone`` answers the pinned ``tier`` question with ``probs``
after a one-off delay (the model "loading"). It runs on 127.0.0.1 for the test, so no test reaches the owner's Ollama.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOllama:
    """A local server with Ollama's two relevant endpoints. ``probs`` is what /v1/systemone returns."""

    def __init__(self) -> None:
        self.probs = {"haiku": 0.1, "sonnet": 0.7, "opus": 0.2}
        self.first_call_delay = 0.0     # the model "loads" on the first call
        self.fail = False               # answer 500
        self.calls = 0
        self.resident = False
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401, ANN002
                return

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                if self.path == "/api/ps":
                    self._send(200, {"models": [{"name": "nimble:9b"}] if outer.resident else []})
                else:
                    self._send(404, {})

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(n)
                with outer.lock:
                    outer.calls += 1
                    first = not outer.resident
                    outer.resident = True
                if first and outer.first_call_delay:
                    time.sleep(outer.first_call_delay)
                if outer.fail:
                    self._send(500, {"error": "boom"})
                    return
                top = max(outer.probs, key=outer.probs.get)
                self._send(200, {"model": "nimble:9b", "answers": {"tier": {
                    "type": "choice", "choice": top, "probabilities": outer.probs, "confidence": 0.3}}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
