"""P1.6 wiring (PLAN v16): the 7 classifier call sites decide with the engine's one Decision.

Covers P1.6-a (same Decision at every door on 200 synthetic prompts; the AST test: 0 other
classifier calls on head, >= 7 sites on da31df7), P1.6-h mechanics (hook -> proxy reuse by
``text_sha`` and the counts-only instrument), and the wiring's no-routing-change switch (abstain is
log-only). Synthetic prompts only (D-42: counts prove mechanics, not accuracy). Needs live organic
data, so NOT proven here: the P1.6-h >= 90% bar (n = 100*), P1.6-i's final disagreement on the
frozen corpus (a script run, see the PR).
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import io
import json
import random
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_router import engine

SRC = Path(engine.__file__).parent
HOOKS = SRC / "hooks"
FIXTURE = Path(__file__).parent / "fixtures" / "classifier_sites_da31df7.py.txt"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DECIDE_CACHE", "memory")
    monkeypatch.delenv("LLM_ROUTER_ENGINE_ABSTAIN", raising=False)
    monkeypatch.delenv("LLM_ROUTER_ENSEMBLE", raising=False)
    # These tests cover the engine-decides wiring (the #433 behaviour). The default is shadow (D-72),
    # covered by tests/test_engine_shadow.py.
    monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", "decide")
    engine.clear_cache(persistent=False)
    engine._db.clear()
    yield
    engine.clear_cache(persistent=False)
    engine._db.clear()


def _load(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HOOKS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── P1.6-a: the AST test ─────────────────────────────────────────────────────

#: Entry points of the old classifiers. A call to any of these outside ALLOWED is a second engine.
OLD_CLASSIFIERS = frozenset({
    "classify_signals", "complexity_for", "classify_complexity", "classify_prompt_heuristic",
    "_classify_task_type", "_classify_complexity", "classify_ensemble",
})
#: module (relative to src/llm_router) -> why it may still call one.
ALLOWED = {
    "classify.py": "defines the L1 heuristics; classify() and complexity_for() call each other",
    "engine.py": "the one engine: L1 is classify_signals(ENGINE_POLICY)",
    "ensemble.py": "legacy path behind LLM_ROUTER_ENSEMBLE=legacy, kept for one release (PLAN task 3 item 2)",
    "commands/test.py": "`llm-router test`: a diagnostic that exercises the cloud classifier on purpose, not a routing door",
}


def classifier_calls(source: str) -> list[tuple[str, int]]:
    out = []
    for n in ast.walk(ast.parse(source)):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name in OLD_CLASSIFIERS:
                out.append((name, n.lineno))
    return out


def scan_tree(root: Path) -> dict[str, list[tuple[str, int]]]:
    found = {}
    for p in sorted(root.rglob("*.py")):
        calls = classifier_calls(p.read_text(encoding="utf-8"))
        if calls:
            found[p.relative_to(root).as_posix()] = calls
    return found


def test_every_remaining_rules_classifier_is_paired_with_the_engine():
    """D-72: the rules still route at the 7 doors, so the old classifier calls stay. Each module that
    keeps one must also reach for the engine (shadow call and the ``decide`` opt-in); a rules site with
    no engine pairing is the second classifier P1.6-a forbids. (P1.6-a proper, 0 calls outside the
    engine, returns when the engine decides, 16.1.)"""
    found = scan_tree(SRC)
    # An empty scan passes everything: the scanner must see the allowed callers it is told to skip.
    assert {"classify.py", "engine.py", "ensemble.py"} <= set(found), sorted(found)
    others = {m: c for m, c in found.items() if m not in ALLOWED}
    assert len(others) >= 7, sorted(others)  # the doors: gateway, proxy, 3 hooks, router, agentic
    # The server doors pair through llm_router.engine_door (fail-open seam); the hooks call engine directly.
    def paired(m: str) -> bool:
        t = (SRC / m).read_text(encoding="utf-8")
        return (("engine.shadow(" in t and "decides()" in t)
                or ("engine_door.shadow(" in t and "engine_door.decision(" in t))

    unpaired = [m for m in others if not paired(m)]
    assert unpaired == [], "a rules classifier with no engine shadow/decide pairing: %s" % unpaired


def test_scanner_finds_at_least_7_sites_on_da31df7():
    """The proof it can see them: verbatim excerpts of da31df7e, scanned by the same scanner."""
    text = FIXTURE.read_text(encoding="utf-8")
    chunks = {}
    for part in text.split("# FILE: ")[1:]:
        rel, _, body = part.partition("\n")
        chunks[rel.strip()] = classifier_calls(body)
    sites = {m: c for m, c in chunks.items() if c and m not in ALLOWED}
    assert len(sites) >= 7, sites
    assert sum(len(c) for c in sites.values()) >= 7
    # the 7 PLAN sites are among them
    for rel in ("gateway.py", "proxy/backends.py", "hooks/auto-route.py", "hooks/agent-route.py",
                "hooks/gemini-cli-auto-route.py", "router.py"):
        assert chunks.get(rel), rel


# ── P1.6-a: same Decision at every door ──────────────────────────────────────

_VERBS = ["explain", "write", "fix", "summarize", "compare", "implement", "refactor", "research", "list", "draw"]
_NOUNS = ["the capital of Portugal", "a python function that parses json", "tradeoffs of kafka vs sqs",
          "this stack trace in main.py", "a haiku about rain", "the latest news on rust async",
          "our database migration plan", "why the build is slow", "a poem", "unit tests for the parser"]


def synth(n: int, seed: int = 11) -> list[str]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        s = "%s %s %d" % (rng.choice(_VERBS), rng.choice(_NOUNS), i)
        if i % 4 == 0:
            s += " please " + "with more detail " * rng.randint(0, 120)
        out.append(s)
    return out


def _doors():
    from llm_router import gateway
    from llm_router.ensemble import engine_classification
    from llm_router.proxy import backends
    from llm_router.types import RoutingProfile
    from llm_router import router

    hook = _load("hook_doors", "auto-route.py")
    agent = _load("agent_doors", "agent-route.py")
    gemini = _load("gemini_doors", "gemini-cli-auto-route.py")
    cfg = SimpleNamespace(llm_router_profile=RoutingProfile.BALANCED)

    def proxy(t):
        r = asyncio.run(backends.tier_classify(t, None, anthropic=True))
        return r["task_type"], r["complexity"]

    def mcp(t):
        r = engine_classification(t)
        return r.inferred_task_type.value, r.complexity.value

    def hook_door(t):
        r = hook.classify_prompt(t)
        return None if r is None else (r["task_type"], r["complexity"], r["method"])

    return {
        "proxy": proxy, "mcp": mcp, "gateway": gateway._classify, "hook": hook_door,
        "agent_route": agent._engine_decision,
        "gemini_cli": lambda t: (None, asyncio.run(gemini.engine_complexity(t))[0]),
        "router": lambda t: (None, router._resolve_profile(None, None, None, t, None, cfg)[1].value),
    }


def test_p16a_same_decision_at_all_7_sites_on_200_prompts():
    prompts = synth(200)
    assert len(set(prompts)) == 200
    doors = _doors()
    assert set(doors) == set(engine.DOORS)
    gated = 0
    for p in prompts:
        d = engine.decide(p, door="gateway")
        want = (d.task_type, d.complexity)
        for name, fn in doors.items():
            got = fn(p)
            if name == "hook":
                if got is None or got[2].endswith("fast-path") or got[0] == "coordination":
                    gated += 1  # the hook's gates (None, coordinate, build, content, introspect, benchmark,
                    continue    # coordination bucket) are not the engine's: they are the measured residual
                got = got[:2]
            if got[0] is None:  # complexity-only doors
                assert got[1] == want[1], (name, p[:40])
            else:
                assert got == want, (name, p[:40], got, want)
    assert 0 < gated < 200, "the hook's gates took %d of 200 synthetic prompts" % gated  # 0 would hide them


def test_the_hook_gates_are_the_only_hook_disagreement():
    hook = _load("hook_doors", "auto-route.py")
    assert hook.classify_prompt("hi") is None  # under 8 chars
    coord = hook.classify_prompt("coordinate three agents to review the migration plan and report back")
    assert coord["task_type"] == "coordinate" and coord["method"] == "coordination-fast-path"
    plain = hook.classify_prompt("explain the capital of Portugal in detail")
    assert plain["method"] in ("heuristic", "heuristic-weak", "fallback")
    assert (plain["task_type"], plain["complexity"]) == tuple(
        getattr(engine.decide("explain the capital of Portugal in detail", door="hook"), k) for k in ("task_type", "complexity"))


def test_wired_doors_do_not_move_a_tier_on_the_placeholder_tau():
    """No routing change from the wiring: a low-signal prompt keeps the complexity L1 gave it."""
    from llm_router.classify import classify_signals

    for p in ("hmm ok then", "ok", "thanks that works"):
        d = engine.decide(p, door="gateway")
        assert d.abstain and d.complexity == classify_signals(p, engine.ENGINE_POLICY).complexity.value


def test_mcp_door_legacy_switch(monkeypatch):
    from llm_router import ensemble

    assert ensemble._engine_mode() == "engine" and not ensemble._ensemble_enabled()
    for v, mode in (("legacy", "legacy"), ("on", "legacy"), ("off", "off"), ("engine", "engine")):
        monkeypatch.setenv("LLM_ROUTER_ENSEMBLE", v)
        assert ensemble._engine_mode() == mode
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE", "off")
    called = []

    async def cloud(prompt, **kw):
        called.append(prompt)
        return "cloud"

    import llm_router.classifier as clf
    monkeypatch.setattr(clf, "classify_complexity", cloud)
    assert asyncio.run(ensemble.classify_for_routing("explain async", timeout_seconds=1.0)) == "cloud" and called


def test_mcp_door_default_makes_no_llm_call(monkeypatch):
    from llm_router import ensemble

    async def boom(*a, **k):
        raise AssertionError("an LLM was called on the engine path")

    monkeypatch.setattr(ensemble, "local_llm_classify", boom)
    p = "write a python function that parses json"
    r = asyncio.run(ensemble.classify_for_routing(p))
    d = engine.decide(p, door="mcp")
    assert r.classifier_model == "engine" and r.classifier_cost_usd == 0.0
    assert (r.inferred_task_type.value, r.complexity.value) == (d.task_type, d.complexity)


# ── P1.6-h: hook -> proxy reuse ──────────────────────────────────────────────


def test_reuse_roundtrip_matches_by_text_sha_and_stores_no_text(tmp_path):
    text = "explain how the retry loop in the gateway works"
    d = engine.decide(text, door="hook")
    engine.write_hook_decision("sess-1", text, d)
    f = tmp_path / "last_classification_sess-1.json"
    assert f.exists() and (f.stat().st_mode & 0o777) == 0o600
    rec = json.loads(f.read_text())
    assert set(rec) == {"text_sha", "tier_sha", "decision", "ts"} and text not in f.read_text()
    assert rec["text_sha"] == engine.text_sha(text)
    assert engine.read_hook_decision("sess-1", text) == (True, d)
    assert engine.read_hook_decision("sess-1", "a different prompt")[1] is None
    assert engine.read_hook_decision("sess-1", "a different prompt")[0] is True
    assert engine.read_hook_decision("other-sess", text) == (False, None)
    assert engine.read_hook_decision(None, text) == (False, None)


def test_reuse_matches_the_proxys_3000_char_tail():
    text = "start " * 900 + "the question at the end of a long prompt"
    assert len(text) > 3000
    d = engine.decide(text, door="hook")
    engine.write_hook_decision("sess-2", text, d)
    tail = text[-3000:]  # what proxy.steps.tier_text hands the classifier
    assert engine.read_hook_decision("sess-2", tail) == (True, d)


def test_reuse_ignores_a_stale_record(monkeypatch):
    text = "explain the build"
    engine.write_hook_decision("sess-3", text, engine.decide(text, door="hook"))
    real = engine.time.time
    monkeypatch.setattr(engine.time, "time", lambda: real() + engine._REUSE_TTL_S + 1)
    assert engine.read_hook_decision("sess-3", text) == (False, None)


def test_proxy_reuses_the_hook_decision_and_counts_it(tmp_path, monkeypatch):
    from llm_router.proxy import backends

    text = "write a python function that parses json"
    hook_d = engine.decide(text, door="hook")
    engine.write_hook_decision("sess-4", text, hook_d)
    calls = []
    real = engine.decide
    monkeypatch.setattr(engine, "decide", lambda *a, **k: calls.append(1) or real(*a, **k))
    tok = engine.bind_session("sess-4")
    try:
        r = asyncio.run(backends.tier_classify(text, None))              # same text: reuse
        r2 = asyncio.run(backends.tier_classify("something else entirely to classify", None))  # hook-seen, no match
    finally:
        engine.unbind_session(tok)
    assert (r["task_type"], r["complexity"]) == (hook_d.task_type, hook_d.complexity)
    assert len(calls) == 1 and r2["task_type"]
    rows = [json.loads(line) for line in (tmp_path / "decision_reuse.jsonl").read_text().splitlines()]
    assert [x["reused"] for x in rows] == [True, False] and all(set(x) == {"ts", "reused"} for x in rows)
    # no hook record for the session -> not hook-seen -> not counted
    tok = engine.bind_session("never-seen")
    try:
        asyncio.run(backends.tier_classify(text, None))
    finally:
        engine.unbind_session(tok)
    assert len((tmp_path / "decision_reuse.jsonl").read_text().splitlines()) == 2


def test_tier_policy_decide_binds_the_session(monkeypatch):
    """ClaudeTierPolicy.decide hands its session id to the classifier through the engine's context."""
    from llm_router.proxy import tiers

    seen = []
    orig = engine.bind_session
    monkeypatch.setattr(engine, "bind_session", lambda sid: seen.append(sid) or orig(sid))

    async def run():
        policy = tiers.ClaudeTierPolicy.load(None)

        class Sticky:
            def get(self, *a, **k): return None
            def record(self, *a, **k): return None

        body = {"model": "claude-opus-4-6", "messages": [{"role": "user", "content": "explain async"}]}
        await policy.decide(body, "sess-5", Sticky())

    asyncio.run(run())
    assert seen == ["sess-5"] and engine._session_var.get() is None


