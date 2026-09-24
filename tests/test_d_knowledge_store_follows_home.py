"""D (CTX-04): nothing may write into the user's real knowledge store by accident.

Measured 2026-09-24: ~/.llm-router/knowledge/projects held 5,469 project dirs
(307 MB), almost all test fixtures (`test_*`, `repo-<hex>`) plus the capability
probe's temp dirs (`llm_router-agentic-probe-*`), each carrying a semantic
index.sqlite. Two causes:

1. okf.py's 15 functions took `base: Path = _knowledge_dir()` — a default
   evaluated at IMPORT, before conftest points LLM_ROUTER_HOME at a sandbox, so
   every later call wrote into the real home. Seen live: the I4 tests, run once
   under the real HOME, created three new project dirs at 16:32.
2. The probe runs the agent loop on a mkdtemp root, which indexes that root.
"""
from __future__ import annotations

import inspect

from llm_router import okf


def test_the_store_follows_home_after_import(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "late"))
    d = okf.project_knowledge_dir(root=tmp_path)
    assert str(d).startswith(str(tmp_path / "late")), d


def test_no_store_function_freezes_its_base_at_import():
    frozen = []
    for name, fn in inspect.getmembers(okf, inspect.isfunction):
        p = inspect.signature(fn).parameters.get("base")
        if p is not None and p.default is not inspect.Parameter.empty and p.default is not None:
            frozen.append(f"{name}(base={p.default!r})")
    assert not frozen, frozen


def test_the_semantic_index_lands_in_the_sandbox(monkeypatch, tmp_path):
    from llm_router.semantic import store
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "late"))
    assert str(store.index_path(root=tmp_path)).startswith(str(tmp_path / "late"))


def test_the_probe_does_not_index_its_temp_dir(monkeypatch):
    from llm_router import agentic_registry as reg
    seen = {}

    def fake_loop(prompt, model, project_root, timeout_per_call=60, **kw):
        from llm_router import context_injection
        seen["injecting"] = context_injection.enabled()   # the effect, not a variable
        return "done"
    monkeypatch.delenv("LLM_ROUTER_CONTEXT_INJECTION", raising=False)
    from llm_router import context_injection
    assert context_injection.enabled(), "premise: injection is on for the caller"
    monkeypatch.setattr(reg, "run_agent_loop", fake_loop)
    reg.probe_model("m", timeout=5)
    assert seen["injecting"] is False, "the probe tests tool-calling, not retrieval"
    assert context_injection.enabled(), "and restores the caller's setting"


# ── cleanup of what already leaked ───────────────────────────────────────────

def _store(tmp_path, monkeypatch, names):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    projects = okf._knowledge_dir() / "projects"
    for n in names:
        (projects / n / "semantic").mkdir(parents=True)
        (projects / n / "semantic" / "index.sqlite").write_bytes(b"x")
    return projects


DEBRIS = ["test_the_loop_stops_when_its_b0-aaaa1111", "repo-bbbb2222",
          "llm_router-agentic-probe-lfax39bq-cccc3333", "tmpq1w2e3r4-dddd4444",
          "pytest-of-user-eeee5555", "bq_q15_local-ffff6666"]
REAL = ["llm-router-6f6153a7", "s64-workbench-495af0bc", "agenticgraphs-1eb54d64"]


def test_prune_dry_run_lists_debris_and_moves_nothing(tmp_path, monkeypatch):
    projects = _store(tmp_path, monkeypatch, DEBRIS + REAL)
    report = okf.prune_projects(apply=False)
    assert sorted(report["debris"]) == sorted(DEBRIS)
    assert sorted(report["kept"]) == sorted(REAL)
    assert sorted(p.name for p in projects.iterdir()) == sorted(DEBRIS + REAL)


def test_prune_apply_moves_only_debris_and_is_reversible(tmp_path, monkeypatch):
    projects = _store(tmp_path, monkeypatch, DEBRIS + REAL)
    report = okf.prune_projects(apply=True)
    assert sorted(p.name for p in projects.iterdir()) == sorted(REAL)
    moved_to = report["moved_to"]
    assert sorted(p.name for p in moved_to.iterdir()) == sorted(DEBRIS), \
        "moved, not deleted — a user can put one back"
    assert okf.prune_projects(apply=False)["debris"] == []


def test_prune_never_touches_the_current_project(tmp_path, monkeypatch):
    repo = tmp_path / "repo"          # fixture-shaped name, but it is the live project
    repo.mkdir()
    slug = okf.project_slug(repo)
    _store(tmp_path, monkeypatch, [slug])
    assert okf.prune_projects(apply=False, current=repo)["debris"] == []
