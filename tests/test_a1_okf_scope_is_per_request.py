"""Stage A — OKF scope must be an argument, not ambient process state.

`find_relevant(prompt, limit, base)` takes no scope parameter. It calls
`project_root()` internally, which reads `$LLM_ROUTER_PROJECT_ROOT` or the cwd. That
is correct for the hook — one process, one prompt, the user's real cwd — and wrong
for anything long-lived serving several projects.

Demonstrated live on 2026-09-10. Asking the MCP `llm()` tool "what does
_write_source_concept do" returned "I do not have enough information about this
repository", because the server's cwd is `$HOME`:

    project_root() -> /Users/yaliandrona
    store          -> projects/yaliandrona-7cc2188c
    docs visible   -> 2            (1068 in the llm-router scope)
    retrieved      -> NOTHING

Setting `$LLM_ROUTER_PROJECT_ROOT` on the server "fixes" it by pinning the process
to one repository, which is wrong the moment two are in use. Scope has to travel
with the request.

`_get_bundle`'s cache is the subtle half. Its own docstring already explains why the
key includes the project dir — "the MCP server is a long-running process that can
serve requests for several" — but it computes that dir from the same ambient state,
so an explicit per-request root would be ignored by the cache for up to the 60s TTL.
The bug the docstring warns about, reintroduced one layer down.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from llm_router import okf


def _store(base: Path, slug_root: Path, title: str, symbols: list[str]) -> None:
    d = okf.project_knowledge_dir(root=slug_root, base=base) / "source"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{title.replace('/', '_')}.md").write_text(
        textwrap.dedent(f"""\
            ---
            type: SourceFile
            title: {title}
            description: 'Defines: {", ".join(symbols)}'
            key_symbols: [{", ".join(symbols)}]
            tags: [source-file, py]
            ---

            Defines: {", ".join(symbols)}
            """),
        encoding="utf-8",
    )


@pytest.fixture
def two_projects(tmp_path, monkeypatch):
    """Two repos with disjoint symbols, and a cwd belonging to neither."""
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)
    base = tmp_path / "knowledge"
    alpha, bravo = tmp_path / "repo-alpha", tmp_path / "repo-bravo"
    for r in (alpha, bravo):
        (r / ".git").mkdir(parents=True)
    _store(base, alpha, "alpha/core.py", ["alpha_only_symbol"])
    _store(base, bravo, "bravo/core.py", ["bravo_only_symbol"])
    neither = tmp_path / "somewhere-else"
    neither.mkdir()
    monkeypatch.chdir(neither)
    okf.invalidate_cache()
    return base, alpha, bravo


# ── A1: find_relevant takes a root ──────────────────────────────────────────

def test_an_explicit_root_finds_that_projects_docs(two_projects):
    base, alpha, _ = two_projects
    hits = okf.find_relevant("alpha_only_symbol", base=base, root=alpha)
    assert [h.title for h in hits] == ["alpha/core.py"]


def test_an_explicit_root_excludes_the_other_project(two_projects):
    base, alpha, _ = two_projects
    assert okf.find_relevant("bravo_only_symbol", base=base, root=alpha) == []


def test_the_cwd_is_ignored_when_a_root_is_given(two_projects):
    """The live failure: cwd is $HOME and has no docs, the caller knows better."""
    base, alpha, _ = two_projects
    assert okf.find_relevant("alpha_only_symbol", base=base) == [], (
        "precondition: the cwd scope should see nothing"
    )
    assert okf.find_relevant("alpha_only_symbol", base=base, root=alpha)


def test_omitting_the_root_still_resolves_from_env(two_projects, monkeypatch):
    """Every existing caller passes no root; none of them may change behaviour."""
    base, alpha, _ = two_projects
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(alpha))
    okf.invalidate_cache()
    assert [h.title for h in okf.find_relevant("alpha_only_symbol", base=base)] == [
        "alpha/core.py"
    ]


def test_a_nonexistent_root_yields_nothing_and_does_not_raise(two_projects):
    base, _, _ = two_projects
    assert okf.find_relevant("alpha_only_symbol", base=base, root=Path("/no/such/repo")) == []


# ── A2: the cache key — the subtle half ─────────────────────────────────────

def test_two_roots_in_one_process_do_not_share_a_cache(two_projects):
    """The MCP server serves several projects from one process, inside one TTL.

    This is the failure `_get_bundle`'s docstring already describes, arriving one
    layer down: the key is computed from ambient state, so an explicit root would
    be ignored until the 60s TTL expired.
    """
    base, alpha, bravo = two_projects
    a = okf.find_relevant("alpha_only_symbol", base=base, root=alpha)
    b = okf.find_relevant("bravo_only_symbol", base=base, root=bravo)
    assert [h.title for h in a] == ["alpha/core.py"]
    assert [h.title for h in b] == ["bravo/core.py"], (
        "the second project got the first project's cached bundle"
    )


def test_alternating_between_roots_stays_correct(two_projects):
    """Not just A-then-B: a real server interleaves them."""
    base, alpha, bravo = two_projects
    for _ in range(3):
        assert okf.find_relevant("alpha_only_symbol", base=base, root=alpha)
        assert okf.find_relevant("bravo_only_symbol", base=base, root=bravo)
        assert okf.find_relevant("bravo_only_symbol", base=base, root=alpha) == []


def test_the_cache_still_works_within_one_root(two_projects):
    """Scoping the key must not defeat caching — that would reload 1000+ docs
    on every prompt."""
    base, alpha, _ = two_projects
    first = okf._get_bundle(base=base, root=alpha)
    second = okf._get_bundle(base=base, root=alpha)
    assert first is second, "same-root lookups stopped hitting the cache"


# ── A3: retrieval roots honour the argument ─────────────────────────────────

def test_retrieval_roots_point_at_the_given_project(two_projects):
    base, alpha, _ = two_projects
    roots = okf._retrieval_roots(base=base, root=alpha)
    assert any(okf.project_slug(alpha) in str(r) for r in roots)
    assert not any(okf.project_slug(two_projects[2]) in str(r) for r in roots)


# ── A4/A5: the plumbing that carries it ─────────────────────────────────────

def test_route_and_call_accepts_a_project_root():
    import inspect

    from llm_router.router import route_and_call

    assert "project_root" in inspect.signature(route_and_call).parameters, (
        "the router cannot forward a caller's scope to OKF"
    )


def test_route_payload_forwards_project_root(monkeypatch):
    """The HTTP surfaces hand the router a dict; the key has to survive the trip."""
    import asyncio

    seen = {}

    async def _fake_route_and_call(*a, **kw):
        seen.update(kw)

        class _R:
            content = "ok"
            model = "ollama/x"
            provider = "ollama"
            cost_usd = 0.0
            input_tokens = output_tokens = 1
            latency_ms = 1
            # route_payload_async reads this for the savings record; a stub
            # missing it fails for the wrong reason.
            complexity = "simple"
        return _R()

    import llm_router.router as router_mod
    monkeypatch.setattr(router_mod, "route_and_call", _fake_route_and_call)

    from llm_router.route_server import route_payload_async
    asyncio.run(route_payload_async({
        "prompt": "hi", "task_type": "code", "complexity": "simple",
        "project_root": "/tmp/some-repo",
    }))
    assert seen.get("project_root") == "/tmp/some-repo"
