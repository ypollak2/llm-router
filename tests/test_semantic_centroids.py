# SPDX-License-Identifier: MIT
"""Tests for the centroid generator, incl. an end-to-end round-trip through the
generator into the runtime classifier — proving the two agree on schema+geometry.
"""

from __future__ import annotations

import json

import pytest

from llm_router import semantic_classify as sc
from llm_router.semantic_centroids import (
    LabeledPrompt,
    build_artifact,
    calibrate_floor,
    calibrate_temperature,
)
from llm_router.types import TaskType


# A deterministic fake embedder: separable 4-dim points by keyword. No network.
def _fake_embed(text: str):
    t = text.lower()
    if "code" in t or "function" in t or "refactor" in t:
        return [1.0, 0.0, 0.0, 0.0]
    if "?" in t or "what" in t or "define" in t:
        return [0.0, 1.0, 0.0, 0.0]
    if "research" in t or "latest" in t:
        return [0.0, 0.0, 1.0, 0.0]
    return [0.0, 0.0, 0.0, 1.0]


_CORPUS = [
    LabeledPrompt("refactor the code", "code", "code"),
    LabeledPrompt("write a function", "code", "code"),
    LabeledPrompt("what is a pointer?", "query", "general"),
    LabeledPrompt("define recursion", "query", "general"),
    LabeledPrompt("research the latest news", "research", "general"),
]


def test_build_artifact_schema():
    art = build_artifact(_CORPUS, _fake_embed, embedding_model="fake", k=1)
    assert art["dim"] == 4
    assert set(art["task_type"]) == {"code", "query", "research"}
    assert "code" in art["subject"]
    # every prototype is a unit vector
    for protos in art["task_type"].values():
        for p in protos:
            assert sum(x * x for x in p) == pytest.approx(1.0)


def test_calibration_runs():
    art = build_artifact(_CORPUS, _fake_embed, embedding_model="fake", k=1)
    holdout = [(sc._norm(_fake_embed(ex.prompt)), ex.task_type) for ex in _CORPUS]
    t = calibrate_temperature(holdout, art["task_type"])
    assert t > 0
    floor = calibrate_floor(holdout, art["task_type"], t)
    assert 0.0 <= floor <= 1.0


async def test_roundtrip_generator_to_classifier(monkeypatch, tmp_path):
    """Build an artifact with the generator, then classify through the runtime
    module using the same fake embedder — they must agree."""
    art = build_artifact(
        _CORPUS, _fake_embed, embedding_model="fake", k=1,
        holdout=list(_CORPUS),
    )
    path = tmp_path / "semantic_centroids.json"
    path.write_text(json.dumps(art), encoding="utf-8")

    sc._load_centroids.cache_clear()
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_CENTROIDS", str(path))
    # Runtime uses the same embedding geometry as the generator.
    monkeypatch.setattr(sc, "_embed", lambda text, expected_model: _fake_embed(text))

    res = await sc.classify_semantic("please refactor the code", confidence_floor=0.4)
    assert res is not None
    assert res.task_type is TaskType.CODE

    res2 = await sc.classify_semantic("what is a hash map?", confidence_floor=0.4)
    assert res2 is not None
    assert res2.task_type is TaskType.QUERY
    sc._load_centroids.cache_clear()


def test_kmeans_multi_prototype():
    # k=2 on a class with two distinct clusters yields two prototypes.
    corpus = [
        LabeledPrompt("code one", "code"),
        LabeledPrompt("code two", "code"),
        LabeledPrompt("research a", "research"),
        LabeledPrompt("research b", "research"),
    ]

    def two_cluster_embed(text: str):
        # split "code" into two orthogonal sub-clusters
        if text == "code one":
            return [1.0, 0.0, 0.0, 0.0]
        if text == "code two":
            return [0.0, 1.0, 0.0, 0.0]
        return [0.0, 0.0, 1.0, 0.0]

    art = build_artifact(corpus, two_cluster_embed, embedding_model="fake", k=2)
    assert len(art["task_type"]["code"]) == 2
