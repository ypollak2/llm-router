"""P1.6 engine core (PLAN v16). Synthetic prompts only (D-42: counts prove mechanics, not accuracy).

Covers offline: P1.6-b (stability), c (L1 latency), d (L2 off the sync path), e (secrets, synthetic half),
f (invalid LLM output), j (fields; the ECE mechanics, not the held-out bar), k (engine network surface).
Needs wiring or live data, so NOT here: P1.6-a (7 call sites), g, h, i, the FP rate in e.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import random
import socket
import time
from pathlib import Path

import pytest
import yaml

from llm_router import engine, ml_lexical

SRC = Path(engine.__file__).parent


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DECIDE_CACHE", "memory")
    engine.clear_cache(persistent=False)
    engine._db.clear()
    yield
    engine.clear_cache(persistent=False)
    engine._db.clear()


_VERBS = ["explain", "write", "fix", "summarize", "compare", "implement", "refactor", "research", "list", "draw"]
_NOUNS = ["the capital of Portugal", "a python function that parses json", "tradeoffs of kafka vs sqs",
          "this stack trace in main.py", "a haiku about rain", "the latest news on rust async",
          "our database migration plan", "why the build is slow", "a poem", "unit tests for the parser"]


def synth(n: int, seed: int = 7) -> list[str]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        s = "%s %s %d" % (rng.choice(_VERBS), rng.choice(_NOUNS), i)
        if i % 5 == 0:
            s += " " + "detail " * rng.randint(0, 400)
        out.append(s)
    return out


FIELDS = [f.name for f in dataclasses.fields(engine.Decision)]


def test_decision_fields_are_the_plan_fields():
    assert FIELDS == ["task_type", "dims", "tier", "complexity", "needs_tools", "local_eligible",
                      "needs_repo_context", "local_only", "confidence", "calibrated", "abstain", "layer",
                      "reason", "policy_version", "engine_version", "ms"]
    d = engine.decide("explain async", door="hook")
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.tier = "opus"  # type: ignore[misc]


def test_p16j_every_l1_l3_decision_carries_every_field(tmp_path):
    rows = synth(60)
    labels = ["haiku" if len(t) < 90 else "opus" for t in rows]
    ml_lexical.save(ml_lexical.fit(rows, labels), tmp_path / "engine_lexical.json")
    layers = set()
    for t in synth(300, seed=11) + ["", " ", "x"]:
        d = engine.decide(t, door="gateway")
        if d.layer in ("L1", "L3"):
            layers.add(d.layer)
            assert d.task_type and d.dims is not None and d.tier in engine.TIERS and d.complexity
            assert isinstance(d.needs_tools, bool) and isinstance(d.local_eligible, bool)
            assert isinstance(d.needs_repo_context, bool) and isinstance(d.confidence, float)
            assert 0 < len(d.reason) <= 120 and d.policy_version and d.engine_version == engine.ENGINE_VERSION
    assert layers == {"L1", "L3"}, "the check must have seen both layers (an empty set passes everything)"


def test_door_does_not_change_the_decision_and_unknown_door_raises():
    ds = []
    for door in engine.DOORS:
        engine.clear_cache(persistent=False)  # a cache hit reports layer="cache"; compare like with like
        ds.append(engine.decide("write a python function that sorts a list", door=door))
    assert len(engine.DOORS) == 7
    assert all(d == ds[0] for d in ds)
    with pytest.raises(ValueError):
        engine.decide("hi", door="nope")


def test_engine_policy_starts_from_gateway_policy():
    from llm_router.classify import GATEWAY_POLICY
    assert engine.ENGINE_POLICY == GATEWAY_POLICY


def test_tier_map_matches_the_proxy_policy_file():
    y = yaml.safe_load((SRC / "proxy" / "claude_tiers.yaml").read_text())
    assert engine._COMPLEXITY_TIER == y["route"]["default"]
    assert list(engine.TIERS) == [t["name"] for t in y["tiers"]][:3]


def test_p16b_repeat_run_consistency_500_cache_cleared():
    prompts = synth(500, seed=3)
    a = [engine.decide(p, door="proxy") for p in prompts]
    engine.clear_cache(persistent=False)
    b = [engine.decide(p, door="proxy") for p in prompts]
    same = sum(x == y for x, y in zip(a, b))
    assert same == 500 and same / 500 >= 0.98
    assert {d.layer for d in a} <= {"L1", "L3"} and len({d.tier for d in a}) > 1


def test_cache_hit_equals_miss_and_reports_cache_layer():
    p = "refactor the router module to split classification"
    miss = engine.decide(p, door="mcp")
    hit = engine.decide(p, door="hook")
    assert miss.layer in ("L1", "L3") and hit.layer == "cache" and hit == dataclasses.replace(miss, layer="cache")
    # normalisation: system-reminder blocks and whitespace do not change the key
    assert engine.decide("<system-reminder>x</system-reminder>  " + p, door="hook").layer == "cache"
    # ctx digest bucket changes the key
    assert engine.decide(p, door="hook", ctx_digest="abcd1234").layer != "cache"


def test_sqlite_cache_persists_across_lru_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_DECIDE_CACHE", "sqlite")
    p = "implement a retry decorator with jitter"
    first = engine.decide(p, door="router")
    engine.clear_cache(persistent=False)
    again = engine.decide(p, door="router")
    assert again.layer == "cache" and again == dataclasses.replace(first, layer="cache")
    assert (tmp_path / "decide_cache.sqlite").exists()
    engine.clear_cache()
    assert engine.decide(p, door="router").layer != "cache"


def test_cache_key_changes_with_policy_version(tmp_path):
    p = "write a haiku about rain"
    engine.decide(p, door="hook")
    rows = ["write a poem", "fix the bug in parser", "research news today", "explain monads"] * 5
    ml_lexical.save(ml_lexical.fit(rows, ["haiku", "opus", "sonnet", "opus"] * 5), tmp_path / "engine_lexical.json")
    assert engine.decide(p, door="hook").layer != "cache"  # new artifact -> new policy_version


def test_p16c_l1_p95_under_50ms_n1000():
    prompts = synth(1000, seed=5)
    ms = []
    for p in prompts:
        t = time.perf_counter()
        engine.decide(p, door="hook")
        ms.append((time.perf_counter() - t) * 1000)
    ms.sort()
    p95 = ms[int(0.95 * len(ms)) - 1]
    assert len(ms) == 1000 and p95 <= 50, "p95=%.2f ms" % p95


def test_p16d_no_llm_or_network_on_the_sync_path(monkeypatch):
    import llm_router.classifier as c
    import llm_router.classify as cl

    def boom(*a, **k):
        raise AssertionError("LLM/network reached from decide")

    monkeypatch.setattr(c, "classify_complexity", boom)
    monkeypatch.setattr(cl, "classify", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    ds = [engine.decide(p, door="hook") for p in synth(200, seed=9)]
    assert all(d.layer in ("L1", "L3") for d in ds), "boom would have produced a fallback layer"


def _imports(path: Path) -> set[str]:
    out = set()
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module.split(".")[0] if n.level == 0 else "." + n.module)
            if n.module == "llm_router":
                out |= {"llm_router." + a.name for a in n.names}
            elif n.module.startswith("llm_router."):
                out.add(n.module)
    return out


def test_p16k_engine_modules_import_nothing_that_can_reach_the_network():
    net = {"httpx", "requests", "urllib", "urllib3", "aiohttp", "socket", "http", "litellm", "ollama",
           "openai", "anthropic", "websockets", "ssl"}
    forbidden_internal = {"llm_router.classifier", "llm_router.local_classifier", "llm_router.semantic_classify",
                          "llm_router.complexity_knn", "llm_router.router", "llm_router.proxy"}
    for f in ("engine.py", "ml_lexical.py"):
        imps = _imports(SRC / f)
        assert imps, f
        assert not (imps & net), (f, imps & net)
        assert not (imps & forbidden_internal), (f, imps & forbidden_internal)


def test_p16d_engine_has_no_async_or_llm_entry_points():
    tree = ast.parse((SRC / "engine.py").read_text())
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.AsyncFunctionDef, ast.Await))]
    called = {n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
              for n in ast.walk(tree) if isinstance(n, ast.Call)}
    decide_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "decide")
    in_decide = {getattr(c.func, "id", getattr(c.func, "attr", "")) for c in ast.walk(decide_fn) if isinstance(c, ast.Call)}
    assert "apply_l2" not in in_decide and "classify" not in in_decide and "classify_async" not in called


# ── secrets ──────────────────────────────────────────────────────────────────

SECRETS = (
    ["sk-ant-api03-" + "Ab1_-" * 6 + "xyz%02d" % i for i in range(5)]
    + ["sk-proj-" + "Qw3rTy" * 5 + "%02d" % i for i in range(5)]
    + ["AKIA" + "ABCDEFGH12345%03d" % i for i in range(5)]
    + ["AIza" + "SyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY%02d" % i for i in range(5)]
)


def test_p16e_20_of_20_synthetic_secrets_local_only_and_refused_without_local():
    assert len(SECRETS) == 20
    for s in SECRETS:
        d = engine.decide("please debug this call, my key is %s thanks" % s, door="mcp")
        assert d.local_only and d.local_eligible
        assert s not in d.reason and "secret:" in d.reason, "reason names the pattern, never the secret"
        assert engine.secret_enforcement(d, "mcp", local_model_healthy=True) == "local_only"
        with pytest.raises(engine.SecretLocalOnlyError, match="not sent"):
            engine.require_local_or_refuse(d, "gateway", local_model_healthy=False)
        # proxy only logs (D-28: a 400 would break a live Claude Code turn)
        assert engine.secret_enforcement(d, "proxy", local_model_healthy=False) == "log_only"
        engine.require_local_or_refuse(d, "proxy", local_model_healthy=False)


def test_p16e_clean_prompts_are_not_local_only():
    ds = [engine.decide(p, door="hook") for p in synth(200, seed=2)]
    assert not any(d.local_only for d in ds)
    assert engine.secret_enforcement(ds[0], "mcp", local_model_healthy=False) == "none"


# ── abstain / L2 schema ──────────────────────────────────────────────────────


def test_low_signal_abstains_one_tier_safer_and_confident_does_not():
    d = engine.decide("hmm ok then", door="hook")
    assert d.abstain and d.layer == "L1" and d.confidence < engine.TAU and "abstain" in d.reason
    base_tier = engine._COMPLEXITY_TIER[
        __import__("llm_router.classify", fromlist=["x"]).classify_signals("hmm ok then", engine.ENGINE_POLICY).complexity.value]
    assert d.tier == engine._safer(base_tier) != base_tier
    c = engine.decide("write a python function that parses json and add unit tests; refactor the class", door="hook")
    assert not c.abstain and c.confidence >= engine.TAU


def test_engine_error_returns_fallback_abstain(monkeypatch):
    monkeypatch.setattr(engine, "_compute", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    d = engine.decide("anything", door="hook")
    assert d.layer == "fallback" and d.abstain and d.tier == "sonnet" and d.confidence is None
    assert engine.decide("anything", door="hook").layer == "fallback", "an error is never cached"


@pytest.mark.parametrize("bad", [None, "", "not json", "[]", "{}", 3,
                                 {"task_type": "code", "tier": "gpt", "confidence": 0.9},
                                 {"task_type": "nope", "tier": "opus", "confidence": 0.9},
                                 {"task_type": "code", "tier": "opus", "confidence": 1.5},
                                 {"task_type": "code", "tier": "opus", "confidence": "high"},
                                 {"task_type": "code", "tier": "opus", "confidence": float("nan")},
                                 {"task_type": "code", "tier": "opus", "confidence": True}])
def test_p16f_invalid_llm_output_abstains_with_safer_tier_and_logs(bad, caplog):
    base = engine.decide("explain monads", door="hook")
    base = dataclasses.replace(base, tier="haiku", complexity="simple", abstain=False)
    with caplog.at_level("WARNING", logger="llm_router.engine"):
        d = engine.apply_l2(base, bad)
    assert d.abstain and d.tier == "sonnet" and d.complexity == "moderate" and d.layer == base.layer
    assert any("schema" in r.getMessage() for r in caplog.records)


def test_valid_llm_output_is_accepted_and_low_confidence_abstains():
    base = dataclasses.replace(engine.decide("explain monads", door="hook"), abstain=False)
    ok = engine.apply_l2(base, json.dumps({"task_type": "code", "tier": "haiku", "confidence": 0.9}))
    assert (ok.layer, ok.tier, ok.abstain, ok.task_type) == ("L2", "haiku", False, "code")
    lo = engine.apply_l2(base, {"task_type": "code", "tier": "haiku", "confidence": 0.2})
    assert lo.abstain and lo.tier == "sonnet"


# ── lexical model, calibration ───────────────────────────────────────────────


def test_lexical_fit_is_deterministic_and_learns(tmp_path):
    rows = ["fix typo in readme", "what is 2+2", "rename variable x", "list files"] * 10 \
        + ["design a distributed consensus protocol with proofs", "architect a multi-region failover system",
           "prove the algorithm terminates and analyse complexity", "redesign the entire data model"] * 10
    y = ["haiku"] * 40 + ["opus"] * 40
    a, b = ml_lexical.fit(rows, y, seed=1), ml_lexical.fit(rows, y, seed=1)
    assert a == b and a.version == b.version
    assert a.predict("rename the variable in readme")[0] == "haiku"
    assert a.predict("architect a failover protocol with proofs")[0] == "opus"
    p = ml_lexical.save(a, tmp_path / "m.json")
    assert ml_lexical.load(p) == a
    assert ml_lexical.load(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text("{}")
    assert ml_lexical.load(tmp_path / "bad.json") is None
    with pytest.raises(ValueError):
        ml_lexical.fit(["a", "b"], ["haiku", "haiku"])
    with pytest.raises(ValueError):
        ml_lexical.fit([], [])


def test_l3_decides_tier_where_l1_is_low_signal(tmp_path):
    ml_lexical.save(ml_lexical.fit(["hmm ok then", "ok fine"] * 10 + ["zzz big task"] * 10,
                                   ["opus"] * 20 + ["haiku"] * 10), tmp_path / "engine_lexical.json")
    d = engine.decide("hmm ok then", door="hook")
    assert d.layer == "L3" and d.tier == "opus" and d.complexity == "complex" and "L3" in d.reason


def test_calibration_applies_and_marks_calibrated(tmp_path):
    p = "write a python function that parses json and add unit tests; refactor the class"
    raw = engine.decide(p, door="hook")
    assert raw.calibrated is False
    table = {"L1": [0.4] * 10}
    (tmp_path / "engine_calibration.json").write_text(json.dumps(table))
    d = engine.decide(p, door="hook")
    assert d.calibrated and d.confidence == 0.4 and d.abstain and d.tier == engine._safer(raw.tier)


def test_ece_and_reliability_table_mechanics():
    conf = [0.95] * 10 + [0.55] * 10
    ok = [True] * 9 + [False] + [True] * 5 + [False] * 5
    t = engine.reliability_table(conf, ok)
    assert len(t) == 10 and sum(r["n"] for r in t) == 20 and t[9]["n"] == 10 and t[5]["n"] == 10
    assert engine.ece(conf, ok) == pytest.approx(0.5 * abs(0.95 - 0.9) + 0.5 * abs(0.55 - 0.5))
    assert engine.ece([0.5, 0.5], [True, False]) == 0.0
    assert engine.ece([1.0], [True]) == 0.0
    with pytest.raises(ValueError):
        engine.ece([], [])
    assert engine.fit_calibration(conf, ok)[9] == 0.9 and engine.fit_calibration(conf, ok)[0] is None


def test_decision_reason_never_over_120_chars():
    long = "x" * 5000
    assert len(engine.decide(long, door="hook").reason) <= 120
    assert len(engine._reason("y" * 500)) == 120
