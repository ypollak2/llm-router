"""W0-1: every routing decision row carries layer, confidence, reason and inventory_sig.

Evidence behind it (independent audit, 2026-10-25): 2,717 ``routing_decisions`` rows, ``layer`` and
``inventory_sig`` empty on all of them; no caller passed ``inventory_sig``; ``llm-router why`` printed
"n/a (not recorded for hook)". Synthetic data only: each writer path runs for real against a temp
SQLite file or temp ledger; only the provider is stubbed.
"""
from __future__ import annotations

import ast
import builtins
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from llm_router import decision_stamp, engine, resource_map, why
from llm_router.proxy import backends as pb
from llm_router.proxy import ledger as proxy_ledger
from llm_router.proxy.cache_cost import Stickiness
from tests.test_ledger_err1_error_row import _call, _ok, _q, cache_env, caller  # noqa: F401 (fixtures)
from tests.test_proxy_tiers import _req, policy  # noqa: F401 (fixture)

ROOT = Path(__file__).resolve().parent.parent
SIG = "abc123def456"
_COLS = "layer, confidence, reason, inventory_sig, reason_code, classifier_type"


@pytest.fixture(autouse=True)
def _fresh_sig_cache():
    decision_stamp.reset_cache()
    yield
    decision_stamp.reset_cache()


@pytest.fixture
def fixed_sig(monkeypatch):
    monkeypatch.setattr(decision_stamp, "inventory_sig", lambda path=None: SIG)


def _map(tmp_path: Path, statuses: dict[str, str], policy_name: str = "balanced") -> Path:
    rows = [resource_map._row(cid.split("/")[0], "local", cid, status=st, effective_priority=i)
            for i, (cid, st) in enumerate(sorted(statuses.items()))]
    data = {"schema": resource_map.SCHEMA, "generated_at": resource_map._iso(__import__("time").time()),
            "policy": policy_name, "resources": rows}
    return resource_map.write_map(data, tmp_path / "resource_map.json")


# ── inventory_sig: stable, changes with the map, no I/O when warm ───────────────────────────────


def test_inventory_sig_is_stable_and_changes_with_the_inventory(tmp_path):
    p = _map(tmp_path, {"ollama/a": "connected", "codex/b": "absent"})
    first = decision_stamp.inventory_sig(p)
    assert isinstance(first, str) and len(first) == 12
    decision_stamp.reset_cache()
    assert decision_stamp.inventory_sig(p) == first  # same map, fresh cache: same value

    _map(tmp_path, {"ollama/a": "connected", "codex/b": "connected"})  # an inventory change
    decision_stamp.reset_cache()
    changed = decision_stamp.inventory_sig(p)
    assert changed and changed != first


def test_inventory_sig_ignores_quota_and_timestamps(tmp_path):
    p = _map(tmp_path, {"ollama/a": "connected"})
    first = decision_stamp.inventory_sig(p)
    data = json.loads(p.read_text())
    data["generated_at"] = resource_map._iso(__import__("time").time() + 1)
    data["resources"][0]["quota"] = {"used": 0.9}
    resource_map.write_map(data, p)
    decision_stamp.reset_cache()
    assert decision_stamp.inventory_sig(p) == first


def test_inventory_sig_is_none_without_a_usable_map(tmp_path):
    assert decision_stamp.inventory_sig(tmp_path / "missing.json") is None


def test_warm_inventory_sig_does_no_file_io(tmp_path):
    p = _map(tmp_path, {"ollama/a": "connected"})
    cold = decision_stamp.inventory_sig(p)
    calls = MagicMock()
    real_open, real_stat = builtins.open, Path.stat

    def counting_open(*a, **k):
        calls("open", a[:1])
        return real_open(*a, **k)

    def counting_stat(self, *a, **k):
        calls("stat", str(self))
        return real_stat(self, *a, **k)

    with patch("llm_router.map_reader.load_data", side_effect=AssertionError("re-read on a warm cache")), \
         patch.object(builtins, "open", counting_open), patch.object(Path, "stat", counting_stat), \
         patch("os.stat", side_effect=AssertionError("os.stat on a warm cache")):
        for _ in range(50):
            assert decision_stamp.inventory_sig(p) == cold
    calls.assert_not_called()


def test_stamp_helpers_never_invent_a_confidence():
    for layer in ("override", "fallback", "unhinted", "prefix"):
        assert decision_stamp.confidence_for(layer, 0.9) is None
    for bad in (None, 0.0, -1, 1.5, float("nan"), True, "x"):
        assert decision_stamp.confidence_for("L1", bad) is None
    assert decision_stamp.confidence_for("L1", 0.8) == 0.8
    assert decision_stamp.layer_for("prefix:opus") == "prefix"
    assert decision_stamp.layer_for("heuristic+unservable:code") == "heuristic"
    assert decision_stamp.layer_for("") is None and decision_stamp.layer_for(None) is None


