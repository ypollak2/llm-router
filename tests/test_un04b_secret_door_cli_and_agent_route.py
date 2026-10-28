"""UN-04b: a secret-bearing prompt must not reach a cloud sink through ``llm_codex``, ``llm_gemini`` or the
agent-route direct executor. Same door and same user-visible behaviour as ``route_and_call`` / ``sdk._secret_door``:
refuse (no local model to fall back to), ``cloud:`` overrides, a non-secret prompt is untouched.

Sinks are traced in-process: ``asyncio.create_subprocess_exec`` for the CLIs, ``urllib.request.urlopen`` for the
direct executor. Nothing is executed or dialled. Secrets are generated at test time (tests/secret_fixtures.py)."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from secret_fixtures import real_shaped  # noqa: E402

from llm_router import local_privacy as lp  # noqa: E402

SECRET = real_shaped("github_token", 4)
SECRET_PROMPT = f"deploy with token {SECRET} please"
PLAIN_PROMPT = "explain what a mutex is in two sentences"
HOOK = Path(__file__).parent.parent / "hooks" / "agent-route.py"


def _trace_exec(monkeypatch) -> list[tuple]:
    seen: list[tuple] = []

    async def fake_exec(*args, **kw):
        seen.append(args)
        raise OSError("traced; not executed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return seen


def _carries(seen: list[tuple], text: str) -> bool:
    return any(text in str(a) for call in seen for a in call)


@pytest.fixture
def codex_tool(monkeypatch):
    from llm_router import codex_agent
    from llm_router.tools import codex
    monkeypatch.setattr(codex, "is_codex_available", lambda: True)
    monkeypatch.setattr(codex_agent, "is_codex_available", lambda: True)
    monkeypatch.setattr(codex_agent, "find_codex_binary", lambda: "/nonexistent/codex")
    return codex.llm_codex


@pytest.fixture
def gemini_tool(monkeypatch):
    from llm_router import gemini_cli_agent
    from llm_router.tools import gemini_cli
    monkeypatch.setattr(gemini_cli, "is_gemini_cli_available", lambda: True)
    monkeypatch.setattr(gemini_cli_agent, "is_gemini_cli_available", lambda: True)
    monkeypatch.setattr(gemini_cli_agent, "find_gemini_binary", lambda: "/nonexistent/gemini")
    return gemini_cli.llm_gemini


@pytest.mark.parametrize("fixture", ["codex_tool", "gemini_tool"])
def test_cli_tool_refuses_secret_prompt(fixture, request, monkeypatch):
    tool = request.getfixturevalue(fixture)
    seen = _trace_exec(monkeypatch)
    with pytest.raises(lp.SecretLocalOnlyRefused) as ei:
        asyncio.run(tool(SECRET_PROMPT))
    assert not _carries(seen, SECRET), "the secret reached the CLI subprocess"
    assert SECRET not in str(ei.value), "the refusal must name the category, never the value"
    assert "GitHub token" in str(ei.value)


@pytest.mark.parametrize("fixture", ["codex_tool", "gemini_tool"])
def test_cli_tool_cloud_prefix_overrides(fixture, request, monkeypatch):
    tool = request.getfixturevalue(fixture)
    seen = _trace_exec(monkeypatch)
    asyncio.run(tool("cloud: " + SECRET_PROMPT))
    assert _carries(seen, SECRET), "positive control: the override must reach the CLI"
    assert not _carries(seen, "cloud:"), "the override prefix is stripped before sending"


@pytest.mark.parametrize("fixture", ["codex_tool", "gemini_tool"])
def test_cli_tool_plain_prompt_unchanged(fixture, request, monkeypatch):
    tool = request.getfixturevalue(fixture)
    seen = _trace_exec(monkeypatch)
    asyncio.run(tool(PLAIN_PROMPT))
    assert _carries(seen, PLAIN_PROMPT), "no regression: a non-secret prompt still reaches the CLI"


# ── agent-route direct executor ─────────────────────────────────────────────

def _hook():
    spec = importlib.util.spec_from_file_location("agent_route_un04b", HOOK)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


@pytest.fixture
def cloud_chain(monkeypatch):
    """Real execute_chain against a gemini+openai chain; urlopen is the traced sink."""
    import llm_router.hooks.chain_builder as cb
    import llm_router.hooks.direct_executor as de

    sent: list[tuple[str, bytes]] = []

    def fake_urlopen(req, *a, **k):
        sent.append((req.full_url.split("?")[0], req.data or b""))
        raise OSError("traced; not dialled")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(cb, "build_chain", lambda *a, **k: [
        de.ModelSpec("gemini", "gemini-2.5-flash"), de.ModelSpec("openai", "gpt-4o-mini")])
    monkeypatch.setattr(cb, "get_current_pressure", lambda: ("green", 0.0))
    monkeypatch.setattr(cb, "needs_claude_tools", lambda *a, **k: False)
    monkeypatch.setenv("LLM_ROUTER_SUBAGENT_DIRECT", "on")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")
    return sent


def test_agent_route_secret_prompt_reaches_no_cloud_endpoint(cloud_chain):
    assert _hook()._try_direct_subagent(SECRET_PROMPT, "code", "simple", "s1") is None
    # No request at all: the pattern scrubber (RTE-004) may redact the token in the body, but the prompt
    # must not be sent to a cloud endpoint when it carries a secret.
    assert cloud_chain == [], [url for url, _ in cloud_chain]


def test_agent_route_cloud_prefix_overrides(cloud_chain):
    _hook()._try_direct_subagent("cloud: " + SECRET_PROMPT, "code", "simple", "s1")
    assert cloud_chain, "positive control: the override reaches the cloud sink"
    assert not any(b"cloud:" in body for _, body in cloud_chain)


def test_agent_route_plain_prompt_still_dispatches(cloud_chain):
    _hook()._try_direct_subagent(PLAIN_PROMPT, "code", "simple", "s1")
    assert cloud_chain and any(PLAIN_PROMPT.encode() in body for _, body in cloud_chain)