def test_hook_main_leaves_its_decision_for_the_proxy(monkeypatch, tmp_path):
    ar = _load("hook_main_doors", "auto-route.py")
    prompt = "write a python function that parses json and add unit tests"
    for k, v in {"LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "1", "LLM_ROUTER_DIRECT_EXECUTION": "0",
                 "LLM_ROUTER_ENFORCE": "suggest", "LLM_ROUTER_ZERO_CLAUDE": "off"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(ar, "_debug_log", lambda *_a, **_k: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": prompt, "session_id": "sess-6"})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    try:
        ar.main()
    except SystemExit:
        pass
    seen, d = engine.read_hook_decision("sess-6", prompt)
    assert seen and d == engine.decide(prompt, door="proxy")


def test_instrument_reports_pending_below_min_n_and_never_passes_on_small_n(tmp_path):
    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    import hook_reuse_rate as h

    assert h.summarize([])["verdict"].startswith("PENDING")
    rows = [{"ts": 1.0 + i, "reused": True} for i in range(99)]
    r = h.summarize(rows)
    assert r["n"] == 99 and r["rate"] == 1.0 and r["verdict"].startswith("PENDING") and "not a pass" in r["verdict"]
    rows = [{"ts": 1.0 + i, "reused": i % 20 != 0} for i in range(400)]  # 95%
    assert h.summarize(rows)["verdict"] == "PASS"
    rows = [{"ts": 1.0 + i, "reused": i % 2 == 0} for i in range(200)]   # 50%
    assert h.summarize(rows)["verdict"] == "FAIL"
    rows = [{"ts": 1.0 + i, "reused": i % 10 != 0} for i in range(100)]  # 90% on n=100: CI straddles the bar
    assert h.summarize(rows)["verdict"].startswith("NOT DISTINGUISHABLE")
    p = tmp_path / "decision_reuse.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in rows) + "\nnot json\n")
    assert len(h.read_rows(p)) == 100 and len(h.read_rows(p, since=50.0)) == 51


# ── D-69: keyword + length complexity; the 2026-06-06 wrapped-prompt incident stays fixed ──────────

HARD = [
    "design a multi-region architecture for a payments platform with strict consistency",
    "Architect a distributed task queue with retries and dead letter handling",
]


@pytest.mark.parametrize("prompt", HARD)
def test_hard_prompts_are_complex_at_every_door(prompt):
    doors = _doors()
    for name, fn in doors.items():
        got = fn(prompt)
        cx = got[1]
        assert cx in ("complex", "deep_reasoning"), (name, got)
    assert engine.decide(prompt, door="gateway").tier == "opus"


def test_the_keyword_signals_come_from_the_three_old_scorers():
    from llm_router import classify as C

    # one signal from each source, all >= 40 chars so the keyword rule applies
    pad = " in the payment service module of the repo"
    for kw, src in (("build a scalable job runner", "classify._COMPLEXITY_COMPLEX"),
                    ("review the security of the login flow", "agent-route _COMPLEX_SIGNALS"),
                    ("optimize the slow query in the report", "gemini-cli complex_patterns")):
        t = kw + pad
        assert len(t) >= 40
        assert C.complexity_for(t, policy=engine.ENGINE_POLICY).value == "complex", src
    plain = "what time zone is the office in right now, by the way"
    assert C.complexity_for(plain, policy=engine.ENGINE_POLICY).value == "simple"
    assert C.complexity_for("optimize it", policy=engine.ENGINE_POLICY).value == "simple"  # < 40 chars


@pytest.mark.parametrize("n", [401, 450, 500, 599])
def test_wrapped_prompts_of_401_to_599_chars_stay_simple_at_the_router(n):
    """2026-06-06: callers wrap the user's prompt, the wrapped form crosses 300-400 chars, and
    everything routed to Sonnet. The no-hint fallback keeps `simple` up to 599 chars (the engine's
    simple band), and no door sends a keyword-free 401-599 char prompt above `simple`."""
    prompt = ("Please answer the following question for the user. " + "lorem ipsum dolor sit amet " * 40)[:n]
    assert len(prompt) == n
    for name, fn in _doors().items():
        got = fn(prompt)
        if name == "hook":
            continue  # the hook's fast paths may take a prompt; the engine path is checked below
        assert got[1] == "simple", (name, n, got)
    assert engine.decide(prompt, door="hook").complexity == "simple"
    assert engine.decide(prompt + " x" * 100, door="hook").complexity == "moderate"  # > 599


def test_the_router_fallback_boundary_is_unchanged_at_600():
    from types import SimpleNamespace

    from llm_router import router
    from llm_router.types import RoutingProfile

    cfg = SimpleNamespace(llm_router_profile=RoutingProfile.BALANCED)
    cx = lambda n: router._resolve_profile(None, None, None, "x" * n, None, cfg)[1].value  # noqa: E731
    assert [cx(599), cx(600), cx(2000), cx(2001)] == ["simple", "moderate", "moderate", "complex"]
