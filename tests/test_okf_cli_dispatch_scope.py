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

The scope itself must come from the CALLER. route_and_call resolves it once
(explicit project_root, else the MCP client's workspace root) and hands it to
`_dispatch_model_loop(scope_root=...)`; `_cli_scope_root(hint)` then prefers
that hint to the zero-arg cwd walk, which on the long-lived server (cwd=$HOME)
can only answer None. The route_and_call tests below drive that whole path
with the server cwd outside any repo.

agent-route.py's hook already runs with cwd inside the caller's repo, so its
subprocess cwd was already correct without passing `working_dir` — but
`inject()` still never got a `root`, so OKF retrieval there was unscoped
(global), not scoped to the repo the hook is running in. Same fix, different
call site: a new `_delegation_scope_root()` helper feeds `context_root` into
both delegation paths, preferring the hook payload's `cwd` over the hook
process's own cwd.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path

import pytest

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
# 1. router.py: route_and_call's resolved scope reaches the CLI dispatch.
#
#    The long-lived MCP server's cwd is $HOME, so the zero-arg cwd walk in
#    `_cli_scope_root()` answers None there. The caller's scope (explicit
#    project_root, or the MCP client's workspace root) is resolved once in
#    route_and_call and must be handed down to the dispatch loop.
# ---------------------------------------------------------------------------

def _make_repo(base: Path, name: str) -> Path:
    repo = base / name
    (repo / ".git").mkdir(parents=True)
    return repo


@pytest.fixture
def _server_cwd_outside_any_repo(tmp_path, monkeypatch):
    """The long-lived server: cwd is a directory that is not in any repo, and
    no environment override names a project."""
    home = tmp_path / "server-home"
    home.mkdir()
    monkeypatch.chdir(home)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_DIR", raising=False)
    return home


def test_cli_scope_root_hint_wins_over_a_non_repo_cwd(tmp_path, _server_cwd_outside_any_repo):
    from llm_router.router import _cli_scope_root

    repo = _make_repo(tmp_path, "repo-x")
    assert _cli_scope_root() is None  # the bug: no hint, cwd is not a repo
    assert _cli_scope_root(str(repo)) == str(repo.resolve())


def test_cli_scope_root_hint_is_walked_to_the_repo_root(tmp_path, _server_cwd_outside_any_repo):
    from llm_router.router import _cli_scope_root

    repo = _make_repo(tmp_path, "repo-x")
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    assert _cli_scope_root(str(sub)) == str(repo.resolve())


def _clear_subprocess_backend_gates(monkeypatch):
    """Importing llm_router.gateway does
    os.environ.setdefault("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", "codex,gemini_cli")
    for the rest of the process, so a test file that runs after one that imports
    the gateway sees the local CLIs as disabled and the chain loses its codex /
    gemini_cli entries. Make these tests independent of collection order."""
    monkeypatch.delenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", raising=False)
    monkeypatch.delenv("LLM_ROUTER_BLOCK_PROVIDERS", raising=False)


def _codex_route_env(monkeypatch):
    _clear_subprocess_backend_gates(monkeypatch)
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: True)
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: False)
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda: 0.97)


def _capturing_run_codex(captured: dict):
    from llm_router.codex_agent import CodexResult

    async def _run(prompt, **kwargs):
        captured.update(kwargs)
        captured["prompt"] = prompt
        return CodexResult(content="ok", model="gpt-5.4", exit_code=0, duration_sec=0.1)

    return _run


@pytest.mark.asyncio
async def test_route_and_call_explicit_project_root_reaches_codex_dispatch(
    temp_db, mock_env, monkeypatch, tmp_path, _server_cwd_outside_any_repo,
):
    """Server cwd is not a repo; the caller names repo X via project_root. The
    codex dispatch must receive X as context_root -- and must not be given a
    working_dir, which would move the subprocess."""
    from llm_router.router import route_and_call
    from llm_router.types import RoutingProfile, TaskType

    _codex_route_env(monkeypatch)
    repo_x = _make_repo(tmp_path, "repo-x")
    captured: dict = {}
    monkeypatch.setattr("llm_router.router.run_codex", _capturing_run_codex(captured))

    resp = await route_and_call(
        TaskType.CODE, "refactor this function",
        profile=RoutingProfile.BALANCED, project_root=str(repo_x),
    )

    assert resp.provider == "codex"
    assert captured["context_root"] == str(repo_x.resolve())
    assert "working_dir" not in captured


