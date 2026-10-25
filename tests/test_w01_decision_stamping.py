"""W0-1: every routing decision row carries layer, confidence, reason and inventory_sig.

Evidence behind it (independent audit, 2026-10-25): 2,717 ``routing_decisions`` rows, ``layer`` and
``inventory_sig`` empty on all of them; no caller passed ``inventory_sig``; ``llm-router why`` printed
"n/a (not recorded for hook)". Synthetic data only: each writer path runs for real against a temp
SQLite file or temp ledger; only the provider is stubbed.
"""
from __future__ import annotations

import ast
import builtins
import os
import stat
import time
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


def test_warm_inventory_sig_reads_and_opens_no_file(tmp_path):
    """A warm call is one ``stat`` (the map token): no read, no open, no database."""
    p = _map(tmp_path, {"ollama/a": "connected"})
    cold = decision_stamp.inventory_sig(p)
    calls = MagicMock()
    real_open = builtins.open

    def counting_open(*a, **k):
        calls("open", a[:1])
        return real_open(*a, **k)

    with patch("llm_router.map_reader.load_data", side_effect=AssertionError("re-read on a warm cache")), \
         patch("llm_router.state_files.connect_sqlite", side_effect=AssertionError("db on a warm cache")), \
         patch.object(builtins, "open", counting_open):
        for _ in range(50):
            assert decision_stamp.inventory_sig(p) == cold
    calls.assert_not_called()


def test_a_map_rewrite_gives_the_new_sig_at_once_without_waiting_for_a_ttl(tmp_path):
    """The cache is keyed on map_reader.token(), not a clock: no reset_cache() between the two calls."""
    p = _map(tmp_path, {"ollama/a": "connected", "codex/b": "connected"})
    before = decision_stamp.inventory_sig(p)
    old = p.stat().st_mtime_ns
    _map(tmp_path, {"ollama/a": "connected", "codex/b": "absent"})  # model b goes absent
    os.utime(p, ns=(old + 5_000_000, old + 5_000_000))  # a coarse-mtime filesystem still moves the token
    after = decision_stamp.inventory_sig(p)
    assert after and after != before


def _caps_map(tmp_path: Path, caps, window=None) -> Path:
    row = resource_map._row("ollama", "local", "ollama/a", status="connected", effective_priority=1)
    row["capabilities_reported"] = True
    row["capabilities"] = caps
    if window is not None:
        row["limits"] = {"max_context": window}
    data = {"schema": resource_map.SCHEMA, "generated_at": resource_map._iso(time.time()),
            "policy": "balanced", "resources": [row]}
    return resource_map.write_map(data, tmp_path / "resource_map.json")


def test_a_capability_or_window_change_changes_the_sig(tmp_path):
    sigs = []
    for caps, window in ((["completion", "tools"], 8192), (["completion"], 8192),
                         (["completion", "tools", "vision"], 8192), (["completion", "tools"], 32768)):
        p = _caps_map(tmp_path, caps, window)
        decision_stamp.reset_cache()
        sigs.append(decision_stamp.inventory_sig(p))
    assert all(sigs) and len(set(sigs)) == 4, sigs


def test_a_kind_change_changes_the_sig(tmp_path):
    sigs = []
    for kind in ("local", "subscription", "api"):
        p = _caps_map(tmp_path, ["completion", "tools"], 8192)
        data = json.loads(p.read_text())
        data["resources"][0]["kind"] = kind
        p.write_text(json.dumps(data))
        decision_stamp.reset_cache()
        sigs.append(decision_stamp.inventory_sig(p, db_path=tmp_path / "usage.db"))
    assert all(sigs) and len(set(sigs)) == 3, sigs
    snap = decision_stamp.resolve_inventory(sigs[1], db_path=tmp_path / "usage.db")
    assert snap["rows"][0]["kind"] == "subscription"


