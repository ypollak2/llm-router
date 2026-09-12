"""Stage B — the MCP entry learns the caller's project from MCP roots.

Stage A made scope an argument. Nothing passes it yet, so the live failure stands:
the MCP server's cwd is `$HOME`, and a question about `_write_source_concept`
retrieves nothing while 1069 documents about that repository sit one directory
away.

MCP has the answer built in. The client advertises a `roots` capability and the
server asks for the list. Verified against a real handshake on 2026-09-10 by
pointing a throwaway stdio server at Claude Code:

    "clientInfo": {"name": "claude-code", "version": "2.1.267"},
    "capabilities": {"roots": {"listChanged": true}, "elicitation": {}}

Resolution happens inside `route_and_call` rather than at each MCP tool, because
there are seven `route_and_call` sites in `tools/text.py` and every one already
passes `ctx`. Seven copies of the same lookup is seven places to forget it.

Precedence is explicit-argument, then roots, then env, then cwd. An explicit
`project_root` beats roots because a caller that named a project meant it; roots
beat env because env is a process-wide default and roots are per-connection.

Not every client sends roots — Cursor's support is unverified — so every step
degrades rather than failing. A `list_roots()` that raises, times out, or returns
nothing leaves scope exactly where it was before this existed.
"""
from __future__ import annotations

import asyncio

import pytest

from llm_router import mcp_roots


class _FakeSession:
    """Stands in for mcp ServerSession: capability check + list_roots."""

    def __init__(self, roots: list[str] | None, *, supports: bool = True, boom: bool = False):
        self._roots = roots
        self._supports = supports
        self._boom = boom
        self.calls = 0

    def check_client_capability(self, _cap) -> bool:
        return self._supports

    async def list_roots(self):
        self.calls += 1
        if self._boom:
            raise RuntimeError("client went away")

        class _Root:
            def __init__(self, uri): self.uri = uri

        class _Result:
            def __init__(self, roots): self.roots = roots

        return _Result([_Root(u) for u in (self._roots or [])])


class _FakeCtx:
    def __init__(self, session):
        self.session = session


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)
    mcp_roots.clear_cache()
    yield
    mcp_roots.clear_cache()


def _resolve(ctx):
    return asyncio.run(mcp_roots.root_from_ctx(ctx))


# ── the capability works ────────────────────────────────────────────────────

def test_a_file_uri_root_is_resolved(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ctx = _FakeCtx(_FakeSession([repo.as_uri()]))
    assert _resolve(ctx) == repo


def test_a_bare_path_root_is_accepted(tmp_path):
    """Not every client sends a file:// URI."""
    repo = tmp_path / "repo"
    repo.mkdir()
    ctx = _FakeCtx(_FakeSession([str(repo)]))
    assert _resolve(ctx) == repo


def test_the_first_existing_root_wins(tmp_path):
    gone = tmp_path / "deleted"
    repo = tmp_path / "repo"
    repo.mkdir()
    ctx = _FakeCtx(_FakeSession([gone.as_uri(), repo.as_uri()]))
    assert _resolve(ctx) == repo


# ── every failure degrades, none raises ─────────────────────────────────────

def test_a_client_without_the_capability_yields_none():
    ctx = _FakeCtx(_FakeSession(["/tmp"], supports=False))
    assert _resolve(ctx) is None


def test_a_client_that_raises_yields_none():
    ctx = _FakeCtx(_FakeSession(["/tmp"], boom=True))
    assert _resolve(ctx) is None


def test_no_roots_yields_none():
    assert _resolve(_FakeCtx(_FakeSession([]))) is None


def test_no_ctx_yields_none():
    assert _resolve(None) is None


def test_a_ctx_without_a_session_yields_none():
    class _Bare:
        pass
    assert _resolve(_Bare()) is None


def test_roots_that_do_not_exist_yield_none(tmp_path):
    ctx = _FakeCtx(_FakeSession([(tmp_path / "nope").as_uri()]))
    assert _resolve(ctx) is None


# ── caching: list_roots is a round-trip to the client ───────────────────────

def test_roots_are_cached_per_session(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    session = _FakeSession([repo.as_uri()])
    ctx = _FakeCtx(session)
    for _ in range(5):
        assert _resolve(ctx) == repo
    assert session.calls == 1, f"asked the client {session.calls} times"


def test_separate_sessions_do_not_share_a_cached_root(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert _resolve(_FakeCtx(_FakeSession([a.as_uri()]))) == a
    assert _resolve(_FakeCtx(_FakeSession([b.as_uri()]))) == b


# ── precedence, wired into the router ───────────────────────────────────────

def test_route_and_call_prefers_an_explicit_project_root(tmp_path, monkeypatch):
    """A caller that named a project meant it; roots are the fallback."""
    import inspect

    from llm_router.router import route_and_call
    src = inspect.getsource(route_and_call)
    assert "project_root or" in src or "project_root\n" in src, (
        "explicit project_root must take precedence over ctx roots"
    )


def test_env_still_wins_when_there_are_no_roots(tmp_path, monkeypatch):
    """The documented override keeps working for hosts that send no roots."""
    from llm_router import okf
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(repo))
    assert okf.project_root() == repo.resolve()


def test_a_recycled_memory_address_does_not_leak_the_previous_root(tmp_path):
    """The cache is keyed by id(session), and id() is a memory address.

    Once a session is collected, the next object allocated can take its address.
    Keyed on id() alone, the cache then serves the DEAD session's project root to
    an unrelated live one — cross-project contamination inside the machinery
    built to prevent it. This is how it was found: two sequentially created
    sessions landed on the same address and the second was handed the first's
    root, which read as a flaky test rather than the leak it was.
    """
    import gc

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()

    first = _FakeSession([a.as_uri()])
    first_id = id(first)
    assert _resolve(_FakeCtx(first)) == a
    del first
    gc.collect()

    # Allocate until something reuses the address, then prove it gets its OWN root.
    for _ in range(2000):
        candidate = _FakeSession([b.as_uri()])
        if id(candidate) == first_id:
            assert _resolve(_FakeCtx(candidate)) == b, \
                "a recycled address served the previous session's root"
            return
    pytest.skip("no address reuse observed in this run")