@pytest.mark.asyncio
async def test_route_and_call_mcp_workspace_root_reaches_codex_dispatch(
    temp_db, mock_env, monkeypatch, tmp_path, _server_cwd_outside_any_repo,
):
    """Same, for the MCP-roots path: no project_root, the client's workspace
    root (via mcp_roots.root_from_ctx) names repo X."""
    from llm_router.router import route_and_call
    from llm_router.types import RoutingProfile, TaskType

    _codex_route_env(monkeypatch)
    repo_x = _make_repo(tmp_path, "repo-x")

    async def _root_from_ctx(_ctx):
        return str(repo_x / "src")

    (repo_x / "src").mkdir()
    monkeypatch.setattr("llm_router.mcp_roots.root_from_ctx", _root_from_ctx)
    captured: dict = {}
    monkeypatch.setattr("llm_router.router.run_codex", _capturing_run_codex(captured))

    await route_and_call(TaskType.CODE, "refactor this function", profile=RoutingProfile.BALANCED)

    assert captured["context_root"] == str(repo_x.resolve())


@pytest.mark.asyncio
async def test_route_and_call_two_callers_get_their_own_scope(
    temp_db, mock_env, monkeypatch, tmp_path, _server_cwd_outside_any_repo,
):
    """One long-lived server, two callers in two repos: each dispatch is scoped
    to its own caller's repo, not to a shared or leftover value."""
    from llm_router.router import route_and_call
    from llm_router.types import RoutingProfile, TaskType

    _codex_route_env(monkeypatch)
    seen: list = []

    async def _run(prompt, **kwargs):
        from llm_router.codex_agent import CodexResult
        seen.append(kwargs.get("context_root"))
        return CodexResult(content="ok", model="gpt-5.4", exit_code=0, duration_sec=0.1)

    monkeypatch.setattr("llm_router.router.run_codex", _run)
    repo_x = _make_repo(tmp_path, "repo-x")
    repo_y = _make_repo(tmp_path, "repo-y")
    for repo in (repo_x, repo_y):
        await route_and_call(
            TaskType.CODE, "refactor this function",
            profile=RoutingProfile.BALANCED, project_root=str(repo),
        )

    assert seen == [str(repo_x.resolve()), str(repo_y.resolve())]


@pytest.mark.asyncio
async def test_route_and_call_no_scope_anywhere_gives_codex_no_context_root(
    temp_db, mock_env, monkeypatch, _server_cwd_outside_any_repo,
):
    """No project_root, no MCP root, cwd not in a repo: nothing identifies a
    project, so nothing is injected (context_root is None, not a guess)."""
    from llm_router.router import route_and_call
    from llm_router.types import RoutingProfile, TaskType

    _codex_route_env(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr("llm_router.router.run_codex", _capturing_run_codex(captured))

    await route_and_call(TaskType.CODE, "refactor this function", profile=RoutingProfile.BALANCED)

    assert "context_root" in captured
    assert captured["context_root"] is None


@pytest.mark.asyncio
async def test_route_and_call_explicit_project_root_reaches_gemini_dispatch(
    temp_db, mock_env, monkeypatch, tmp_path, _server_cwd_outside_any_repo,
):
    from llm_router.router import route_and_call
    from llm_router.types import RoutingProfile, TaskType

    _clear_subprocess_backend_gates(monkeypatch)
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: True)
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda: 0.97)
    repo_x = _make_repo(tmp_path, "repo-x")
    captured: dict = {}

    async def _run(prompt, **kwargs):
        from llm_router.gemini_cli_agent import GeminiCLIResult
        captured.update(kwargs)
        return GeminiCLIResult(content="ok", model="gemini-2.5-flash", exit_code=0, duration_sec=0.1)

    monkeypatch.setattr("llm_router.router.run_gemini_cli", _run)

    resp = await route_and_call(
        TaskType.CODE, "refactor this function",
        profile=RoutingProfile.BALANCED, project_root=str(repo_x),
    )

    assert resp.provider == "gemini_cli"
    assert captured["context_root"] == str(repo_x.resolve())


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


def test_delegation_scope_root_prefers_payload_cwd_over_process_cwd(tmp_path, monkeypatch):
    """The hook payload's `cwd` is where the calling session is running; the
    hook process's own cwd is only a fallback and may be somewhere else."""
    mod = _load_agent_route_hook()
    repo_a = _make_repo(tmp_path, "repo-a")
    repo_b = _make_repo(tmp_path, "repo-b")
    monkeypatch.chdir(repo_b)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_DIR", raising=False)

    assert mod._delegation_scope_root(str(repo_a)) == str(repo_a.resolve())
    assert mod._delegation_scope_root(str(repo_a / "src")) == str(repo_a.resolve())
    assert mod._delegation_scope_root() == str(repo_b.resolve())  # fallback unchanged


def test_delegation_scope_root_payload_cwd_outside_a_repo_is_none(tmp_path, monkeypatch):
    """A payload cwd that is not in a repo must not become a scope of its own,
    even when the process cwd happens to be inside one."""
    mod = _load_agent_route_hook()
    repo_b = _make_repo(tmp_path, "repo-b")
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    monkeypatch.chdir(repo_b)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_DIR", raising=False)

    assert mod._delegation_scope_root(str(elsewhere)) is None


