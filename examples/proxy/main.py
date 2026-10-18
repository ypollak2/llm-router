"""Hooks / proxy door: an Anthropic client through ``llm-router proxy``.

    python examples/proxy/main.py

``llm-router proxy`` sits at ANTHROPIC_BASE_URL for one session (this is how Claude Code is pointed
at it; the hooks door installs beside it with ``llm-router install``). With no step class enabled it
forwards each call to Anthropic unchanged and writes one row to the proxy ledger. Here the "Anthropic"
is a loopback fake. One call through the proxy, one assertion on the answer, one on the proxy row.
"""
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import fake_backend as fb  # noqa: E402

home = fb.install()
upstream = fb.start_anthropic_upstream()
ledger = home / "proxy_calls.jsonl"
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
proxy = subprocess.Popen(
    [sys.executable, "-m", "llm_router.cli", "proxy", "--port", str(port), "--steps", "off",
     "--no-warm-up", "--ledger", str(ledger)],
    env=fb.child_env({"LLM_ROUTER_PROXY_UPSTREAM": upstream}))
try:
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            fb.expect(proxy.poll() is None and time.monotonic() < deadline, "the proxy did not start")
            time.sleep(0.2)
    request = urllib.request.Request(
        base + "/v1/messages", method="POST",
        data=json.dumps({"model": "claude-haiku-4-5", "max_tokens": 64,
                         "messages": [{"role": "user", "content": fb.PROMPT}]}).encode(),
        headers={"content-type": "application/json", "x-api-key": "x", "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(request, timeout=30) as response:
        reply = json.load(response)
    text = reply["content"][0]["text"]
    print(text)
    fb.expect(text == fb.FINAL, f"unexpected answer: {text!r}")
    deadline = time.monotonic() + 10
    while not (ledger.exists() and ledger.read_text().strip()) and time.monotonic() < deadline:
        time.sleep(0.1)
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    fb.expect(len(rows) == 1, f"expected one proxy row, got {rows}")
    fb.expect(rows[0]["decision"] == "forwarded" and rows[0]["requested_model"] == "claude-haiku-4-5",
              f"proxy row: {rows[0]}")
finally:
    proxy.terminate()
    proxy.wait(timeout=15)
fb.finish()
