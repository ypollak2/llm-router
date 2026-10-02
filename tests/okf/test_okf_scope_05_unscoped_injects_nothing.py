"""OKF-SCOPE-05 — an unscoped `inject()` call must get NOTHING outside a repo.

Diagnosis: ``okf_context_diagnosis.md`` §3, root cause #5. `context_injection.inject()`
fell back, with no `root=` argument, to `okf.find_relevant(prompt, limit=limit)` with
no explicit scope at all. That resolves via `okf.project_root()` -- `resolve_scope()`
-- which ALWAYS answers, falling back to the caller's cwd itself when no `.git` is
found above it. The MCP server is long-lived with a fixed cwd (`$HOME` in the field),
so every unscoped call across every project on the machine shared that ONE bucket:
whatever an earlier unscoped call from an unrelated project had written there was
then offered to a later, unrelated, also-unscoped prompt.

Fix: `inject()` now resolves an absent root with `semantic.scope.resolve_scope_or_none()`,
which answers None when the cwd is not inside a repo and nothing else identifies a
project -- so "no known project" means no retrieval, not "whatever is in the $HOME
bucket".
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from llm_router import context_injection, okf


@pytest.fixture(autouse=True)
def _clean_cache():
    okf.invalidate_cache()
    yield
    okf.invalidate_cache()


@pytest.fixture
def home_outside_any_repo(tmp_path, monkeypatch):
    """The MCP server's situation: a fixed cwd with no `.git` above it, and no
    explicit project override set."""
    cwd = tmp_path / "home"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_DIR", raising=False)
    return cwd


def _write_doc(store: Path, title: str, symbols: list[str]) -> None:
    store.mkdir(parents=True, exist_ok=True)
    (store / "doc.md").write_text(textwrap.dedent(f"""\
        ---
        type: SourceFile
        title: {title}
        description: d
        tags: []
        key_symbols: {symbols}
        ---

        Defines: {", ".join(symbols)}.
        """), encoding="utf-8")


def test_unscoped_call_outside_any_repo_injects_nothing(home_outside_any_repo, monkeypatch):
    """The shared bucket an earlier unscoped call wrote to (keyed by the cwd
    itself) must not answer a LATER unscoped call, even when the prompt names
    exactly what that bucket holds."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home_outside_any_repo.parent / "llm_router_home"))
    shared = okf._knowledge_dir() / "projects" / okf.project_slug(home_outside_any_repo)
    _write_doc(shared, "search/query_planner.py", ["plan_query", "QueryPlan", "cost_estimate"])
    okf.invalidate_cache()

    prompt = "why does plan_query return the wrong cost_estimate for QueryPlan?"
    out = context_injection.inject(prompt, root=None)
    assert out == prompt
    assert "<knowledge_context>" not in out


def test_unscoped_call_inside_a_repo_still_injects(home_outside_any_repo, monkeypatch):
    """The fix must not silence retrieval for the ordinary case: a cwd that IS
    inside a repo still answers without an explicit root."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home_outside_any_repo.parent / "llm_router_home"))
    repo = home_outside_any_repo.parent / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    store = okf._knowledge_dir() / "projects" / okf.project_slug(repo.resolve())
    _write_doc(store, "billing/invoice_reconciler.py", ["reconcile_invoice", "ledger_delta", "InvoiceMismatch"])
    okf.invalidate_cache()

    out = context_injection.inject("reconcile_invoice raises InvoiceMismatch on ledger_delta drift", root=None)
    assert "<knowledge_context>" in out
    assert "invoice_reconciler" in out


def test_explicit_root_is_unaffected_by_the_fix(home_outside_any_repo, monkeypatch):
    """A caller that names a project still gets it, even outside any repo and
    even with an unrelated-looking cwd -- the explicit hint is unconditional."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home_outside_any_repo.parent / "llm_router_home"))
    named = home_outside_any_repo.parent / "named-project"
    named.mkdir()
    store = okf._knowledge_dir() / "projects" / okf.project_slug(named.resolve())
    _write_doc(store, "billing/invoice_reconciler.py", ["reconcile_invoice", "ledger_delta", "InvoiceMismatch"])
    okf.invalidate_cache()

    out = context_injection.inject(
        "reconcile_invoice raises InvoiceMismatch on ledger_delta drift", root=str(named))
    assert "<knowledge_context>" in out
    assert "invoice_reconciler" in out
