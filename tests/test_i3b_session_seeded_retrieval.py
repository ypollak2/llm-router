"""I3b: semantic retrieval is seeded from what the session just touched.

Retrieval is lexical — it needs an identifier or path in the query. Real prompts
rarely carry one ("keep going", "why is that number so high?"): replayed over
456 real prompts in sessions working on this repo (2026-09-24), code was
retrieved for 0.9% of them from the prompt alone and 98.9% when the query also
carried the file paths of the session's recent tool calls. The prompt the model
sees is unchanged; only the retrieval query gains the seeds.
"""
from __future__ import annotations

import subprocess

import pytest


@pytest.fixture
def indexed_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_OKF", "on")
    monkeypatch.delenv("LLM_ROUTER_SEMANTIC_ARM", raising=False)
    monkeypatch.delenv("LLM_ROUTER_SEMANTIC_SOURCE", raising=False)
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "ledger.py").write_text(
        "def settle_balance(rows):\n    return sum(r['amount'] for r in rows)\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
                   cwd=repo, check=True)
    from llm_router.semantic import indexer
    indexer.index(repo)
    return repo


def test_a_prompt_with_no_identifier_retrieves_nothing_on_its_own(indexed_repo):
    """Premise: the lexical retriever cannot use 'keep going'."""
    from llm_router.semantic.retrieve import retrieve
    assert retrieve("keep going", root=indexed_repo).entities == []


def test_the_session_seeds_retrieval(indexed_repo):
    from llm_router import session_store
    from llm_router.context_injection import inject
    sid = "i3bsess-77aa1c"
    session_store.record_event(sid, "tool_call",
                               'Edit({"file_path": "pkg/ledger.py", "old_string": "x"})',
                               role="tool", tool="Edit")
    out = inject("keep going", root=str(indexed_repo), session_id=sid)
    assert "settle_balance" in out, "the session's recent file did not seed retrieval"
    assert out.rstrip().endswith("keep going"), "the prompt itself must be unchanged"


def test_no_session_means_no_seeds(indexed_repo):
    from llm_router.context_injection import inject
    assert "settle_balance" not in inject("keep going", root=str(indexed_repo))
