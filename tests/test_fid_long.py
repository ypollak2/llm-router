"""fid_long_120 (scripts/release/fid_long_gen.py, run_fid_long.py): REPORT-ONLY long-session set.

Model-free and offline. The full-set hash runs in child processes (PYTHONHASHSEED 0 vs 1).
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
RELEASE = REPO / "scripts" / "release"
sys.path.insert(0, str(RELEASE))

import fid_long_gen as gen  # noqa: E402
import run_fid_long as rfl  # noqa: E402

SUBSET = [i * gen.PER_CATEGORY for i in range(len(gen.CATEGORIES))]  # one item per category


def _hash_child(seed: str) -> str:
    env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(REPO / "src"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    out = subprocess.run([sys.executable, str(RELEASE / "fid_long_gen.py"), "--hash"], env=env,
                         capture_output=True, text=True, check=True, timeout=300)
    return out.stdout.strip()


@pytest.fixture(scope="module")
def full_set():
    return gen.items()


def test_hash_stable_across_runs_and_hashseeds():
    h0, h0b, h1 = _hash_child("0"), _hash_child("0"), _hash_child("1")
    assert len(h0) == 64
    assert h0 == h0b == h1


def test_set_shape_and_threshold(full_set):
    g1 = gen.load_g1()
    assert len(full_set) == gen.N_ITEMS == 120
    assert [it.item_id for it in full_set] == [f"fidl-{i}" for i in range(120)]
    for cat in gen.CATEGORIES:
        assert sum(it.category == cat for it in full_set) == 20
    assert len({it.token for it in full_set}) == 120
    for it in full_set:
        full = g1.full_context(it)
        assert len(full) // 4 >= gen.MIN_WHOLE_TOKENS
        assert full.count(it.token) == 1


def test_late_decoy_differs_from_token(full_set):
    for it in full_set:
        text = json.dumps(it.entries)
        assert text.count("An older run used") == 1
        decoy = text.split("An older run used ")[1].split(".)")[0]
        assert decoy != it.token and it.token not in decoy


def test_report_shape_and_controls_retained_in_pack():
    doc = rfl.report(SUBSET)
    assert doc["n_items"] == 6 and "REPORT-ONLY" in doc["label"]
    assert set(doc["windows"]) == {"65536", "32768"}
    for w, d in doc["windows"].items():
        assert set(d["by_category"]) == set(gen.CATEGORIES)
        for cat, c in d["by_category"].items():
            assert set(c) == {"n", "worse", "better", "both", "neither", "pack_mode"}
            assert c["n"] == 1 and c["pack_mode"] == 1
            assert c["worse"] + c["better"] + c["both"] + c["neither"] == 1
            if cat in gen.CONTROLS:
                assert c["both"] == 1, f"control {cat} not retained in PACK at {w}"
    assert json.dumps(doc)  # serialisable, counts only
    assert "token" not in json.dumps(doc["windows"])


def test_aborts_and_writes_nothing_when_an_item_is_not_pack(tmp_path, monkeypatch):
    g1 = gen.load_g1()
    real = g1.pack_context
    monkeypatch.setattr(g1, "pack_context", lambda it, path, prov: (real(it, path, prov)[0], "full"))
    out = tmp_path / "x.json"
    assert rfl.main(["--indices", "0", "--out", str(out)]) == 1
    assert not out.exists()


def test_main_writes_counts_only_json(tmp_path):
    out = tmp_path / "sub" / "r.json"
    assert rfl.main(["--indices", ",".join(map(str, SUBSET)), "--out", str(out)]) == 0
    doc = json.loads(out.read_text())
    assert doc["n_items"] == 6 and doc["windows"]["65536"]["total"]["n"] == 6
