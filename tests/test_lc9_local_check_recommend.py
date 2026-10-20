"""LC-9: recommendations data file + hardware-fit selection."""
from __future__ import annotations

import ast
import json
import re
from datetime import date
from pathlib import Path

from llm_router.local_check import recommend as R

D = R.load()
SRC = Path(R.__file__)


def mac(total, free=None):
    """Unified-memory Mac; ``free`` is deliberately independent of capacity (a busy machine)."""
    return {"gpu": "apple_unified", "vram_gb": total, "total_gb": total,
            "free_gb": total if free is None else free, "usable_gb": 0.0 if free is None else free}


# ---- data file
def test_schema_and_required_fields():
    assert D["schema_version"] == R.SCHEMA_VERSION and D["bank_version"] == 1
    date.fromisoformat(D["generated"])
    assert 1 <= len(D["entries"]) <= 8
    for e in D["entries"]:
        for k in ("family", "ids", "min_ram_gb", "min_vram_gb", "download_size_gb", "licence", "bench", "status", "sources"):
            assert k in e, (e.get("family"), k)
        assert any(e["ids"].get(k) for k in ("ollama", "lmstudio", "hf_repo"))
        assert e["status"] in ("measured", "untested")
        assert e["licence"].strip()
        assert e["sources"]["size"] and e["sources"]["licence"]


def test_measured_claims_have_source_and_untested_have_none():
    measured = [e for e in D["entries"] if e["status"] == "measured"]
    assert measured, "an empty set passes everything"
    for e in D["entries"]:
        b = e["bench"]
        if e["status"] == "untested":
            assert b is None
            continue
        assert b["source"].strip() and b["date"] and b["listed_date"] and b["hardware_class"] and b["bank_version"]
        lanes = b["lanes"]
        assert sum(v["k"] for v in lanes.values()) == b["pooled"]["k"]
        assert sum(v["n"] for v in lanes.values()) == b["pooled"]["n"]


def test_q37_values_match_the_recorded_result():
    e = next(x for x in D["entries"] if x["status"] == "measured")
    assert e["bench"]["lanes"] == {"commit_message": {"k": 32, "n": 35}, "pr_description": {"k": 28, "n": 35}}
    assert e["bench"]["pooled"] == {"k": 60, "n": 70}
    assert "pre-bank" in e["bench"]["bank_version"]


def test_untested_sources_are_urls():
    for e in D["entries"]:
        if e["status"] == "untested":
            assert re.match(r"https://", e["sources"]["size"])


def test_json_is_packaged_next_to_the_module():
    assert R.DATA_FILE.is_file() and R.DATA_FILE.parent.name == "data"
    toml = (SRC.parents[3] / "pyproject.toml").read_text()
    assert not re.search(r"(?m)^\s*\"[^\"]*local_check[^\"]*\"", toml)  # nothing excludes the data dir


# ---- no model names in code
def test_no_model_name_literal_in_recommend_py():
    names = set()
    for e in D["entries"]:
        names |= {v for v in e["ids"].values() if v}
        names |= {e["family"], e["ids"]["ollama"].split(":")[0] if e["ids"].get("ollama") else ""}
    names.discard("")
    for node in ast.walk(ast.parse(SRC.read_text())):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            low = node.value.lower()
            for n in names:
                assert n.lower() not in low, (n, node.value)
    assert not re.search(r"qwen|llama|gemma|mistral|phi|deepseek", SRC.read_text().replace("ollama", "").replace("llamacpp", ""), re.I)


# ---- selection
def test_big_machine_ranks_measured_first_then_untested_by_size():
    r = R.recommend(mac(64), "ollama", None, None, today=date(2026, 10, 21))
    assert r[0].status == "measured" and r[0].pooled_rate == 60 / 70
    rest = [x.need_gb for x in r[1:]]
    assert rest == sorted(rest) and all(x.status == "untested" for x in r[1:])
    assert r[0].pull_command == f"ollama pull {r[0].model_id}"


