"""P2.7: exercise prefixes through the real UserPromptSubmit subprocess."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "src/llm_router/hooks/auto-route.py"
REQUEST = "Write a Python function that returns the nth Fibonacci number."
MODEL = "qwen2.5:7b"
NATIVE = ("claude", "opus", "sonnet", "haiku")


class Ollama(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def send_json(self, value):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send_json({"models": [{"name": MODEL}]})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_json({
            "model": MODEL, "done": True, "response": "Stub answer.",
            "message": {"role": "assistant", "content": "Stub answer."},
            "prompt_eval_count": 10, "eval_count": 5,
        })


@pytest.fixture
def ollama():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Ollama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def run(home, url, prompt, **overrides):
    home.mkdir(parents=True, exist_ok=True)
    state = home / ".llm-router"
    state.mkdir(exist_ok=True)
    # Supply a discoverable Codex executable without depending on host installs.
    codex = home / "codex"
    codex.write_text("#!/bin/sh\nprintf '%s\\n' 'Stub answer.'\n")
    codex.chmod(0o700)
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TMPDIR")
           if k in os.environ}
    env.update({
        "HOME": str(home), "LLM_ROUTER_HOME": str(state),
        "LLM_ROUTER_DB_PATH": str(state / "test.db"),
        "LLM_ROUTER_OLLAMA_URL": url, "LLM_ROUTER_OLLAMA_MODEL": MODEL,
        "LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "1",
        "LLM_ROUTER_MINI_SUMMARY_EVERY": "0",
        "LLM_ROUTER_ENFORCE": "hard",
        "CODEX_PATH": str(codex),
        "OPENAI_API_KEY": "", "GEMINI_API_KEY": "", "GOOGLE_API_KEY": "",
    })
    env.update(overrides)
    env = {key: value for key, value in env.items() if value is not None}
    result = subprocess.run(
        [sys.executable, str(HOOK)], cwd=home, env=env,
        input=json.dumps({"hook_event_name": "UserPromptSubmit",
                          "prompt": prompt, "session_id": "p27"}),
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


def examples(home):
    path = home / ".llm-router/examples.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []


def pending(home):
    return list((home / ".llm-router").rglob("pending_route_*.json"))


@pytest.mark.parametrize("zero", [False, True])
@pytest.mark.parametrize("prefix", (*NATIVE, "local", "codex"))
def test_prefixes(tmp_path, ollama, prefix, zero):
    out = run(tmp_path, ollama, f"{prefix}: {REQUEST}",
              LLM_ROUTER_ZERO_CLAUDE=str(int(zero)))
    rows = examples(tmp_path)
    assert len(rows) == 1
    assert rows[0]["forced"] == prefix
    assert rows[0]["decided"] == ("claude" if prefix in NATIVE else prefix)
    assert rows[0]["door"] == "hook"
    assert set(rows[0]) == {"forced", "decided", "door", "text_sha", "ts"}
    assert REQUEST not in json.dumps(rows)
    if prefix in NATIVE:
        assert out is None
        assert not pending(tmp_path)
        logged = [json.loads(line) for line in
                  (tmp_path / ".llm-router/model_tracking.jsonl").read_text().splitlines()]
        row = [r for r in logged if r["classification_method"] == "prefix"][0]
        assert (row["selected_model"], row["provider"]) == (prefix, "claude")
    elif zero:
        assert out["decision"] == "block"
        assert "USER PREFIX" not in out.get("reason", "")
        assert "Stub answer." in json.dumps(out) or "zero" in json.dumps(out).lower()
    else:
        text = json.dumps(out, ensure_ascii=False)
        assert f"USER PREFIX `{prefix}:`" in text
        assert ("a local model" if prefix == "local" else "Codex") in text
        assert pending(tmp_path)
        saved = json.loads(pending(tmp_path)[0].read_text())
        assert saved["original_prompt"] == REQUEST
        assert f"{prefix}: " not in saved["original_prompt"]


def test_local_refusal(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    out = run(tmp_path, f"http://127.0.0.1:{port}", f"local: {REQUEST}",
              LLM_ROUTER_OLLAMA_MODEL=None, OLLAMA_HOST=f"http://127.0.0.1:{port}")
    assert out == {"decision": "block", "reason":
                   "[llm_router] `local:` refused: no healthy local model "
                   "(Ollama unreachable or no model installed); not sent to Claude."}
    assert examples(tmp_path)[0]["decided"] == "refused:no_local"


def test_unknown_prefix(tmp_path, ollama):
    # An unknown prefix is ordinary text: nothing stripped, nothing recorded.
    run(tmp_path, ollama, f"gpt: {REQUEST}")
    assert not examples(tmp_path)
    saved = json.loads(pending(tmp_path)[0].read_text())
    assert saved["original_prompt"] == f"gpt: {REQUEST}"


@pytest.mark.parametrize("prefix,mode,held", [
    ("local", "off", False), ("local", "hard", True), ("claude", "hard", False),
])
def test_enforcement(tmp_path, ollama, prefix, mode, held):
    run(tmp_path, ollama, f"{prefix}: what is the capital of France",
        LLM_ROUTER_ENFORCE=mode)
    assert bool(pending(tmp_path)) == held


COMPLEX = ("Architect a multi-region, fault tolerant, distributed database replication system "
           "from scratch with consensus, sharding, and formal verification, and analyze the "
           "tradeoffs in depth.")


def test_critical_pressure(tmp_path, ollama):
    state = tmp_path / ".llm-router"
    state.mkdir()
    (state / "usage.json").write_text(json.dumps({
        "session_pct": 99, "weekly_pct": 10, "sonnet_pct": 10,
        "updated_at": time.time(),
    }))
    sub = {"LLM_ROUTER_CLAUDE_SUBSCRIPTION": "true"}
    # Control: without a prefix the soft pressure override fires, so the check is not vacuous.
    control = run(tmp_path, ollama, COMPLEX, **sub)
    assert "SUBSCRIPTION OVERRIDE" in json.dumps(control)
    out = run(tmp_path, ollama, f"local: {COMPLEX}", **sub)
    assert "SUBSCRIPTION OVERRIDE" not in json.dumps(out)
    assert "USER PREFIX" in json.dumps(out)


def test_corrupt_examples_fails_open(tmp_path, ollama):
    (tmp_path / ".llm-router/examples.jsonl").mkdir(parents=True)
    run(tmp_path, ollama, f"local: {REQUEST}")
