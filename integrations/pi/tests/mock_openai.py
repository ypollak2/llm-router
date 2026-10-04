#!/usr/bin/env python3
"""A scripted OpenAI-compatible chat server for testing the Pi profile without a model.

Each POST /v1/chat/completions takes the next step from a JSON script and streams it
back as Server-Sent Events, the way Ollama's /v1 endpoint does. Every request body is
appended to a JSONL log so a test can see exactly what Pi sent.

Script format (a JSON list; one item per model call):
    {"text": "final answer"}
    {"tool_calls": [{"name": "bash", "arguments": {"command": "ls"}}]}
    {"prompt_tokens": 32767, "text": "..."}   # override the reported usage
    {"echo_last_user": true}                  # answer with the last user text
When the script runs out, the server answers "done".

Usage:
    python3 mock_openai.py --port 0 --script script.json --log requests.jsonl
It prints "PORT <n>" on stdout once it is listening.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _State:
    def __init__(self, script: list, log_path: str | None) -> None:
        self.script = list(script)
        self.log_path = log_path
        self.lock = threading.Lock()
        self.calls = 0

    def next_step(self) -> dict:
        with self.lock:
            self.calls += 1
            return self.script.pop(0) if self.script else {"text": "done"}

    def log(self, row: dict) -> None:
        if not self.log_path:
            return
        with self.lock, open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")


def _last_user_text(messages: list) -> str:
    for m in reversed(messages or []):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        return "".join(b.get("text", "") for b in c or [] if isinstance(b, dict))
    return ""


def make_handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):  # quiet
            return

        def do_GET(self):  # noqa: N802
            body = json.dumps({"object": "list", "data": [{"id": "mock", "object": "model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            try:
                req = json.loads(raw)
            except ValueError:
                req = {}
            step = state.next_step()
            state.log({"n": state.calls, "bytes": len(raw), "request": req, "step": step})
            if step.get("http_status"):
                body = json.dumps({"error": {"message": step.get("error", "mock error")}}).encode()
                self.send_response(int(step["http_status"]))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if step.get("sleep_s"):
                import time

                time.sleep(float(step["sleep_s"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            def emit(obj: dict) -> None:
                self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
                self.wfile.flush()

            base = {"id": f"chatcmpl-{state.calls}", "object": "chat.completion.chunk",
                    "created": 0, "model": req.get("model", "mock")}
            text = step.get("text")
            if step.get("echo_last_user"):
                text = _last_user_text(req.get("messages"))
            if text is not None:
                emit(base | {"choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                                          "finish_reason": None}]})
                finish = "stop"
            else:
                calls = step.get("tool_calls") or []
                for i, c in enumerate(calls):
                    emit(base | {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{
                        "index": i, "id": f"call_{state.calls}_{i}", "type": "function",
                        "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments", {}))}}]},
                        "finish_reason": None}]})
                finish = "tool_calls"
            prompt_tokens = int(step.get("prompt_tokens") or max(1, len(raw) // 4))
            emit(base | {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                         "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 5,
                                   "total_tokens": prompt_tokens + 5}})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--script", required=True)
    ap.add_argument("--log")
    a = ap.parse_args()
    with open(a.script, encoding="utf-8") as fh:
        script = json.load(fh)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(_State(script, a.log)))
    print(f"PORT {srv.server_address[1]}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
