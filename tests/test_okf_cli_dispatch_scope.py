"""OKF-SCOPE-06 — CLI-delegated calls get the session's project scope.

router.py's codex / gemini_cli / anthropic-subscription dispatch branches, and
agent-route.py's CLI sub-agent delegation (`_try_cli_delegation` and the NS3
`_try_codex_subagent_delegation`), all called `run_codex` / `run_gemini_cli` /
`run_claude` without ever telling them which project the call belongs to.
Inside each wrapper, `context_injection.inject(prompt, root=working_dir)` then
received `None` — after OKF-SCOPE-05 (#235) that means no repo context at
all, not merely the wrong one.

`working_dir` also doubles as the subprocess cwd for all three wrappers
(`cwd = working_dir or os.getcwd()`). router.py's MCP-server dispatch could
not simply start passing the caller's project root as `working_dir` without
ALSO moving where the Codex/Gemini/Claude subprocess actually runs — a much
bigger behaviour change than "scope OKF retrieval correctly", and not
something a one-shot text-completion dispatch intends. The fix threads a new,
decoupled `context_root` parameter through each wrapper, used for `inject()`
only; the subprocess cwd (and anything derived from `working_dir`) is
untouched.

agent-route.py's hook already runs with cwd inside the caller's repo, so its
subprocess cwd was already correct without passing `working_dir` — but
`inject()` still never got a `root`, so OKF retrieval there was unscoped
(global), not scoped to the repo the hook is running in. Same fix, different
call site: a new `_delegation_scope_root()` helper feeds `context_root` into
both delegation paths.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path

import pytest

ROUTER_PATH = Path(__file__).parent.parent / "src" / "llm_router" / "router.py"
ROUTER_SRC = ROUTER_PATH.read_text(encoding="utf-8")
AGENT_ROUTE_HOOK_PATH = Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "agent-route.py"


def _load_agent_route_hook():
    """Import the hyphenated hook file as a module — same pattern as
    test_agent_route_hook.py's `_load_hook_module()`, kept local here so this
    file does not depend on import order with that module."""
    spec = importlib.util.spec_from_file_location("agent_route_hook_scope_test", AGENT_ROUTE_HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


# ---------------------------------------------------------------------------
# 1. router.py source: the three CLI-dispatch call sites pass context_root=,
#    resolved from the same `_cli_scope_root()` the SCA context already uses.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("call", ["run_codex(", "run_gemini_cli(", "run_claude("])
def test_router_cli_dispatch_passes_context_root(call):
    """Each CLI-dispatch call site must thread context_root=, or OKF
    injection inside the wrapper silently falls back to None (no context)."""
    idx = ROUTER_SRC.index(call)
    window = ROUTER_SRC[idx: idx + 600]
    assert "context_root=" in window, (
        f"router.py's {call} call site does not pass context_root=:\n{window}"
    )


@pytest.mark.parametrize("call", ["run_codex(", "run_gemini_cli(", "run_claude("])
def test_router_cli_dispatch_scope_root_is_the_cli_scope_resolver(call):
    """context_root must come from `_cli_scope_root()` — the same resolver
    the SCA context (`_cli_prompt_with_context`) already uses for these three
    branches — not some other, newly invented notion of scope."""
    idx = ROUTER_SRC.index(call)
    window = ROUTER_SRC[max(0, idx - 400): idx + 600]
    assert "_cli_scope_root()" in window, (
        f"No _cli_scope_root() resolution found near the {call} call site:\n{window}"
    )


def test_router_cli_dispatch_does_not_set_working_dir():
    """The fix must not start threading the scope root as working_dir — that
    would also move the Codex/Gemini/Claude subprocess's cwd, a bigger and
    unintended behaviour change. Only context_root= is new here."""
    for call in ("run_codex(", "run_gemini_cli(", "run_claude("):
        idx = ROUTER_SRC.index(call)
        window = ROUTER_SRC[idx: idx + 600]
        assert "working_dir=" not in window, (
            f"router.py's {call} call site now sets working_dir=, which also "
            f"changes the subprocess cwd — use context_root= instead:\n{window}"
        )


# ---------------------------------------------------------------------------
# 2. run_codex / run_gemini_cli / run_claude: context_root scopes injection
#    independently of working_dir / the subprocess cwd.
# ---------------------------------------------------------------------------

class _FakeProc:
    returncode = 0

    async def wait(self):
        pass

    @property
    def stdout(self):
        return self._stdout_gen()

    @property
    def stderr(self):
        return self._stderr_gen()

    async def _stdout_gen(self):
        import json as _json
        yield _json.dumps({"type": "item.completed", "item": {"text": "OK"}}).encode() + b"\n"

    async def _stderr_gen(self):
        return
        yield  # pragma: no cover - makes this an async generator


@pytest.fixture
def _mock_codex(monkeypatch):
    from llm_router import codex_agent

    captured: dict = {}

    async def _fake_invoke(*args, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return _FakeProc()

    def _fake_inject(prompt, *, root=None, **kwargs):
        captured["inject_root"] = root
        return prompt

    monkeypatch.setattr(codex_agent, "find_codex_binary", lambda: "/fake/codex")
    monkeypatch.setattr(codex_agent.asyncio, "create_subprocess_exec", _fake_invoke)
    monkeypatch.setattr("llm_router.safe_subprocess.get_safe_env", lambda: {})
    monkeypatch.setattr("llm_router.context_injection.inject", _fake_inject)
    return captured


def test_run_codex_context_root_scopes_injection_without_moving_cwd(_mock_codex, tmp_path):
    """A session has a project scope (context_root) that differs from
    working_dir — e.g. the MCP server's working_dir is unset/None while the
    caller's session scope is a real repo. inject() must see the session
    scope; the subprocess cwd must still follow working_dir, untouched."""
    from llm_router.codex_agent import run_codex

    session_scope = str(tmp_path / "caller-repo")
    result = asyncio.run(run_codex("hi", working_dir=None, context_root=session_scope))

    assert result.success
    assert _mock_codex["inject_root"] == session_scope
    assert _mock_codex["cwd"] == os.getcwd()  # working_dir=None -> os.getcwd(), unaffected by context_root


def test_run_codex_context_root_defaults_to_working_dir(_mock_codex, tmp_path):
    """Backward compatible: a caller that only ever knew about working_dir
    (no context_root given) keeps injecting scoped to working_dir, exactly as
    before this change."""
    from llm_router.codex_agent import run_codex

    wd = str(tmp_path)
    asyncio.run(run_codex("hi", working_dir=wd))

    assert _mock_codex["inject_root"] == wd
    assert _mock_codex["cwd"] == wd


def test_run_codex_no_scope_at_all_injects_nothing(_mock_codex):
    """Behavioural half of the "no scope means nothing is injected" pair:
    with neither working_dir nor context_root, inject() is asked for root=None
    — the same "no known project" signal OKF-SCOPE-05 made safe (None, not a
    wrong bucket), not a silently-wrong default."""
    from llm_router.codex_agent import run_codex

    asyncio.run(run_codex("hi"))

    assert _mock_codex["inject_root"] is None


@pytest.fixture
def _mock_gemini(monkeypatch):
    from llm_router import gemini_cli_agent

    captured: dict = {}

    async def _fake_invoke(*args, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return _FakeProc()

    def _fake_inject(prompt, *, root=None, **kwargs):
        captured["inject_root"] = root
        return prompt

    monkeypatch.setattr(gemini_cli_agent, "find_gemini_binary", lambda: "/fake/gemini")
    monkeypatch.setattr(gemini_cli_agent.asyncio, "create_subprocess_exec", _fake_invoke)
    monkeypatch.setattr("llm_router.safe_subprocess.get_safe_env", lambda: {})
    monkeypatch.setattr("llm_router.context_injection.inject", _fake_inject)
    return captured


def test_run_gemini_cli_context_root_scopes_injection_without_moving_cwd(_mock_gemini, tmp_path):
    from llm_router.gemini_cli_agent import run_gemini_cli

    session_scope = str(tmp_path / "caller-repo")
    result = asyncio.run(run_gemini_cli("hi", working_dir=None, context_root=session_scope))

    assert result.success
    assert _mock_gemini["inject_root"] == session_scope
    assert _mock_gemini["cwd"] == os.getcwd()


@pytest.fixture
def _mock_claude(monkeypatch):
    from llm_router import claude_agent

    captured: dict = {}

    async def _fake_invoke(*args, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return _FakeProc()

    def _fake_inject(prompt, *, root=None, **kwargs):
        captured["inject_root"] = root
        return prompt

    monkeypatch.setattr(claude_agent, "find_claude_binary", lambda: "/fake/claude")
    monkeypatch.setattr(claude_agent.asyncio, "create_subprocess_exec", _fake_invoke)
    monkeypatch.setattr("llm_router.safe_subprocess.get_safe_env", lambda: {})
    monkeypatch.setattr("llm_router.context_injection.inject", _fake_inject)
    return captured


def test_run_claude_context_root_scopes_injection_without_moving_cwd(_mock_claude, tmp_path):
    from llm_router.claude_agent import run_claude

    session_scope = str(tmp_path / "caller-repo")
    result = asyncio.run(run_claude("hi", working_dir=None, context_root=session_scope))

    assert result.success
    assert _mock_claude["inject_root"] == session_scope
    assert _mock_claude["cwd"] == os.getcwd()


# ---------------------------------------------------------------------------
# 3. agent-route.py: CLI sub-agent delegation gets correct project scope too.
# ---------------------------------------------------------------------------

def test_delegation_scope_root_resolves_inside_a_repo(tmp_path, monkeypatch):
    """The hook already runs with cwd inside the caller's repo — the new
    helper must resolve that repo root, matching what the subprocess cwd
    fallback (`working_dir or os.getcwd()`) already resolves to."""
    mod = _load_agent_route_hook()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_DIR", raising=False)

    assert mod._delegation_scope_root() == str(repo.resolve())


def test_delegation_scope_root_none_outside_any_repo(tmp_path, monkeypatch):
    """Outside any repo and with no override, the resolver must answer None —
    not a confidently wrong cwd-derived bucket (OKF-SCOPE-05's rule, reused)."""
    mod = _load_agent_route_hook()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(home)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_DIR", raising=False)

    assert mod._delegation_scope_root() is None


def test_try_cli_delegation_passes_context_root_to_run_codex(tmp_path, monkeypatch):
    """`_try_cli_delegation`'s Codex branch must thread the resolved scope
    into run_codex's context_root, not leave OKF injection unscoped."""
    mod = _load_agent_route_hook()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_DIR", raising=False)

    from llm_router.codex_agent import CodexResult

    captured = {}

    async def _fake_run_codex(prompt, timeout=None, **kwargs):
        captured.update(kwargs)
        return CodexResult(content="codex answer", model="gpt-5.5", exit_code=0, duration_sec=0.1)

    monkeypatch.setattr("llm_router.codex_agent.is_codex_available", lambda: True)
    monkeypatch.setattr("llm_router.codex_agent.run_codex", _fake_run_codex)

    result = mod._try_cli_delegation("refactor and run the tests", "code", "complex", "sess-scope-1")

    assert result == "codex answer"
    assert captured.get("context_root") == str(repo.resolve())


def test_try_codex_subagent_delegation_passes_context_root(tmp_path, monkeypatch):
    """Same wiring for the NS3 path (`_try_codex_subagent_delegation`) — the
    task names this one explicitly: Codex sub-agent work is measured live, so
    it must get correct project context, not None."""
    mod = _load_agent_route_hook()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_DIR", raising=False)

    from llm_router.codex_agent import CodexResult

    captured = {}

    async def _fake_run_codex(prompt, timeout=None, **kwargs):
        captured.update(kwargs)
        return CodexResult(content="the root cause is X", model="gpt-5.5", exit_code=0, duration_sec=0.1)

    monkeypatch.setattr("llm_router.codex_agent.is_codex_available", lambda: True)
    monkeypatch.setattr("llm_router.codex_agent.run_codex", _fake_run_codex)

    result = mod._try_codex_subagent_delegation(
        "analyze the auth module for bugs", "analyze", "moderate", "general-purpose", "sess-scope-2",
    )

    assert result == "the root cause is X"
    assert captured.get("context_root") == str(repo.resolve())


def test_try_cli_delegation_no_scope_passes_none(tmp_path, monkeypatch):
    """Outside any repo, delegation must still work (fail-open) but pass
    context_root=None rather than inventing a scope — "no scope means nothing
    is injected", not "injected from whatever cwd happens to be"."""
    mod = _load_agent_route_hook()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(home)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_DIR", raising=False)

    from llm_router.codex_agent import CodexResult

    captured = {}

    async def _fake_run_codex(prompt, timeout=None, **kwargs):
        captured.update(kwargs)
        return CodexResult(content="codex answer", model="gpt-5.5", exit_code=0, duration_sec=0.1)

    monkeypatch.setattr("llm_router.codex_agent.is_codex_available", lambda: True)
    monkeypatch.setattr("llm_router.codex_agent.run_codex", _fake_run_codex)

    result = mod._try_cli_delegation("refactor and run the tests", "code", "complex", "sess-scope-3")

    assert result == "codex answer"
    assert captured.get("context_root") is None


def test_hooks_mirror_is_byte_identical():
    """hooks/agent-route.py (the distributed copy) must stay byte-identical
    to src/llm_router/hooks/agent-route.py — CI has no separate check for
    this, so a drift here would ship a stale plugin hook silently."""
    repo_root = Path(__file__).parent.parent
    mirror = repo_root / "hooks" / "agent-route.py"
    source = repo_root / "src" / "llm_router" / "hooks" / "agent-route.py"
    assert mirror.read_bytes() == source.read_bytes()
