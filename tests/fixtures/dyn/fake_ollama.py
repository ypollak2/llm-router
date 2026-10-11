"""A loopback Ollama / OpenAI-compatible server that returns exactly what it is told."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeLocalServer:
    """``/api/tags``, ``/api/show``, ``/api/ps`` (Ollama) and ``/v1/models`` (OpenAI-compatible).

    ``models`` is the list of names served; change it with :meth:`set_models`
    between probes. ``requests`` records every path asked, in order.
    """

    def __init__(self, models: list[str] | None = None) -> None:
        self.models: list[str] = list(models or [])
        self.requests: list[str] = []
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D401 - silence the default stderr log
                return

            def _send(self, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                outer.requests.append(self.path)
                if self.path == "/api/tags":
                    self._send({"models": [{"name": m, "digest": f"sha256:{abs(hash(m)):x}"} for m in outer.models]})
                elif self.path == "/api/ps":
                    self._send({"models": []})
                elif self.path == "/v1/models":
                    self._send({"object": "list", "data": [{"id": m, "object": "model"} for m in outer.models]})
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):  # noqa: N802
                outer.requests.append(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                if self.path == "/api/show":
                    self._send({"capabilities": ["completion"]})
                else:
                    self.send_response(404)
                    self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def set_models(self, models: list[str]) -> None:
        self.models = list(models)

    def __enter__(self) -> "FakeLocalServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
