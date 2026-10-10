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


def _stems_store(tmp_path: Path) -> Path:
    proj = tmp_path / "projects" / okf.project_slug(tmp_path / "repo-stems")
    proj.mkdir(parents=True)
    files = {"api": "src/llm_router/api.py", "cli": "src/llm_router/cli.py",
             "base": "src/llm_router/base.py", "gc": "src/llm_router/commands/gc.py",
             "test": "src/llm_router/commands/test.py", "core": "src/llm_router/observability/core.py",
             "state": "src/llm_router/state.py", "okf": "src/llm_router/okf.py"}
    for stem, title in files.items():
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


def test_retrieval_false_positives_not_above_the_fixed_top3_baseline(tmp_path, monkeypatch, capsys):
    """FP = a doc returned for a prompt that is not about this repo.

    Measured on the same store and prompts with the legacy path (`limit=3`, the
    behaviour at da31df7) and the new ranked path; n is printed. The ranked path
    applies the same precision gate first and only changes how many survivors are
    kept, so it can return fewer docs than top-3 and (with a big budget) more, but
    it can never return a doc the gate dropped.
    """
    base = _stems_store(tmp_path)
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(tmp_path / "repo-stems"))
    prompts = _PROMPTS_233 + _PROMPTS_244
    legacy = sum(len(okf.find_relevant(p, limit=3, base=base)) for p in prompts)
    okf.invalidate_cache()
    ranked = sum(len(okf.find_relevant(p, base=base)) for p in prompts)
    with capsys.disabled():
        print(f"\nOKF FP docs over n={len(prompts)} prompts "
              f"(#233={len(_PROMPTS_233)}, #244={len(_PROMPTS_244)}): "
              f"legacy limit=3 -> {legacy}, ranked -> {ranked}")
    assert len(prompts) == 11
    assert ranked <= legacy
    # and the positive control: the set is not vacuous (an explicit filename still hits)
    assert _titles(okf.find_relevant("explain okf.py", base=base)) == ["src/llm_router/okf.py"]