def test_busy_machine_low_free_but_enough_total_still_fits():
    # 48 GiB Mac with 2 GB free right now: capacity 48 - 9.6 = 38.4 >= 28.4 (the review's case)
    r = R.recommend(mac(48, free=2.0), "ollama", None, None, today=date(2026, 10, 21))
    assert r and r[0].status == "measured" and r[0].fit == "ok"
    assert R.capacity_gb(mac(48, free=2.0)) == 38.4


def test_small_machine_filters_out_big_models():
    # 12 GiB: capacity 12 - 4 = 8.0
    r = R.recommend(mac(12), "ollama", None, None, today=date(2026, 10, 21))
    assert r and all(x.need_gb <= 8.0 for x in r) and not any(x.status == "measured" for x in r)
    assert R.recommend(mac(4), "ollama") == []  # capacity 0: nothing fits, and it is not "unknown"


def test_capacity_reserve_is_max_4_or_20_percent():
    assert R.capacity_gb({"total_gb": 8}) == 4.0
    assert R.capacity_gb({"total_gb": 16}) == 12.0   # 4 GiB floor
    assert R.capacity_gb({"total_gb": 48}) == 38.4   # 20%


def test_headroom_and_kv_boundary():
    e = next(x for x in D["entries"] if x["status"] == "measured")
    need = R.need_gb(e)
    assert need == round(22 * 1.2 + 2.0, 2)
    total = need / 0.8
    assert R.capacity_gb(mac(total)) == need
    assert R.recommend(mac(total), "ollama")[0].status == "measured"
    assert not any(x.status == "measured" for x in R.recommend(mac(total - 0.1), "ollama"))


def test_empty_reason_text():
    assert R.empty_reason("lmstudio") == "no verified recommendation for lmstudio"
    assert R.recommend(mac(99), "lmstudio") == []


def test_runtime_without_ids_is_empty_and_pull_is_runtime_specific():
    assert R.recommend(mac(99), "lmstudio") == []
    assert R.recommend(mac(99), "vllm") == []


def test_unknown_hardware_is_kept_and_marked_not_zero():
    for hw in ({"usable_gb": None, "gpu": None}, {"total_gb": None}, {"usable_gb": 50.0}, None):
        r = R.recommend(hw, "ollama")
        assert r and all(x.fit == "unknown" for x in r), hw
    assert len(r) == len([e for e in D["entries"] if e["ids"].get("ollama")])


def test_discrete_gpu_uses_vram_when_entry_gives_min_vram():
    d = json.loads(json.dumps(D))
    for e in d["entries"]:
        e["min_vram_gb"] = e["min_ram_gb"]
    r = R.recommend({"gpu": "nvidia", "vram_gb": 12.0, "total_gb": 16.0}, "ollama", data=d)
    assert r and all(x.need_gb <= 12 for x in r) and not any(x.status == "measured" for x in r)


def test_discrete_gpu_without_min_vram_falls_back_to_ram_rule():
    hw = {"gpu": "nvidia", "vram_gb": 8.0, "total_gb": 64.0}  # capacity 51.2
    assert R.recommend(hw, "ollama")[0].status == "measured"
    assert all(e["min_vram_gb"] is None for e in D["entries"])  # no model card gives a VRAM figure (see docstring)


def test_current_model_excluded_and_pass_returns_nothing():
    tag = D["entries"][0]["ids"]["ollama"]
    assert tag not in [x.model_id for x in R.recommend(mac(99), "ollama", tag)]
    assert R.recommend(mac(99), "ollama", tag, {"verdict": "PASS"}) == []
    assert R.recommend(mac(99), "ollama", tag, {"verdict": "FAIL"})


def test_staleness_note():
    assert R.recommend(mac(99), "ollama", today=date(2026, 12, 1))[0].note is None
    n = R.recommend(mac(99), "ollama", today=date(2027, 2, 1))[0].note
    assert n == "list from 2026-10-20; newer models may exist"


def test_recommend_never_executes_anything(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    assert R.recommend(mac(99), "ollama")