# ── writer 1: router.route_and_call (MCP / gateway) ──────────────────────────────────────────────


def _row(db: Path):
    rows = _q(db, f"SELECT {_COLS} FROM routing_decisions ORDER BY id")
    assert len(rows) == 1, rows
    return rows[0]


async def test_router_writer_stamps_all_four_when_the_classifier_provides_them(cache_env, caller, fixed_sig):  # noqa: F811
    cd = {"task_type": "code", "profile": "budget", "classifier_type": "heuristic",
          "classifier_model": "engine", "classifier_confidence": 0.82, "classifier_latency_ms": 1.0,
          "complexity": "moderate", "recommended_model": "ollama/qwen3-coder:30b",
          "base_model": "ollama/qwen3-coder:30b", "layer": "L3"}
    await _call(_ok, classification_data=cd)
    layer, conf, reason, sig, reason_code, _ct = _row(cache_env)
    assert (layer, conf, sig) == ("L3", 0.82, SIG)
    assert reason and reason == reason_code


async def test_router_writer_confidence_is_null_when_the_classifier_has_none(cache_env, caller, fixed_sig):  # noqa: F811
    cd = {"task_type": "code", "profile": "budget", "classifier_type": "fallback", "classifier_model": "none",
          "classifier_confidence": 0.0, "classifier_latency_ms": 0.0, "complexity": "moderate"}
    await _call(_ok, classification_data=cd)
    layer, conf, reason, sig, *_ = _row(cache_env)
    assert (layer, conf, sig) == ("fallback", None, SIG) and reason


async def test_router_writer_unhinted_call_has_layer_unhinted_and_no_confidence(cache_env, caller, fixed_sig):  # noqa: F811
    await _call(_ok)  # no classification_data: the llm() / gateway shape
    layer, conf, reason, sig, _rc, ctype = _row(cache_env)
    assert (layer, conf, sig, ctype) == ("unhinted", None, SIG, "unhinted") and reason


async def test_error_row_writer_stamps_layer_reason_and_inventory(cache_env, caller, fixed_sig):  # noqa: F811
    def boom(model, messages, **kw):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await _call(boom)
    layer, conf, reason, sig, rc, _ = _row(cache_env)
    assert (layer, conf, sig) == ("unhinted", None, SIG)
    assert reason == rc and rc.startswith("error_")


# ── writer 2: hooks.savings_logger.log_direct_to_db (hook, agent-route, SDK) ──────────────────────


def _direct(**kw):
    from llm_router.hooks.direct_executor import DirectResult, ModelSpec
    from llm_router.hooks.savings_logger import log_direct_to_db

    res = DirectResult(text="The capital of France is Paris, a city on the Seine in northern France.", model=ModelSpec(provider="ollama", model="qwen3.5:latest"),
                       latency_ms=10, input_tokens=10, output_tokens=5)
    base = dict(prompt="hello", task_type="query", complexity="simple", classifier_type="heuristic")
    base.update(kw)
    log_direct_to_db(res, **base)


def test_direct_writer_stamps_engine_layer_confidence_reason_and_inventory(temp_db, fixed_sig):
    _direct(layer="L1", confidence=0.91)
    layer, conf, reason, sig, rc, _ = _row(temp_db)
    assert (layer, conf, reason, sig, rc) == ("L1", 0.91, "direct", SIG, "direct")


def test_direct_writer_confidence_null_and_layer_from_label_when_not_given(temp_db, fixed_sig):
    _direct(classifier_type="build-fast-path")
    layer, conf, reason, sig, *_ = _row(temp_db)
    assert (layer, conf, reason, sig) == ("build-fast-path", None, "direct", SIG)


def test_direct_writer_prefix_has_layer_prefix_and_no_confidence(temp_db, fixed_sig):
    _direct(classifier_type="prefix:opus", layer="prefix", confidence=0.99)
    layer, conf, *_ = _row(temp_db)
    assert (layer, conf) == ("prefix", None)


