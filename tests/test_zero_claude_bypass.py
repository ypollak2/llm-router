"""Audit §2.2/§2.3: zero-Claude must actually bypass Claude on SUCCESS.

Findings:
  §2.3 — With LLM_ROUTER_ZERO_CLAUDE=1 and a healthy provider the hook returned
         ``decision: approve`` (echo mode), so Claude still processed the turn.
         "Strict" was inverted: airtight only when routing *failed*.
  T10  — A whitespace-only prompt exited early → native Claude turn, bypassing
         zero-Claude entirely.

Contract now:
  * zero-Claude + direct SUCCESS  → ``decision: block``  (Claude bypassed)
  * zero-Claude + direct FAILURE  → ``decision: block``  (fail-closed, unchanged)
  * zero-Claude + empty prompt    → ``decision: block``  (no native leak)
  * default (echo) mode           → ``decision: approve`` (unchanged advisory)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"

# The one model the stub advertises AND the chain is told to use, so the §2.4
# availability gate lets the call through.
STUB_MODEL = "qwen2.5:7b"


class _StubOllama(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            self._send({"models": [{"name": STUB_MODEL}, {"name": "llama3.2:3b"}]})
        else:
            self._send({"ok": True})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n) or b"{}")
        if self.path.startswith("/api/chat"):
            self._send({"model": payload.get("model"), "done": True,
                        "prompt_eval_count": 10, "eval_count": 5,
                        "message": {"role": "assistant",
                                    "content": "Paris is the capital of France."}})
        else:
            self._send({"model": payload.get("model"),
                        "response": "Paris is the capital of France.", "done": True,
                        "prompt_eval_count": 10, "eval_count": 5})


@pytest.fixture
def stub_ollama():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _run(prompt: str, home: Path, ollama_url: str, extra_env=None) -> dict | None:
    (home / ".llm-router").mkdir(parents=True, exist_ok=True)
    # Hermeticity (INV-TEST-000 / B0-1): build a MINIMAL clean env instead of
    # inheriting the parent process's os.environ. The hook reads several ambient
    # (non-LLM_ROUTER_) vars directly — e.g. OLLAMA_BASE_URL/OLLAMA_URL via
    # effective_ollama_base_url — so copying os.environ let any var set by an
    # earlier test bleed into this subprocess, making the test order-dependent
    # (passed alone, failed in the full suite). Only PATH + explicit knobs are kept.
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TMPDIR")
           if k in os.environ}
    env["HOME"] = str(home)
    env["LLM_ROUTER_OLLAMA_URL"] = ollama_url
    env["LLM_ROUTER_OLLAMA_MODEL"] = STUB_MODEL   # matches /api/tags → passes §2.4 gate
    env["LLM_ROUTER_DISABLE_LLM_CLASSIFIERS"] = "1"
    env["OPENAI_API_KEY"] = ""
    env["GEMINI_API_KEY"] = ""
    env["GOOGLE_API_KEY"] = ""
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"hook_event_name": "UserPromptSubmit",
                          "prompt": prompt, "session_id": "zc"}),
        capture_output=True, text=True, env=env,
        # Hermeticity (INV-TEST-000 / B0-1): pin cwd to the isolated HOME so the
        # hook's `.llm_router.yml` discovery (repo_config/enforce_config walk UP from
        # cwd) cannot read a config left by, or a cwd leaked from, another test.
        # Without this, these subprocess tests were order-dependent: they passed
        # alone but failed in the full suite.
        cwd=str(home),
    )
    out = result.stdout.strip()
    return json.loads(out) if out else None


def test_zero_claude_blocks_on_success(tmp_path, stub_ollama):
    """zero-Claude + healthy provider must BLOCK Claude, not approve."""
    out = _run("What is the capital of France?", tmp_path, stub_ollama,
               extra_env={"LLM_ROUTER_ZERO_CLAUDE": "1"})
    assert out is not None, "hook produced no output"
    assert out.get("decision") == "block", (
        f"zero-Claude leaked on success — expected block, got {out.get('decision')!r}"
    )
    # The routed answer must be surfaced to the user via the block reason.
    assert "Paris" in json.dumps(out)


def test_default_mode_echoes_self_contained_outside_zero_claude(tmp_path, stub_ollama):
    """CHZ-DRAFT-01 / RED2-01: default RENDER_MODE=auto must NOT block-replace the
    user's turn outside zero-Claude — a stateless local-model draft must never
    masquerade as the answer (the block path is reserved for explicit zero-Claude,
    see test_zero_claude_* below). The draft is instead delivered as advisory
    context (decision=approve) that the assistant verifies. This replaces the old
    assertion that default mode BLOCKS self-contained drafts, which was exactly
    the turn-replacement fabrication risk the audit identified: _is_context_dependent
    is a fixed noun list with a ~60% false-negative rate, so "self-contained" could
    not be trusted to gate a turn-replacing block."""
    out = _run("What is the capital of France?", tmp_path, stub_ollama)
    assert out is not None
    assert out.get("decision") == "approve", (
        f"auto default must stay advisory outside zero-Claude, got {out.get('decision')!r}"
    )
    # The advisory draft still reaches the assistant (via additionalContext).
    assert "Paris" in json.dumps(out)


def test_explicit_echo_mode_stays_advisory(tmp_path, stub_ollama):
    """Opting into RENDER_MODE=echo keeps the old advisory behavior."""
    out = _run(
        "What is the capital of France?", tmp_path, stub_ollama,
        extra_env={"LLM_ROUTER_RENDER_MODE": "echo"},
    )
    assert out is not None
    assert out.get("decision") == "approve", (
        f"explicit echo mode should stay advisory (approve), got {out.get('decision')!r}"
    )


def test_zero_claude_blocks_empty_prompt(tmp_path, stub_ollama):
    """T10: a whitespace-only prompt in zero-Claude must not leak to Claude."""
    out = _run("   ", tmp_path, stub_ollama, extra_env={"LLM_ROUTER_ZERO_CLAUDE": "1"})
    assert out is not None, "whitespace prompt produced no output → native Claude turn"
    assert out.get("decision") == "block"


# ── §2.5: hook builds token-capped history from the CC transcript ─────────────

def _load_ar():
    """Import auto-route.py as a module (hyphen in filename blocks plain import)."""
    import importlib.util
    cached = sys.modules.get("auto_route_hist")
    if cached:
        return cached
    spec = importlib.util.spec_from_file_location("auto_route_hist", HOOK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_hist"] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_transcript(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries))


def test_history_parses_user_and_assistant_turns(tmp_path):
    ar = _load_ar()
    tp = tmp_path / "transcript.jsonl"
    _write_transcript(tp, [
        {"type": "user", "message": {"role": "user", "content": "call my project Zephyr"}},
        {"type": "assistant", "message": {"role": "assistant",
         "content": [{"type": "text", "text": "Noted — Zephyr."}]}},
        {"type": "user", "message": {"role": "user", "content": "what did I name it?"}},
    ])
    hist = ar._load_conversation_history(str(tp), "what did I name it?")
    # trailing user turn == current prompt is dropped; text blocks extracted
    assert hist == [
        {"role": "user", "content": "call my project Zephyr"},
        {"role": "assistant", "content": "Noted — Zephyr."},
    ]


def test_history_skips_tool_only_and_bad_lines(tmp_path):
    ar = _load_ar()
    tp = tmp_path / "t.jsonl"
    tp.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n"
        + "{ not json\n"
        + json.dumps({"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "tool_use", "name": "Bash"}]}}) + "\n"
    )
    hist = ar._load_conversation_history(str(tp), "unrelated")
    assert hist == [{"role": "user", "content": "hi"}]  # tool-only + bad line dropped


def test_history_token_capped(tmp_path):
    ar = _load_ar()
    tp = tmp_path / "t.jsonl"
    big = "x" * 20000  # exceeds default 16k char budget alone
    _write_transcript(tp, [
        {"type": "user", "message": {"role": "user", "content": big}},
        {"type": "assistant", "message": {"role": "assistant",
         "content": [{"type": "text", "text": "recent short answer"}]}},
    ])
    hist = ar._load_conversation_history(str(tp), "now")
    # most-recent turn always kept; the oversized older turn is trimmed out
    assert hist[-1]["content"] == "recent short answer"
    assert all(len(t["content"]) < 20000 for t in hist) or len(hist) == 1


def test_history_empty_when_no_transcript(tmp_path):
    ar = _load_ar()
    assert ar._load_conversation_history("", "q") == []
    assert ar._load_conversation_history(str(tmp_path / "nope.jsonl"), "q") == []
