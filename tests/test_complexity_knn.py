"""complexity_knn: the learned 0-1 "needs frontier" score (Phase 2.1).

The module must be a pure no-op without its artifact, abstain on every
embedding failure, reproduce the kappa-shrinkage formula exactly, and only
move a routing decision when explicitly enabled.
"""
from __future__ import annotations

import asyncio
import math

import pytest

from llm_router import complexity_knn as ck
from llm_router.types import Complexity


def _unit(*xs: float) -> list[float]:
    n = math.sqrt(sum(x * x for x in xs))
    return [x / n for x in xs]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    for var in ("LLM_ROUTER_COMPLEXITY_KNN", "LLM_ROUTER_COMPLEXITY_KNN_ARTIFACT",
                "LLM_ROUTER_COMPLEXITY_KNN_THRESHOLD"):
        monkeypatch.delenv(var, raising=False)
    ck.clear_cache()
    yield
    ck.clear_cache()


def _artifact(tmp_path, vecs, labels, **kw):
    path = tmp_path / "knn.json"
    ck.write_artifact(path, vecs, labels, embedding_model=kw.pop("embedding_model", "nomic-embed-text"),
                      **kw)
    return path


def test_formula_matches_the_routerarena_shrinkage_by_hand():
    vecs = [_unit(1, 0), _unit(0, 1), _unit(1, 1)]
    labels = [1.0, 0.0, 1.0]
    art = ck.Artifact.from_parts(vecs, labels, embedding_model="m", k=2, kappa=3.0,
                                 temperature=0.1, threshold=0.5, prior=0.25)
    q = _unit(1, 0.1)
    sims = sorted(((sum(a * b for a, b in zip(q, v)), y) for v, y in zip(vecs, labels)), reverse=True)[:2]
    mx = sims[0][0]
    w = [math.exp((s - mx) / 0.1) for s, _ in sims]
    num = sum(wi * y for wi, (_, y) in zip(w, sims))
    expect = (num + 3.0 * 0.25) / (sum(w) + 3.0)
    got = ck.score_vector(q, art)
    assert got.score == pytest.approx(expect, abs=1e-6)
    assert got.evidence == pytest.approx(sum(w) / (sum(w) + 3.0), abs=1e-6)
    assert got.needs_frontier is (expect >= 0.5)


def test_unnormalised_weights_make_kappa_a_neighbour_count():
    vecs = [_unit(1, 0, 0)] * 3
    art = ck.Artifact.from_parts(vecs, [1.0, 1.0, 1.0], embedding_model="m", k=3, kappa=3.0,
                                 temperature=0.1, threshold=0.5, prior=0.1)
    near = ck.score_vector(_unit(1, 0, 0), art)
    assert near.score > 0.5  # three agreeing neighbours at full weight: (3 + 0.3) / 6
    # every neighbour is equally far, so weights stay 1 each; kappa still pulls to prior
    far = ck.score_vector(_unit(0, 1, 0), art)
    assert far.score == pytest.approx((3 + 0.3) / 6)


def test_artifact_round_trips_and_threshold_env_overrides(tmp_path, monkeypatch):
    path = _artifact(tmp_path, [_unit(1, 0), _unit(0, 1)], [1.0, 0.0], threshold=0.4)
    monkeypatch.setenv("LLM_ROUTER_COMPLEXITY_KNN_ARTIFACT", str(path))
    art = ck.load_artifact()
    assert art is not None and art.n == 2 and art.dim == 2 and art.threshold == 0.4
    assert art.prior == pytest.approx(0.5)
    monkeypatch.setenv("LLM_ROUTER_COMPLEXITY_KNN_THRESHOLD", "0.9")
    assert ck.threshold(art) == 0.9
    monkeypatch.setenv("LLM_ROUTER_COMPLEXITY_KNN_THRESHOLD", "not-a-number")
    assert ck.threshold(art) == 0.4


def test_no_artifact_is_a_pure_no_op(monkeypatch):
    called = []
    monkeypatch.setattr(ck, "_embed", lambda text, model: called.append(text) or [1.0, 0.0])
    assert ck.load_artifact() is None
    assert ck.is_available() is False
    assert asyncio.run(ck.complexity_score("anything")) is None
    assert called == []  # no embedding call without an artifact


def test_embedding_failure_or_dim_mismatch_abstains(tmp_path, monkeypatch):
    path = _artifact(tmp_path, [_unit(1, 0), _unit(0, 1)], [1.0, 0.0])
    monkeypatch.setenv("LLM_ROUTER_COMPLEXITY_KNN_ARTIFACT", str(path))
    monkeypatch.setattr(ck, "_embed", lambda text, model: None)
    assert asyncio.run(ck.complexity_score("x")) is None
    monkeypatch.setattr(ck, "_embed", lambda text, model: [1.0, 0.0, 0.0])
    assert asyncio.run(ck.complexity_score("x")) is None
    monkeypatch.setattr(ck, "_embed", lambda text, model: [2.0, 0.0])
    s = asyncio.run(ck.complexity_score("x"))
    assert s is not None and 0.0 <= s.score <= 1.0


def test_corrupt_artifact_abstains(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv("LLM_ROUTER_COMPLEXITY_KNN_ARTIFACT", str(bad))
    assert ck.load_artifact() is None


def test_disabled_by_default_and_env_switch(monkeypatch):
    assert ck.enabled() is False
    monkeypatch.setenv("LLM_ROUTER_COMPLEXITY_KNN", "on")
    assert ck.enabled() is True


@pytest.mark.parametrize("cx,needs,expect", [
    ("simple", True, "complex"),
    ("moderate", True, "complex"),
    ("complex", True, "complex"),
    ("deep_reasoning", True, "deep_reasoning"),
    ("simple", False, "simple"),
    ("moderate", False, "moderate"),
    ("complex", False, "moderate"),
    ("deep_reasoning", False, "moderate"),
])
def test_frontier_complexity_moves_only_across_the_frontier_boundary(cx, needs, expect):
    assert ck.frontier_complexity(cx, needs) == expect
    assert ck.frontier_complexity(Complexity(cx), needs) == Complexity(expect)


def test_auc_and_bootstrap_helpers():
    assert ck.auc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]) == 1.0
    assert ck.auc([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]) == 0.0
    assert ck.auc([0.5, 0.5, 0.5, 0.5], [0, 1, 0, 1]) == 0.5