def test_sdk_writer_passes_the_engine_decision_layer(temp_db, fixed_sig, monkeypatch):
    """sdk._route -> log_direct_to_db(layer=Decision.layer, confidence=Decision.confidence)."""
    from llm_router import sdk
    from llm_router.hooks import chain_builder, direct_executor
    from llm_router.hooks.direct_executor import DirectResult, ModelSpec

    d = engine.decide("write a python function that sorts a list", door="sdk")
    monkeypatch.setattr(sdk, "_decide", lambda *a, **k: (d.task_type, d.complexity, d))
    monkeypatch.setattr(chain_builder, "build_chain", lambda *a, **k: [])
    monkeypatch.setattr(chain_builder, "get_current_pressure", lambda: ("green", 0))
    monkeypatch.setattr(chain_builder, "needs_claude_tools", lambda *a, **k: False)
    monkeypatch.setattr(direct_executor, "execute_chain", lambda *a, **k: DirectResult(
        text="ok", model=ModelSpec(provider="ollama", model="qwen3.5:latest"),
        latency_ms=3, input_tokens=4, output_tokens=2))
    sdk._route("write a python function that sorts a list", task_type=None, complexity=None,
               timeout=5, session_id="w01-sdk")
    layer, conf, _r, sig, *_ = _row(temp_db)
    assert layer == d.layer and sig == SIG
    assert conf == (d.confidence if d.confidence and 0 < d.confidence <= 1 else None)


# ── writer 3: the hook's classifier hands the engine layer / confidence to the writer ─────────────


@pytest.fixture(scope="module")
def hook():
    path = ROOT / "src/llm_router/hooks/auto-route.py"
    spec = importlib.util.spec_from_file_location("_w01_hook", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


def test_hook_engine_classification_carries_layer_and_confidence(hook, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DECIDE_CACHE", "memory")
    engine.clear_cache(persistent=False)
    out = hook._classify_prompt_engine("refactor the session store to take an explicit project root")
    assert out is not None and out["layer"] in {"L1", "L3", "cache", "fallback", "prefix"}
    assert "confidence" in out  # a number, or None when the engine has none


@pytest.mark.parametrize("rel", ["src/llm_router/hooks/auto-route.py", "hooks/auto-route.py"])
def test_hook_passes_layer_and_confidence_to_log_direct_to_db(rel):
    tree = ast.parse((ROOT / rel).read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "log_direct_to_db"]
    assert calls, "the hook no longer writes a DIRECT decision row"
    for c in calls:
        assert {"layer", "confidence"} <= {k.arg for k in c.keywords}


# ── writer 4: the proxy ledger ──────────────────────────────────────────────────────────────────


def test_proxy_ledger_row_gets_inventory_sig_and_keeps_cls_fields(tmp_path, fixed_sig):
    path = tmp_path / "proxy_calls.jsonl"
    proxy_ledger.write_row({"ts": 1.0, "cls_layer": "L1", "cls_confidence": 0.7, "cls_reason": "policy"}, path)
    proxy_ledger.write_row({"ts": 2.0}, path)
    a, b = (json.loads(line) for line in path.read_text().splitlines())
    assert (a["cls_layer"], a["cls_confidence"], a["cls_reason"], a["inventory_sig"]) == ("L1", 0.7, "policy", SIG)
    assert (b["cls_layer"], b["cls_confidence"], b["inventory_sig"]) == (None, None, SIG)  # unknown stays NULL


def test_proxy_classifier_reports_rules_layer_without_a_confidence(monkeypatch):
    from llm_router import engine_door

    monkeypatch.setattr(engine_door, "decision", lambda *a, **k: None)
    monkeypatch.setattr(engine_door, "shadow", lambda *a, **k: None)
    task, cx, layer, conf = pb._classify_full("what is the capital of France?")
    assert (layer, conf) == ("rules", None) and (task, cx) == pb._classify_class("what is the capital of France?")


def test_proxy_classifier_reports_the_engine_layer_when_the_engine_decides(monkeypatch):
    from llm_router import engine_door

    d = MagicMock(task_type="code", complexity="moderate", layer="L3", confidence=0.66)
    monkeypatch.setattr(engine_door, "decision", lambda *a, **k: d)
    assert pb._classify_full("x")[2:] == ("L3", 0.66)


async def test_tier_decision_carries_the_classifier_layer_and_confidence(policy):  # noqa: F811
    async def classify(text):
        return {"task_type": "code", "complexity": "moderate", "chain_head": [], "model": None,
                "layer": "L1", "confidence": 0.77}

    d = await policy.decide(_req(), "11111111-2222-3333-4444-555555555555", Stickiness(), classify=classify)
    assert (d.cls_layer, d.cls_confidence) == ("L1", 0.77)


# ── why prints the real layer and confidence for a new row ──────────────────────────────────────


def test_why_prints_the_real_layer_and_confidence_for_a_new_row(cache_env, monkeypatch, fixed_sig):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_ALLOW_STUBS", "1")
    _direct(layer="L3", confidence=0.64)
    (rec,), _notes = why.last_decisions(1)
    text = why.render(rec)
    assert "3 layer:        L3" in text
    assert "0.64" in text
    assert "n/a" not in text.split("2 confidence:")[1].split("3 layer:")[0]
    assert sqlite3.connect(str(cache_env)).execute(
        "SELECT inventory_sig FROM routing_decisions").fetchone() == (SIG,)
