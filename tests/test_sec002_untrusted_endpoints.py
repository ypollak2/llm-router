"""SEC-002/003: a cloned repository must not decide where credentials are sent.

Audit 2026-09-24 (audit/forensic_2026-09-24/13_verify_security_deps.md) reproduced
it end to end: a project `.env` containing

    OPENAI_COMPAT_BASE_URL=http://<attacker>/v1
    OPENAI_COMPAT_MODELS=gpt-4

made `call_llm("openai_compat/gpt-4", ...)` send the user's real OPENAI_API_KEY as
`Authorization: Bearer ...` to that host. Two defects composed:

  * the openai_compat quirk injected `api_base` and no `api_key`, so LiteLLM fell
    back to OPENAI_API_KEY for a server that is not OpenAI;
  * `RouterConfig` and the auto-route hook trusted the working directory's `.env`
    exactly like the user's own config, so repository content chose the endpoint.

Both are closed here, and each test below fails if either fix is reverted alone.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import threading

import pytest

CANARY = "sk-test-CANARY-never-a-real-key-0000"


class _Capture(http.server.BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_POST(self):  # noqa: N802 — http.server API
        _Capture.seen.append(self.headers.get("Authorization", ""))
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({
            "id": "x", "object": "chat.completion", "created": 0, "model": "gpt-4",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def listener():
    _Capture.seen = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


@pytest.fixture
def fresh_config(monkeypatch, tmp_path):
    """Isolated state dir, empty cwd, and no cached config."""
    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / "project"
    proj.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.chdir(proj)
    for k in ("OPENAI_COMPAT_BASE_URL", "OPENAI_COMPAT_MODELS", "OPENAI_COMPAT_API_KEY",
              "LLM_ROUTER_PXPIPE_URL", "GROQ_API_KEY", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    import llm_router.config as cfg
    monkeypatch.setattr(cfg, "_config", None, raising=False)
    yield home, proj, cfg
    monkeypatch.setattr(cfg, "_config", None, raising=False)


def test_openai_compat_endpoint_never_receives_the_real_openai_key(
        monkeypatch, fresh_config, listener):
    """The reproduction itself: an explicitly configured compat endpoint, a real
    OPENAI_API_KEY in the environment — the endpoint must not see that key."""
    _, _, cfg = fresh_config
    monkeypatch.setenv("OPENAI_API_KEY", CANARY)
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", listener)
    monkeypatch.setenv("OPENAI_COMPAT_MODELS", "gpt-4")
    from llm_router.providers import call_llm
    asyncio.run(call_llm("openai_compat/gpt-4",
                         [{"role": "user", "content": "hello"}]))
    assert _Capture.seen, "premise: the request never reached the listener"
    assert not any(CANARY in h for h in _Capture.seen), _Capture.seen


def test_an_explicit_compat_key_is_still_sent(monkeypatch, fresh_config, listener):
    """Servers that do need a key keep working — through their own setting."""
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", listener)
    monkeypatch.setenv("OPENAI_COMPAT_MODELS", "gpt-4")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "sk-compat-own-key")
    from llm_router.providers import call_llm
    asyncio.run(call_llm("openai_compat/gpt-4",
                         [{"role": "user", "content": "hello"}]))
    assert any("sk-compat-own-key" in h for h in _Capture.seen), _Capture.seen


def test_a_project_dotenv_cannot_choose_an_endpoint(fresh_config):
    _, proj, cfg = fresh_config
    (proj / ".env").write_text(
        "OPENAI_COMPAT_BASE_URL=http://evil.example/v1\n"
        "GROQ_API_KEY=gsk-project-key\n")
    c = cfg.RouterConfig()
    assert c.openai_compat_base_url == ""
    # API keys still load from a project .env: they only spend its author's money.
    assert c.groq_api_key == "gsk-project-key", "premise: the project .env was read"


def test_the_users_own_dotenv_can_still_choose_an_endpoint(fresh_config):
    home, _, cfg = fresh_config
    (home / ".env").write_text("OPENAI_COMPAT_BASE_URL=http://gpu-box.lan:8080/v1\n")
    assert cfg.RouterConfig().openai_compat_base_url == "http://gpu-box.lan:8080/v1"


def test_project_dotenv_still_overrides_user_dotenv_for_non_endpoint_keys(fresh_config):
    """Precedence for everything else is unchanged (cwd .env beats state .env)."""
    home, proj, cfg = fresh_config
    (home / ".env").write_text("GROQ_API_KEY=from-home\n")
    (proj / ".env").write_text("GROQ_API_KEY=from-project\n")
    assert cfg.RouterConfig().groq_api_key == "from-project"


@pytest.mark.parametrize("url,ok", [
    ("http://127.0.0.1:47821", True),
    ("http://localhost:47821", True),
    ("http://[::1]:47821", True),
    ("http://evil.example:47821", False),
    ("http://10.0.0.5:47821", False),
])
def test_pxpipe_must_be_local(monkeypatch, fresh_config, url, ok):
    """pxpipe forwards to the real provider WITH the real key, so its URL is the
    one place a non-local host would receive credentials by design."""
    _, _, cfg = fresh_config
    monkeypatch.setenv("LLM_ROUTER_PXPIPE_URL", url)
    got = cfg.RouterConfig().llm_router_pxpipe_url
    assert (got == url) if ok else (got == ""), got


@pytest.mark.parametrize("key,endpoint", [
    ("OPENAI_COMPAT_BASE_URL", True), ("OPENAI_BASE_URL", True),
    ("ANTHROPIC_API_BASE", True), ("OLLAMA_HOST", True), ("HTTPS_PROXY", True),
    ("LLM_ROUTER_ALERT_WEBHOOK", True),
    ("OPENAI_API_KEY", False), ("LLM_ROUTER_ENFORCE", False), ("GROQ_API_KEY", False),
])
def test_endpoint_key_rule(key, endpoint):
    from llm_router.config import is_endpoint_key
    assert is_endpoint_key(key) is endpoint


def test_the_hook_does_not_import_endpoints_from_a_project_dotenv(monkeypatch, tmp_path):
    """auto-route.py loads .env files into os.environ at import, where LiteLLM
    reads OPENAI_BASE_URL & co. directly — a second path to the same sink."""
    import importlib.util
    import os
    from pathlib import Path
    proj = tmp_path / "project"
    proj.mkdir()
    (proj / ".env").write_text(
        "OPENAI_BASE_URL=http://evil.example/v1\n"
        "OPENAI_COMPAT_BASE_URL=http://evil.example/v1\n"
        "PYTHONPATH=/tmp/evil-sitecustomize\n"
        "NODE_OPTIONS=--require /tmp/evil.js\n"
        "SEC002_PROBE_API_KEY=loaded\n")
    monkeypatch.chdir(proj)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    for k in ("OPENAI_BASE_URL", "OPENAI_COMPAT_BASE_URL", "SEC002_PROBE_API_KEY",
              "NODE_OPTIONS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PYTHONPATH", os.environ.get("PYTHONPATH", ""))  # restored after
    spec = importlib.util.spec_from_file_location(
        "auto_route_sec002",
        Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "auto-route.py")
    try:
        spec.loader.exec_module(importlib.util.module_from_spec(spec))
        assert os.environ.get("SEC002_PROBE_API_KEY") == "loaded", \
            "premise: the hook read the project .env at all"
        assert "OPENAI_BASE_URL" not in os.environ
        assert "OPENAI_COMPAT_BASE_URL" not in os.environ
        assert "NODE_OPTIONS" not in os.environ, "process-env injection from repo content"
    finally:
        for k in ("OPENAI_BASE_URL", "OPENAI_COMPAT_BASE_URL", "SEC002_PROBE_API_KEY",
                  "NODE_OPTIONS"):
            os.environ.pop(k, None)


@pytest.mark.parametrize("key,allowed", [
    ("OPENAI_API_KEY", True), ("GROQ_API_KEY", True), ("LLM_ROUTER_ENFORCE", True),
    ("LLM_ROUTER_OLLAMA_URL", False), ("PYTHONPATH", False), ("NODE_OPTIONS", False),
    ("DYLD_INSERT_LIBRARIES", False), ("SSL_CERT_FILE", False), ("OPENAI_BASE_URL", False),
])
def test_project_env_allowlist(key, allowed):
    from llm_router.config import project_env_may_set
    assert project_env_may_set(key) is allowed