@pytest.mark.parametrize("which", ["cli", "codex_subagent"])
def test_delegation_uses_payload_cwd_when_it_differs_from_process_cwd(which, tmp_path, monkeypatch):
    mod = _load_agent_route_hook()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")
    repo_a = _make_repo(tmp_path, "repo-a")  # the session's repo (payload cwd)
    repo_b = _make_repo(tmp_path, "repo-b")  # wherever the hook process runs
    monkeypatch.chdir(repo_b)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_DIR", raising=False)

    from llm_router.codex_agent import CodexResult

    captured = {}

    async def _fake_run_codex(prompt, timeout=None, **kwargs):
        captured.update(kwargs)
        return CodexResult(content="answer", model="gpt-5.5", exit_code=0, duration_sec=0.1)

    monkeypatch.setattr("llm_router.codex_agent.is_codex_available", lambda: True)
    monkeypatch.setattr("llm_router.codex_agent.run_codex", _fake_run_codex)

    if which == "cli":
        result = mod._try_cli_delegation(
            "refactor and run the tests", "code", "complex", "sess-cwd-1", cwd=str(repo_a))
    else:
        result = mod._try_codex_subagent_delegation(
            "analyze the auth module for bugs", "analyze", "moderate", "general-purpose",
            "sess-cwd-2", cwd=str(repo_a))

    assert result == "answer"
    assert captured.get("context_root") == str(repo_a.resolve())


def test_main_passes_the_hook_payload_cwd_to_delegation(tmp_path, monkeypatch, capsys):
    """End to end through `main()`: the stdin payload's `cwd` reaches
    run_codex's context_root, while the process cwd is a different repo."""
    import io
    import json

    mod = _load_agent_route_hook()
    # main() writes session state (budget, depth, logs): keep all of it in tmp.
    (tmp_path / ".llm-router").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    monkeypatch.setenv("LLM_ROUTER_SESSIONS_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    repo_a = _make_repo(tmp_path, "repo-a")
    repo_b = _make_repo(tmp_path, "repo-b")
    monkeypatch.chdir(repo_b)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_DIR", raising=False)

    from llm_router.codex_agent import CodexResult

    captured = {}

    async def _fake_run_codex(prompt, timeout=None, **kwargs):
        captured.update(kwargs)
        return CodexResult(content="the root cause is X", model="gpt-5.5", exit_code=0, duration_sec=0.1)

    monkeypatch.setattr("llm_router.codex_agent.is_codex_available", lambda: True)
    monkeypatch.setattr("llm_router.codex_agent.run_codex", _fake_run_codex)
    payload = {
        "tool_name": "Agent",
        "cwd": str(repo_a),
        "tool_input": {"prompt": "analyze the auth module for bugs", "subagent_type": "general-purpose"},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    mod.main()  # the delegated path emits a block decision and returns
    out = capsys.readouterr().out
    assert "delegated to Codex CLI" in out

    assert captured.get("context_root") == str(repo_a.resolve()), out


def test_hooks_mirror_is_byte_identical():
    """hooks/agent-route.py (the distributed copy) must stay byte-identical
    to src/llm_router/hooks/agent-route.py — CI has no separate check for
    this, so a drift here would ship a stale plugin hook silently."""
    repo_root = Path(__file__).parent.parent
    mirror = repo_root / "hooks" / "agent-route.py"
    source = repo_root / "src" / "llm_router" / "hooks" / "agent-route.py"
    assert mirror.read_bytes() == source.read_bytes()


# ---------------------------------------------------------------------------
# 4. semantic.scope helpers the hook uses instead of reading env/cwd itself.
# ---------------------------------------------------------------------------

def test_find_repo_root_walks_up_and_is_none_outside_a_repo(tmp_path):
    from llm_router.semantic.scope import find_repo_root

    repo = _make_repo(tmp_path, "repo-x")
    (repo / "src" / "pkg").mkdir(parents=True)
    bare = tmp_path / "not-a-repo"
    bare.mkdir()

    assert find_repo_root(repo / "src" / "pkg") == repo.resolve()
    assert find_repo_root(bare) is None


def test_resolve_scope_or_none_cwd_argument_replaces_the_process_cwd(
    tmp_path, monkeypatch,
):
    from llm_router.semantic.scope import resolve_scope_or_none

    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_DIR", raising=False)
    repo_a = _make_repo(tmp_path, "repo-a")
    repo_b = _make_repo(tmp_path, "repo-b")
    monkeypatch.chdir(repo_b)

    assert resolve_scope_or_none(cwd=repo_a) == repo_a.resolve()
    assert resolve_scope_or_none() == repo_b.resolve()
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    assert resolve_scope_or_none(cwd=bare) is None