@pytest.mark.timing
def test_a_locked_db_costs_under_300ms_and_the_snapshot_is_retried_after_unlock(tmp_path):
    from llm_router import failopen

    db = tmp_path / "usage.db"
    sqlite3.connect(db).close()
    p = _caps_map(tmp_path, ["completion", "tools"], 8192)
    decision_stamp.reset_cache()
    seen = []
    real = failopen.record
    with patch.object(failopen, "record", lambda code, exc=None, *a, **k: (seen.append(code), real(code, exc, *a, **k))[1]):
        locker = sqlite3.connect(db, isolation_level=None)
        locker.execute("BEGIN EXCLUSIVE")
        try:
            t0 = time.monotonic()
            sig = decision_stamp.inventory_sig(p, db_path=db)
            elapsed = time.monotonic() - t0
        finally:
            locker.execute("ROLLBACK")
            locker.close()
    assert sig and elapsed < 0.3, elapsed
    assert "W01-SNAPSHOT" in seen
    assert decision_stamp.resolve_inventory(sig, db_path=db) is None  # not written while locked
    assert decision_stamp.inventory_sig(p, db_path=db) == sig  # same process, not cached: retried
    assert decision_stamp.resolve_inventory(sig, db_path=db) is not None


# ── inventory_snapshots: a sig resolves to the inventory it hashed ───────────────────────────────


def test_an_old_sig_still_resolves_after_the_map_changed(tmp_path):
    db = tmp_path / "usage.db"
    p = _caps_map(tmp_path, ["completion", "tools"], 8192)
    old_sig = decision_stamp.inventory_sig(p, db_path=db)
    _caps_map(tmp_path, ["completion"], 4096)  # the map changes: tools lost, window smaller
    decision_stamp.reset_cache()
    new_sig = decision_stamp.inventory_sig(p, db_path=db)
    assert new_sig != old_sig
    old = decision_stamp.resolve_inventory(old_sig, db_path=db)
    new = decision_stamp.resolve_inventory(new_sig, db_path=db)
    assert old["rows"] == [{"chain_id": "ollama/a", "kind": "local", "status": "connected", "effective_priority": 1,
                            "tools": True, "vision": False, "window": 8192}]
    assert (new["rows"][0]["tools"], new["rows"][0]["window"]) == (False, 4096)
    assert old["policy"] == "balanced"
    assert decision_stamp.resolve_inventory("000000000000", db_path=db) is None
    assert decision_stamp.resolve_inventory(None, db_path=db) is None


def test_the_snapshot_is_written_once_per_sig_and_the_db_stays_0600(tmp_path):
    db = tmp_path / "usage.db"
    p = _caps_map(tmp_path, ["completion", "tools"], 8192)
    for _ in range(3):
        decision_stamp.reset_cache()  # a cold compute each time, as in a short-lived hook process
        decision_stamp.inventory_sig(p, db_path=db)
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT count(*) FROM inventory_snapshots").fetchone() == (1,)
        sig, ts, body = con.execute("SELECT sig, first_seen_ts, body_json FROM inventory_snapshots").fetchone()
    finally:
        con.close()
    assert len(sig) == 12 and ts.endswith("Z") and json.loads(body)["rows"]
    assert stat.S_IMODE(os.stat(db).st_mode) == 0o600


def test_a_decision_row_sig_resolves_to_the_inventory_in_the_same_db(temp_db):
    """End to end: a real writer stamps the sig, and the snapshot is in the same usage.db."""
    p = _map(temp_db.parent, {"ollama/a": "connected"})
    with patch("llm_router.map_reader.token", return_value=(1,)), \
         patch("llm_router.map_reader.load_data", return_value=json.loads(p.read_text())):
        decision_stamp.reset_cache()
        _direct(layer="L1", confidence=0.9)
    (sig,) = sqlite3.connect(str(temp_db)).execute("SELECT inventory_sig FROM routing_decisions").fetchone()
    assert sig and decision_stamp.resolve_inventory(sig, db_path=temp_db)["rows"][0]["chain_id"] == "ollama/a"


