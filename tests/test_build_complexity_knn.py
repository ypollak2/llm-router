"""scripts/build_complexity_knn.py: label joins, splits and the pure-python scorer."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from llm_router import complexity_knn as ck

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_complexity_knn.py"
_spec = importlib.util.spec_from_file_location("build_complexity_knn", _PATH)
bk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bk)


def _h(t):
    return hashlib.sha256(t.encode()).hexdigest()[:16]


def _jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_public_label_is_cheap_wrong_and_strong_right(tmp_path):
    prompts = ["p-both-right", "p-escalation-pays", "p-both-wrong", "p-cheap-only", "p-missing-strong"]
    _jsonl(tmp_path / "corpus" / "s.jsonl", [{"prompt": p, "source": "s"} for p in prompts])
    outcome = {"p-both-right": (1, 1), "p-escalation-pays": (0, 1), "p-both-wrong": (0, 0),
               "p-cheap-only": (1, 0)}
    rows = []
    for p, (c, s) in outcome.items():
        rows += [{"prompt_hash": _h(p), "model": "cheap", "correct": c},
                 {"prompt_hash": _h(p), "model": "strong", "correct": s}]
    rows += [{"prompt_hash": _h("p-missing-strong"), "model": "cheap", "correct": 0},
             {"prompt_hash": _h("p-both-right"), "model": "other", "correct": 0},
             {"prompt_hash": _h("p-cheap-only"), "model": "strong", "correct": 1, "error": "timeout"}]
    _jsonl(tmp_path / "outcomes" / "o.jsonl", rows)
    got = {it["text"]: it["y"] for it in bk.public_items(tmp_path, "cheap", "strong")}
    assert got == {"p-both-right": 0, "p-escalation-pays": 1, "p-both-wrong": 0, "p-cheap-only": 0}


def test_groundtruth_reads_only_labelled_tune_rows_and_never_the_test_split(tmp_path):
    _jsonl(tmp_path / "tune.jsonl", [{"task_id": "a", "prompt": "A"}, {"task_id": "b", "prompt": "B"},
                                     {"task_id": "c", "prompt": "C"}])
    (tmp_path / "test.jsonl").write_text("SENTINEL: must never be read\n")
    _jsonl(tmp_path / "labels.tune.jsonl", [
        {"task_id": "a", "status": "labelled", "cheapest_acceptable_model": "premium"},
        {"task_id": "b", "status": "labelled", "cheapest_acceptable_model": "local"},
        {"task_id": "c", "label_status": "ambiguous", "cheapest_acceptable_model": None}])
    got = {it["text"]: it["y"] for it in bk.groundtruth_items(tmp_path)}
    assert got == {"A": 1, "B": 0}


def test_split_is_stratified_disjoint_and_deterministic():
    items = [{"source": s, "y": y} for s in "ab" for y in (0, 1) for _ in range(10)]
    dev, test = bk.stratified_split(items, 0.2)
    assert not set(dev) & set(test) and len(dev) + len(test) == 40
    assert sorted((items[i]["source"], items[i]["y"]) for i in test).count(("a", 1)) == 2
    assert bk.stratified_split(items, 0.2) == (dev, test)


def test_pure_python_batch_scores_match_the_runtime_scorer():
    x = [[1.0, 0.0], [0.0, 1.0], [0.7, 0.7], [0.2, 0.9]]
    y = [1, 0, 1, 0]
    q = [[0.9, 0.1], [0.1, 0.9]]
    scores, evid, _ = bk.knn_scores(q, x, y, k=3)
    art = ck.Artifact.from_parts(x, y, embedding_model="m", k=3)
    for qi, s, e in zip(q, scores, evid):
        r = ck.score_vector(qi, art)
        assert s == pytest.approx(r.score) and e == pytest.approx(r.evidence)


def test_balanced_accuracy_and_threshold():
    assert bk.balanced_accuracy([True, False, True, False], [1, 0, 0, 1]) == 0.5
    assert bk.best_threshold([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]) == 0.8
