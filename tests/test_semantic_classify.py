# SPDX-License-Identifier: MIT
"""Tests for the embedding-space prompt classifier (semantic_classify.py).

Covers the load-bearing invariants:
  * abstain-safe when no centroid artifact is present (the ship-default);
  * confident classification against a tiny synthetic artifact;
  * floor abstention when the softmax mass is split;
  * embedding-space guard (artifact model must match the backend);
  * build_prototype normalization + the ClassifySignal bridge.

No network: the embedding backend is monkeypatched. The synthetic artifact uses
4-dim vectors so the geometry is easy to reason about.
"""

from __future__ import annotations

import json
import math

import pytest

from llm_router import semantic_classify as sc
from llm_router.types import Subject, TaskType


def _write_artifact(tmp_path, *, temperature=12.0, floor=0.55, model="nomic-embed-text"):
    art = {
        "embedding_model": model,
        "dim": 4,
        "temperature": temperature,
        "confidence_floor": floor,
        "task_type": {
            "code": [[1.0, 0.0, 0.0, 0.0]],
            "query": [[0.0, 1.0, 0.0, 0.0]],
        },
        "subject": {
            "code": [[1.0, 0.0, 0.0, 0.0]],
            "math": [[0.0, 1.0, 0.0, 0.0]],
        },
        "provenance": {"sources": ["synthetic:test"]},
    }
    p = tmp_path / "semantic_centroids.json"
    p.write_text(json.dumps(art), encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _clear_cache():
    """Artifacts are lru-cached by path; clear around every test."""
    sc._load_centroids.cache_clear()
    yield
    sc._load_centroids.cache_clear()


def _point_at(monkeypatch, path):
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_CENTROIDS", str(path))


def _fake_embed(monkeypatch, vec):
    monkeypatch.setattr(sc, "_embed", lambda text, expected_model: list(vec))


# ── abstain-safety ────────────────────────────────────────────────────────────


async def test_abstains_when_no_artifact(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path / "does_not_exist.json")
    assert sc.is_available() is False
    assert await sc.classify_semantic("write a function to sort a list") is None


async def test_abstains_when_backend_unreachable(monkeypatch, tmp_path):
    _point_at(monkeypatch, _write_artifact(tmp_path))
    monkeypatch.setattr(sc, "_embed", lambda text, expected_model: None)  # backend down
    assert sc.is_available() is True  # artifact loaded…
    assert await sc.classify_semantic("anything") is None  # …but embedding failed


# ── confident classification ──────────────────────────────────────────────────


async def test_confident_code_classification(monkeypatch, tmp_path):
    _point_at(monkeypatch, _write_artifact(tmp_path))
    _fake_embed(monkeypatch, [1.0, 0.0, 0.0, 0.0])  # dead-on the code prototype
    res = await sc.classify_semantic("refactor the auth handler")
    assert res is not None
    assert res.task_type is TaskType.CODE
    assert res.subject is Subject.CODE
    assert res.task_confidence > 0.9
    assert res.method == "embedding"


async def test_confident_query_classification(monkeypatch, tmp_path):
    _point_at(monkeypatch, _write_artifact(tmp_path))
    _fake_embed(monkeypatch, [0.0, 1.0, 0.0, 0.0])
    res = await sc.classify_semantic("what is a foreign key")
    assert res is not None
    assert res.task_type is TaskType.QUERY


# ── floor abstention ──────────────────────────────────────────────────────────


async def test_abstains_when_confidence_below_floor(monkeypatch, tmp_path):
    _point_at(monkeypatch, _write_artifact(tmp_path, floor=0.55))
    # Equal cosine to both prototypes ⇒ softmax mass ≈ 0.5 each ⇒ below floor.
    _fake_embed(monkeypatch, [1.0, 1.0, 0.0, 0.0])
    assert await sc.classify_semantic("ambiguous prompt") is None


async def test_floor_override_forces_result(monkeypatch, tmp_path):
    _point_at(monkeypatch, _write_artifact(tmp_path, floor=0.55))
    _fake_embed(monkeypatch, [1.0, 1.0, 0.0, 0.0])
    # A permissive override accepts the (weak) top pick rather than abstaining.
    res = await sc.classify_semantic("ambiguous prompt", confidence_floor=0.4)
    assert res is not None
    assert res.task_type in (TaskType.CODE, TaskType.QUERY)


# ── embedding-space guard ─────────────────────────────────────────────────────


async def test_dim_mismatch_abstains(monkeypatch, tmp_path):
    _point_at(monkeypatch, _write_artifact(tmp_path))
    _fake_embed(monkeypatch, [1.0, 0.0, 0.0])  # 3-dim vs artifact dim 4
    assert await sc.classify_semantic("x") is None


def test_embed_refuses_mismatched_model_on_ollama(monkeypatch):
    # Default (ollama) backend serves nomic-embed-text; an artifact built with a
    # different model must abstain rather than silently mix embedding spaces.
    monkeypatch.delenv("LLM_ROUTER_SEMANTIC_CLASSIFIER_BACKEND", raising=False)
    assert sc._embed("hello", expected_model="some-other-model") is None


# ── helpers ───────────────────────────────────────────────────────────────────


def test_build_prototype_is_unit_norm():
    proto = sc.build_prototype([[3.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]])
    mag = math.sqrt(sum(x * x for x in proto))
    assert mag == pytest.approx(1.0)


def test_build_prototype_rejects_empty():
    with pytest.raises(ValueError):
        sc.build_prototype([])


async def test_to_signal_bridges_to_classifysignal(monkeypatch, tmp_path):
    from llm_router.classify import ClassifySignal
    from llm_router.types import Complexity

    _point_at(monkeypatch, _write_artifact(tmp_path))
    _fake_embed(monkeypatch, [1.0, 0.0, 0.0, 0.0])
    res = await sc.classify_semantic("build a CLI")
    sig = res.to_signal(Complexity.MODERATE)
    assert isinstance(sig, ClassifySignal)
    assert sig.task_type is TaskType.CODE
    assert sig.complexity is Complexity.MODERATE
    assert sig.confident is True
    assert sig.method == "embedding"


def test_unknown_labels_dropped(monkeypatch, tmp_path):
    art = {
        "embedding_model": "nomic-embed-text",
        "dim": 4,
        "task_type": {"code": [[1.0, 0, 0, 0]], "nonsense": [[0, 1.0, 0, 0]]},
        "subject": {},
    }
    p = tmp_path / "semantic_centroids.json"
    p.write_text(json.dumps(art), encoding="utf-8")
    _point_at(monkeypatch, p)
    cents = sc._load_centroids(str(p))
    assert "code" in cents.task
    assert "nonsense" not in cents.task  # invalid enum label dropped, not fatal