@pytest.mark.parametrize("label", ["agent-route", "agent-route-cli", "build-fast-path", "benchmark-fp",
                                   "ollama", "api", "heuristic-weak", "hook", "something-new"])
def test_a_label_outside_the_closed_set_is_null_not_a_layer(label):
    assert decision_stamp.layer_for(label) is None
    assert decision_stamp.stamp(label, 0.9, "r")["layer"] is None


def test_every_layer_in_the_closed_set_is_accepted_and_the_ledger_layers_are_included():
    from llm_router import decision_ledger

    assert set(decision_ledger.LAYERS) <= set(decision_stamp.LAYERS)
    for lyr in decision_stamp.LAYERS:
        assert decision_stamp.layer_for(lyr) == lyr


def test_stamp_helpers_never_invent_a_confidence():
    for layer in ("override", "fallback", "unhinted", "prefix"):
        assert decision_stamp.confidence_for(layer, 0.9) is None
    for bad in (None, 0.0, -1, 1.5, float("nan"), True, "x"):
        assert decision_stamp.confidence_for("L1", bad) is None
    assert decision_stamp.confidence_for("L1", 0.8) == 0.8
    assert decision_stamp.layer_for("prefix:opus") == "prefix"
    assert decision_stamp.layer_for("heuristic+unservable:code") == "heuristic"
    assert decision_stamp.layer_for("L3") == "L3" and decision_stamp.layer_for("rules") == "rules"
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
    assert (layer, conf, sig) == (None, None, SIG)
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


def test_direct_writer_layer_from_a_known_label_and_null_for_a_door_name(temp_db, fixed_sig):
    _direct(classifier_type="heuristic")
    layer, conf, reason, sig, *_ = _row(temp_db)
    assert (layer, conf, reason, sig) == ("heuristic", None, "direct", SIG)


@pytest.mark.parametrize("door", ["agent-route", "agent-route-cli", "build-fast-path"])
def test_direct_writer_stores_null_layer_for_a_door_or_fast_path_name(temp_db, fixed_sig, door):
    _direct(classifier_type=door)
    layer, conf, *_ = _row(temp_db)
    assert (layer, conf) == (None, None)


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


# ── AST ratchet: a new routing-decision writer cannot skip the stamp ─────────────────────────────

#: Writers deliberately NOT stamped, each with the reason. Anything not here must stamp.
_EXEMPT_DIRECT_CALLERS = {
    # Door names, not layers: the closed set maps them to a NULL layer; confidence/reason/inventory_sig
    # are still stamped by log_direct_to_db itself.
    ("hooks/agent-route.py", "agent-route"), ("hooks/agent-route.py", "agent-route-cli"),
    ("src/llm_router/hooks/agent-route.py", "agent-route"), ("src/llm_router/hooks/agent-route.py", "agent-route-cli"),
}
_SRC_FILES = [*sorted((ROOT / "src").rglob("*.py")), *sorted((ROOT / "hooks").glob("*.py"))]


def _callee(call: ast.Call) -> str | None:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)


def _enclosing(tree: ast.AST, call: ast.Call):
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.lineno <= call.lineno <= (n.end_lineno or n.lineno)]
    return min(funcs, key=lambda n: (n.end_lineno or 0) - n.lineno) if funcs else None


def _is_stamp_call(node) -> bool:
    return isinstance(node, ast.Call) and _callee(node) == "stamp"


def _stamps(call: ast.Call, tree: ast.AST) -> bool:
    """True when ``call`` passes inventory_sig itself or splats the result of ``stamp(...)`` (a Name is
    resolved in the enclosing function; ambiguous or unresolved = not stamped)."""
    if any(k.arg == "inventory_sig" for k in call.keywords):
        return True
    for k in call.keywords:
        if k.arg is not None:
            continue
        if _is_stamp_call(k.value):
            return True
        if isinstance(k.value, ast.Name):
            fn = _enclosing(tree, call)
            vals = [n.value for n in ast.walk(fn) if isinstance(n, ast.Assign)
                    and any(getattr(t, "id", None) == k.value.id for t in n.targets)] if fn else []
            if len(vals) == 1 and _is_stamp_call(vals[0]):
                return True
    return False


