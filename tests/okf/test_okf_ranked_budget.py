"""Ranked OKF retrieval within a token budget (PLAN v16 P1.5 task 1, MUST P1.5-a).

Replaces the fixed top-3 with: every doc scoring >= max(floor, 0.5 x top), best
first, until a token budget (default 1,500). Offline: markdown fixtures, no model.
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


def _store(tmp_path: Path, docs: list[tuple[str, str, str, str]], name="repo") -> Path:
    """docs: (file stem, title, tags, body). Curated type, so no anchor is needed."""
    proj = tmp_path / "projects" / okf.project_slug(tmp_path / name)
    proj.mkdir(parents=True)
    for stem, title, tags, body in docs:
        (proj / f"{stem}.md").write_text(textwrap.dedent(f"""\
            ---
            type: Decision
            title: {title}
            description: {title}
            tags: [{tags}]
            ---

            {body}
            """), encoding="utf-8")
    return tmp_path


def _titles(hits):
    return [c.title for c in hits]


_Q = "explain quorum_gate retry_policy backoff_cap"


def test_relative_cutoff_keeps_every_doc_near_the_top_and_drops_the_tail(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(tmp_path / "repo"))
    # each keyword in the title scores 2 -> 6; the weak doc has one keyword as a tag -> 2
    # (above the floor of 2, below the 0.5 x 6 = 3 cutoff)
    docs = [(f"near{i}", f"quorum_gate retry_policy backoff_cap {i}", "misc", "details") for i in range(5)]
    docs += [("weak", "Unrelated heading", "quorum_gate", "nothing else")]
    base = _store(tmp_path, docs)
    legacy = okf.find_relevant(_Q, limit=3, base=base)
    assert len(legacy) == 3
    ranked = okf.find_relevant(_Q, base=base)
    assert sorted(_titles(ranked)) == [f"quorum_gate retry_policy backoff_cap {i}" for i in range(5)]  # 5 > top-3
    assert "Unrelated heading" not in _titles(ranked)
    everything = okf.find_relevant(_Q, limit=10, base=base)
    assert "Unrelated heading" in _titles(everything), "weak doc must clear the floor or this test proves nothing"


def test_a_single_clear_winner_returns_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(tmp_path / "repo"))
    docs = [("top", "quorum_gate retry_policy backoff_cap", "misc", "x"),
            ("w1", "Other one", "quorum_gate", "x"),
            ("w2", "Other two", "retry_policy", "x")]
    base = _store(tmp_path, docs)
    assert _titles(okf.find_relevant(_Q, base=base)) == ["quorum_gate retry_policy backoff_cap"]
    assert len(okf.find_relevant(_Q, limit=3, base=base)) == 3


def test_budget_ends_the_list_in_rank_order_and_the_top_doc_always_returns(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(tmp_path / "repo"))
    fat = "quorum_gate " + "filler " * 600           # ~1,000+ tokens by chars/4
    docs = [(f"d{i}", f"quorum_gate {i}", "quorum_gate", fat) for i in range(4)]
    base = _store(tmp_path, docs)
    from llm_router.token_budget import estimate_tokens
    one = estimate_tokens(okf.find_relevant("explain quorum_gate", base=base, budget_tokens=10**6)[0].as_context_block())
    assert one > 900
    hits = okf.find_relevant("explain quorum_gate", base=base)               # default 1,500
    assert len(hits) == 1, f"budget 1500 and one doc ~{one} tokens: expected 1, got {len(hits)}"
    assert len(okf.find_relevant("explain quorum_gate", base=base, budget_tokens=one * 2 + 5)) == 2
    assert len(okf.find_relevant("explain quorum_gate", base=base, budget_tokens=1)) == 1  # top always kept
    assert len(okf.find_relevant("explain quorum_gate", base=base, budget_tokens=10**6)) == 4


def test_budget_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(tmp_path / "repo"))
    docs = [(f"d{i}", f"quorum_gate {i}", "quorum_gate", "quorum_gate") for i in range(6)]
    base = _store(tmp_path, docs)
    assert len(okf.find_relevant("quorum_gate", base=base)) == 6
    monkeypatch.setenv("LLM_ROUTER_OKF_BUDGET", "30")
    assert 1 <= len(okf.find_relevant("quorum_gate", base=base)) < 6
    monkeypatch.setenv("LLM_ROUTER_OKF_BUDGET", "garbage")
    assert len(okf.find_relevant("quorum_gate", base=base)) == 6


def test_rank_within_budget_unit():
    def c(title, body="x"):
        return okf.OKFConcept(path=Path("u.md"), type="Decision", title=title, body=body,
                              description="", tags=[], extra={})
    a, b, d = c("a"), c("b"), c("d")
    assert okf.rank_within_budget([], 2, 1500) == []
    # order by score; cutoff = max(floor=2, 0.5 x 10) = 5 drops the score-4 doc
    assert okf.rank_within_budget([(b, 6), (a, 10), (d, 4)], 2, 1500) == [a, b]
    # floor beats the relative cutoff when the top is weak
    assert okf.rank_within_budget([(a, 2), (b, 1)], 2, 1500) == [a]


def test_inject_defaults_to_ranked_retrieval_and_still_honours_an_explicit_limit(monkeypatch, tmp_path):
    seen = []

    def fake(query, limit=None, base=None, root=None, budget_tokens=None):
        seen.append(limit)
        return []

    monkeypatch.setattr(okf, "find_relevant", fake)
    monkeypatch.setattr(context_injection, "enabled", lambda: True)
    context_injection.inject("explain quorum_gate", root=str(tmp_path))
    context_injection.inject("explain quorum_gate", root=str(tmp_path), limit=2)
    assert seen == [None, 2]


# -- retrieval false positives must not rise (MUST P1.5-a, last clause) ---------------

# #233 (okf: explicit filenames anchor retrieval): generic prompts whose capitalised
# short word equals a real file's stem. Source: tests/okf/test_okf_anchor_basename_and_acronym.py.
_PROMPTS_233 = [
    "how does GC work in Java", "BASE image for docker", "best API for weather data in my app",
    "is PY good for data science", "how do I TEST a flask endpoint",
    "what is the CORE idea behind functional programming",
    "how should I manage STATE in a react app", "write a quick CLI tool in bash",
]
# #244 (retrieval: find a constant by its string value): generic prompts naming text that
# is in the repo. Source: tests/semantic/test_retrieve_value_lookup.py.
_PROMPTS_244 = [
    "what's the right value for DATABASE_URL in docker-compose",
    "what does user_id map to in a typical JWT payload",
    "how should I set max_retries for an http client",
]


# Curated (non-SourceFile) docs need no identifier anchor, so they are what can leak
# into a generic prompt. They are the bait: on these prompts the legacy top-3 returns
# some of them, and every one returned is a false positive (the prompts are not about
# this repo). Three docs carry "docker" in title or tags, so top-3 is full; two more match the
# DATABASE_URL prompt only in prose (weaker), so ranked stops before them.
_BAIT = {
    "docker-a": ("Docker image decision", "docker, image", "Base image for docker builds"),
    "docker-b": ("Docker compose notes", "docker, compose", "docker compose database_url"),
    "docker-c": ("Docker cache", "docker", "docker layer cache"),
    "docker-d": ("Registry notes", "registry", "database_url compose registry"),
    "docker-e": ("Volume notes", "volumes", "database_url compose volumes"),
    "react-a": ("React state management", "react, state", "manage state in a react app"),
    "react-b": ("React hooks", "react", "react hooks state"),
    "flask": ("Flask endpoint testing", "flask, endpoint, test", "how to test a flask endpoint"),
}
_STEM_FILES = {"api": "src/llm_router/api.py", "cli": "src/llm_router/cli.py",
               "base": "src/llm_router/base.py", "gc": "src/llm_router/commands/gc.py",
               "test": "src/llm_router/commands/test.py", "core": "src/llm_router/observability/core.py",
               "state": "src/llm_router/state.py", "okf": "src/llm_router/okf.py"}

#: Recorded output of `scripts/okf_fp_baseline.py` (the da31df7 `find_relevant(limit=3)`
#: run on THIS store and THESE prompts): total docs returned over the 11 prompts.
#: Regenerate with that script; `test_baseline_constant_matches_da31df7_source` re-derives
#: it from `git show da31df7:src/llm_router/okf.py` whenever that commit is reachable.
DA31DF7_BASELINE_FP = 9


def _fp_store(tmp_path: Path) -> Path:
    proj = tmp_path / "projects" / okf.project_slug(tmp_path / "repo-fp")
    proj.mkdir(parents=True)
    for stem, (title, tags, body) in _BAIT.items():
        (proj / f"{stem}.md").write_text(
            f"---\ntype: Decision\ntitle: {title}\ndescription: {title}\ntags: [{tags}]\n---\n\n{body}\n",
            encoding="utf-8")
    for stem, title in _STEM_FILES.items():   # #233: bare-stem prompts must never reach these
        (proj / f"{stem}.md").write_text(textwrap.dedent(f"""\
            ---
            type: SourceFile
            title: {title}
            description: A module named {stem}.
            tags: [py]
            key_symbols: [{stem}_entry]
            ---

            Defines: {stem}_entry.
            """), encoding="utf-8")
    return tmp_path


def _fp_counts(base: Path, prompts, find):
    legacy = ranked = 0
    for p in prompts:
        okf.invalidate_cache()
        legacy += len(find(p, limit=3, base=base))
        okf.invalidate_cache()
        ranked += len(find(p, base=base))
    return legacy, ranked


def test_retrieval_false_positives_not_above_the_fixed_top3_baseline(tmp_path, monkeypatch, capsys):
    """FP = a doc returned for a prompt that is not about this repo.

    SCOPE, stated plainly: the #233 and #244 prompt sets run through `okf.find_relevant`
    here. They do NOT run through the #244 semantic retriever (`semantic/retrieve.py`),
    which this PR does not change. The sets are the prompts of #233/#244's own tests,
    over a synthetic store (bait docs + the #233 stem files), not the real index.

    The legacy figure is `find_relevant(limit=3)`, the same code path as da31df7: that
    module differs from this PR's base by one comment line, and the limit=3 branch is
    unchanged (see DA31DF7_BASELINE_FP and the git-derived test below). n and both
    counts are printed. Non-vacuous: the legacy count must be >= 1, ranked must be
    strictly lower here, and the control test shows an everything-returning ranker fails.
    """
    base = _fp_store(tmp_path)
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(tmp_path / "repo-fp"))
    prompts = _PROMPTS_233 + _PROMPTS_244
    legacy, ranked = _fp_counts(base, prompts, okf.find_relevant)
    with capsys.disabled():
        print(f"\nOKF FP docs over n={len(prompts)} prompts "
              f"(#233={len(_PROMPTS_233)}, #244={len(_PROMPTS_244)}), store={len(_BAIT) + len(_STEM_FILES)} docs: "
              f"da31df7-baseline={DA31DF7_BASELINE_FP}, legacy limit=3={legacy}, ranked={ranked}")
    assert len(prompts) == 11
    assert legacy == DA31DF7_BASELINE_FP, "legacy path drifted from the recorded da31df7 count"
    assert legacy >= 1, "vacuous: the baseline has no false positive, so the check cannot fail"
    assert ranked <= DA31DF7_BASELINE_FP
    assert ranked < legacy, "ranked should drop the tail the top-3 kept on this store"
    # an explicit filename still hits (precision gate intact)
    assert _titles(okf.find_relevant("explain okf.py", base=base)) == ["src/llm_router/okf.py"]


def test_fp_check_fails_if_ranked_returns_everything(tmp_path, monkeypatch):
    """Positive control: swap in a ranker that returns every gated doc; the same
    comparison must then break, otherwise `ranked <= legacy` proves nothing."""
    base = _fp_store(tmp_path)
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(tmp_path / "repo-fp"))
    prompts = _PROMPTS_233 + _PROMPTS_244
    monkeypatch.setattr(okf, "rank_within_budget", lambda scored, floor, budget, **kw: [c for c, _ in scored])
    legacy, everything = _fp_counts(base, prompts, okf.find_relevant)
    assert everything > legacy, (legacy, everything)


def test_baseline_constant_matches_da31df7_source(tmp_path, monkeypatch):
    """Re-derive the baseline from da31df7's own `find_relevant` (git show -> temp module)."""
    import importlib.util
    import subprocess
    import sys
    repo = Path(__file__).resolve().parents[2]
    proc = subprocess.run(["git", "show", "da31df7:src/llm_router/okf.py"], cwd=repo,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip("da31df7 not reachable in this checkout (shallow clone)")
    src_file = tmp_path / "okf_da31df7.py"
    src_file.write_text(proc.stdout, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("llm_router._okf_da31df7", src_file)
    old = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "llm_router._okf_da31df7", old)
    spec.loader.exec_module(old)
    base = _fp_store(tmp_path / "s")
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(tmp_path / "s" / "repo-fp"))
    total = 0
    for p in _PROMPTS_233 + _PROMPTS_244:
        old.invalidate_cache()
        total += len(old.find_relevant(p, limit=3, base=base))
    assert total == DA31DF7_BASELINE_FP
