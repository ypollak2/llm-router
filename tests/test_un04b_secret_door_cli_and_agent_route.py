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
    out = asyncio.run(tool(SECRET_PROMPT))
    assert not _carries(seen, SECRET), "the secret reached the CLI subprocess"
    assert seen == [], "no CLI process may start"
    assert out.startswith("Refused:") and "GitHub token" in out
    assert SECRET not in out, "the refusal must name the category, never the value"


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


HOOK = Path(__file__).parent.parent / "hooks" / "agent-route.py"


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


# ── MCP layer renders the refusal as a tool error, not a traceback ──────────

@pytest.mark.parametrize("fixture", ["codex_tool", "gemini_tool"])
def test_mcp_renders_refusal_as_clean_refusal_text(fixture, request, monkeypatch):
    from mcp.server.mcpserver import MCPServer as FastMCP
    tool = request.getfixturevalue(fixture)
    seen = _trace_exec(monkeypatch)
    mcp = FastMCP("t")
    mcp.tool()(tool)
    res = asyncio.run(mcp.call_tool(tool.__name__, {"prompt": SECRET_PROMPT}))
    assert not getattr(res, "is_error", getattr(res, "isError", False)), "a policy refusal is a normal answer"
    msg = str(res)
    assert "Refused" in msg and "GitHub token" in msg
    assert "Traceback" not in msg and "Error executing tool" not in msg and SECRET not in msg
    assert not _carries(seen, SECRET)


# ── the real hook entry (main), default env: NS3 on, CLI delegation on ───────

def _run_main(monkeypatch, prompt, *, direct=False, routed_spawn=True, cli=True):
    """Drive hooks/agent-route.py main() in-process. Returns (cli argv calls, urlopen calls, stdout)."""
    import io
    import json

    import llm_router.hooks.chain_builder as cb
    import llm_router.hooks.direct_executor as de
    from llm_router import codex_agent, gemini_cli_agent

    argv_calls = _trace_exec(monkeypatch)
    urls: list[tuple[str, bytes]] = []

    def fake_urlopen(req, *a, **k):
        urls.append((req.full_url.split("?")[0], req.data or b""))
        raise OSError("traced; not dialled")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    for mod, avail, find, path in ((codex_agent, "is_codex_available", "find_codex_binary", "/nx/codex"),
                                   (gemini_cli_agent, "is_gemini_cli_available", "find_gemini_binary", "/nx/gemini")):
        monkeypatch.setattr(mod, avail, lambda: True)
        monkeypatch.setattr(mod, find, lambda path=path: path)
    monkeypatch.setattr(cb, "build_chain", lambda *a, **k: [
        de.ModelSpec("gemini", "gemini-2.5-flash"), de.ModelSpec("openai", "gpt-4o-mini")])
    monkeypatch.setattr(cb, "get_current_pressure", lambda: ("green", 0.0))
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")
    for k in ("LLM_ROUTER_AGENT_ROUTE_CODEX", "LLM_ROUTER_SUBAGENT_CLI_DELEGATION", "LLM_ROUTER_SUBAGENT_DIRECT"):
        monkeypatch.delenv(k, raising=False)  # defaults: NS3 on, CLI delegation on, DIRECT off
    if not cli:  # isolate the DIRECT sink: Codex (NS3) and CLI delegation off
        monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "off")
        monkeypatch.setenv("LLM_ROUTER_SUBAGENT_CLI_DELEGATION", "off")
    if direct:
        monkeypatch.setenv("LLM_ROUTER_SUBAGENT_DIRECT", "on")
    monkeypatch.setenv("LLM_ROUTER_ALLOW_SUBAGENTS", "on" if routed_spawn else "off")
    payload = {"tool_name": "Agent", "session_id": "un04b", "tool_use_id": "t1", "cwd": os.getcwd(),
               "tool_input": {"prompt": prompt, "subagent_type": "general-purpose"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    mod = _hook()
    try:
        mod.main()
    except SystemExit:
        pass
    return argv_calls, urls, out.getvalue()


RESEARCH = "research and explain how the retry logic works across the codebase, then summarise: "


@pytest.mark.parametrize("routed_spawn", [True, False])
def test_main_secret_prompt_reaches_no_cloud_sink(monkeypatch, routed_spawn):
    argv, urls, _ = _run_main(monkeypatch, RESEARCH + SECRET_PROMPT, direct=True, routed_spawn=routed_spawn)
    assert argv == [], "a cloud CLI was started for a secret prompt"
    assert urls == [], [u for u, _ in urls]


@pytest.mark.parametrize("routed_spawn", [True, False])
def test_main_plain_prompt_still_reaches_the_cloud_paths(monkeypatch, routed_spawn):
    """No regression, and the tracer is live: NS3 starts Codex (routed_spawn on or off); with routed spawn off the
    DIRECT path dials the API as well."""
    argv, _, _ = _run_main(monkeypatch, RESEARCH + PLAIN_PROMPT, direct=True, routed_spawn=routed_spawn)
    assert _carries(argv, PLAIN_PROMPT)


SIMPLE = "what is a mutex? "


@pytest.mark.parametrize("secret", [True, False])
def test_main_direct_executor_sink(monkeypatch, secret):
    """DIRECT (LLM_ROUTER_SUBAGENT_DIRECT=on, routed spawn off, Codex off so nothing else runs): the API sink is
    dialled for a plain prompt (control) and never for a secret one."""
    argv, urls, _ = _run_main(monkeypatch, SIMPLE + (SECRET_PROMPT if secret else PLAIN_PROMPT),
                              direct=True, routed_spawn=False, cli=False)
    assert argv == []
    assert (urls == []) if secret else bool(urls)


def test_main_cloud_prefix_overrides(monkeypatch):
    argv, _, _ = _run_main(monkeypatch, "cloud: " + RESEARCH + SECRET_PROMPT)
    assert _carries(argv, SECRET), "positive control: the override reaches the CLI"
    assert not _carries(argv, "cloud:")