def test_every_cost_routing_decision_writer_call_stamps():
    """Calls that carry ``reason_code=`` are the cost.log_routing_decision writer (the other functions of
    the same name, in model_tracking / routing_hints, never take it). Each must splat stamp(...)."""
    seen = []
    for path in _SRC_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            if _callee(call) in {"log_routing_decision", "_cost_log_routing_decision"} \
                    and any(k.arg == "reason_code" for k in call.keywords):
                seen.append(f"{path.relative_to(ROOT)}:{call.lineno}")
                assert _stamps(call, tree), f"{path.relative_to(ROOT)}:{call.lineno} writes a routing decision without the stamp"
    assert len(seen) >= 3, seen  # router.py, cost.py (failed call), savings_logger.py: an empty scan proves nothing


def test_every_log_direct_to_db_caller_passes_the_layer_or_is_a_listed_door():
    seen, exempt = [], []
    for path in _SRC_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call) and _callee(n) == "log_direct_to_db"):
            rel = str(path.relative_to(ROOT))
            seen.append(f"{rel}:{call.lineno}")
            if any(k.arg == "layer" for k in call.keywords):
                continue
            ct = next((k.value.value for k in call.keywords if k.arg == "classifier_type"
                       and isinstance(k.value, ast.Constant)), None)
            assert (rel, ct) in _EXEMPT_DIRECT_CALLERS, f"{rel}:{call.lineno} log_direct_to_db without layer= ({ct!r})"
            exempt.append((rel, ct))
    assert len(seen) >= 4 and exempt, (seen, exempt)  # sdk, auto-route x2, agent-route x4
    # every listed exemption is still real: a stale entry would hide a future bypass
    assert set(exempt) == _EXEMPT_DIRECT_CALLERS, set(exempt) ^ _EXEMPT_DIRECT_CALLERS


def test_the_writers_that_take_no_stamp_kwargs_stamp_inside():
    """log_direct_to_db (hook, agent-route, SDK) and the proxy ledger's write_row stamp themselves."""
    sl = ast.parse((ROOT / "src/llm_router/hooks/savings_logger.py").read_text())
    fn = next(n for n in ast.walk(sl) if isinstance(n, ast.FunctionDef) and n.name == "log_direct_to_db")
    assert any(_is_stamp_call(n) for n in ast.walk(fn)), "log_direct_to_db no longer calls decision_stamp.stamp"
    pl = ast.parse((ROOT / "src/llm_router/proxy/ledger.py").read_text())
    fn = next(n for n in ast.walk(pl) if isinstance(n, ast.FunctionDef) and n.name == "write_row")
    assert any(isinstance(n, ast.Call) and _callee(n) == "inventory_sig" for n in ast.walk(fn)), \
        "proxy ledger write_row no longer stamps inventory_sig"
    # and the proxy ledger has one door: only proxy/server.py calls write_row
    callers = {str(p.relative_to(ROOT)) for p in _SRC_FILES
               for c in ast.walk(ast.parse(p.read_text())) if isinstance(c, ast.Call) and _callee(c) == "write_row"
               and isinstance(c.func, ast.Attribute) and getattr(c.func.value, "id", None) == "ledger"}
    assert callers == {"src/llm_router/proxy/server.py"}, callers


def test_unstamped_routing_decision_inserts_are_the_listed_ones():
    """A raw ``INSERT INTO routing_decisions`` outside cost.py is a writer the stamp does not reach."""
    hits = {str(p.relative_to(ROOT)) for p in (ROOT / "src").rglob("*.py")
            if "INSERT INTO routing_decisions" in p.read_text(encoding="utf-8")}
    # cost.py = the stamped writer. lineage_store.py = the lineage subsystem's own table of the same
    # name in its own database (a different schema): documented as not stamped in the PR.
    assert hits == {"src/llm_router/cost.py", "src/llm_router/lineage/lineage_store.py"}, hits
