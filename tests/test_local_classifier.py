"""LLM_ROUTER_LOCAL_CLASSIFIER=off|shadow|on — the local Ollama classifier seam.

Rules under test: off is byte-identical routing and never touches Ollama; shadow
logs the local answer next to the rules' answer and changes nothing; on feeds the
local task_type/complexity to the router, hook and proxy; a timeout or malformed
JSON falls back to the rules; no prompt text is ever persisted.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from pathlib import Path

import pytest

from llm_router import local_classifier as lc
from llm_router.classify import GATEWAY_POLICY, HOOK_POLICY, classify, classify_signals

MARKER = "ZQX-SECRET-PROMPT-MARKER-7731"
# Low-signal prompt: rules answer query/simple, so a local "opus" is a visible change.
PROMPT = f"hmm {MARKER} thoughts?"


def _reply(task="code", cx="complex", tier="opus"):
    return json.dumps({"task_type": task, "complexity": cx, "tier": tier})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_LOCAL_CLASSIFIER", raising=False)
    monkeypatch.setattr(lc, "_cool_until", 0.0)

    async def no_cloud(*a, **k):  # the cloud chain after the local entry: unavailable here
        raise RuntimeError("no classifier models")

    monkeypatch.setattr("llm_router.classifier.classify_complexity", no_cloud)


@pytest.fixture
def ollama(monkeypatch):
    """Fake Ollama. ``calls`` records every request; ``reply`` is what it says."""
    state = {"calls": [], "reply": _reply(), "delay": 0.0}

    def fake_post(model, text, timeout):
        state["calls"].append((model, text))
        time.sleep(state["delay"])
        return state["reply"]

    monkeypatch.setattr(lc, "_post", fake_post)
    return state


def _log_rows():
    p = lc._log_path()
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _all_home_text():
    return "".join(p.read_text(errors="ignore") for p in Path.home().rglob("*") if p.is_file())


# ── mode parsing ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    (None, "off"), ("", "off"), ("off", "off"), ("on", "on"), ("ON ", "on"),
    ("shadow", "shadow"), ("true", "off"), ("1", "off"), ("onn", "off"),
])
def test_mode_only_accepts_the_three_words(monkeypatch, raw, want):
    if raw is None:
        monkeypatch.delenv("LLM_ROUTER_LOCAL_CLASSIFIER", raising=False)
    else:
        monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", raw)
    assert lc.mode() == want


# ── off is byte-identical ─────────────────────────────────────────────────────

def test_off_never_calls_ollama_and_changes_nothing(ollama):
    base = classify_signals(PROMPT, GATEWAY_POLICY)
    out = asyncio.run(classify(PROMPT, policy=GATEWAY_POLICY))
    assert (out.task_type, out.complexity, out.method) == (base.task_type, base.complexity, base.method)
    assert ollama["calls"] == []
    assert not lc._log_path().exists()
    assert lc.apply(PROMPT, "query", "simple", "x") == ("query", "simple")


def test_off_policy_chain_matches_the_rules(monkeypatch, ollama):
    from llm_router.proxy import backends

    seen = []

    async def fake_chain(task, profile, *a):
        seen.append((task.value, a[2].value))
        return ["m"]

    monkeypatch.setattr("llm_router.router._build_and_filter_chain", fake_chain)
    backends._chain_cache.clear()
    sig = classify_signals(PROMPT, GATEWAY_POLICY)
    got = asyncio.run(backends.policy_chain(PROMPT))
    assert got[:2] == (sig.task_type.value, sig.complexity.value)
    assert ollama["calls"] == []


# ── shadow logs, changes nothing ──────────────────────────────────────────────

def test_shadow_logs_beside_rules_and_returns_rules(monkeypatch, ollama):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    base = classify_signals(PROMPT, GATEWAY_POLICY)
    out = asyncio.run(classify(PROMPT, policy=GATEWAY_POLICY))
    assert out.method != "local"
    assert (out.task_type, out.complexity) == (base.task_type, base.complexity)
    assert len(ollama["calls"]) == 1
    (row,) = _log_rows()
    assert row["rules"] == {"task_type": base.task_type.value, "complexity": base.complexity.value}
    assert row["local"]["tier"] == "opus" and row["local"]["task_type"] == "code"
    assert row["surface"] == "classify"


def test_shadow_proxy_chain_is_unchanged(monkeypatch, ollama):
    from llm_router.proxy import backends

    async def fake_chain(task, profile, *a):
        return ["m"]

    monkeypatch.setattr("llm_router.router._build_and_filter_chain", fake_chain)
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    backends._chain_cache.clear()
    sig = classify_signals(PROMPT, GATEWAY_POLICY)
    got = asyncio.run(backends.policy_chain(PROMPT))
    assert got[:2] == (sig.task_type.value, sig.complexity.value)
    assert _log_rows()[0]["surface"] == "proxy"


# ── on feeds the result ───────────────────────────────────────────────────────

def test_on_feeds_router_classify(monkeypatch, ollama):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    ollama["reply"] = _reply("analyze", "simple", "sonnet")  # tier decides: sonnet -> moderate
    out = asyncio.run(classify(PROMPT, policy=GATEWAY_POLICY))
    assert (out.task_type.value, out.complexity.value, out.method) == ("analyze", "moderate", "local")


def test_on_applies_the_policy_floor_only_where_the_policy_has_one(monkeypatch, ollama):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    ollama["reply"] = _reply("code", "simple", "haiku")
    floored = asyncio.run(classify(PROMPT, policy=HOOK_POLICY))
    unfloored = asyncio.run(classify(PROMPT, policy=GATEWAY_POLICY))
    assert floored.complexity.value == "moderate"  # code floor
    assert unfloored.complexity.value == "simple"


def test_on_feeds_proxy_tier_policy(monkeypatch, ollama):
    from llm_router.proxy import backends

    seen = []

    async def fake_chain(task, profile, *a):
        seen.append((task.value, a[2].value))
        return ["m"]

    monkeypatch.setattr("llm_router.router._build_and_filter_chain", fake_chain)
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    ollama["reply"] = _reply("code", "complex", "opus")
    backends._chain_cache.clear()
    assert asyncio.run(backends.policy_chain(PROMPT))[:2] == ("code", "complex")
    assert seen == [("code", "complex")]


def _load_hook():
    path = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"
    spec = importlib.util.spec_from_file_location("auto_route_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hook_off_shadow_on(monkeypatch, ollama):
    hook = _load_hook()
    monkeypatch.setattr(hook, "DISABLE_LLM_CLASSIFIERS", True)
    text = f"please look at the thing {MARKER}"
    rules = hook._classify_prompt_rules(text)
    assert hook.classify_prompt(text) == rules  # off
    assert ollama["calls"] == []

    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    assert hook.classify_prompt(text) == rules
    (row,) = _log_rows()
    assert row["surface"] == "hook" and row["rules"]["task_type"] == rules["task_type"]

    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    ollama["reply"] = _reply("code", "complex", "opus")
    got = hook.classify_prompt(text)
    assert (got["task_type"], got["complexity"], got["method"]) == ("code", "complex", "local")


# ── fallbacks ─────────────────────────────────────────────────────────────────

def test_timeout_falls_back_to_rules_within_budget(monkeypatch, ollama):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER_TIMEOUT_MS", "100")
    ollama["delay"] = 1.0
    base = classify_signals(PROMPT, GATEWAY_POLICY)
    t0 = time.monotonic()
    out = asyncio.run(classify(PROMPT, policy=GATEWAY_POLICY))
    assert time.monotonic() - t0 < 0.8
    assert out.method != "local"
    assert (out.task_type, out.complexity) == (base.task_type, base.complexity)


def test_failure_cools_down_so_a_dead_ollama_costs_one_budget(monkeypatch, ollama):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER_TIMEOUT_MS", "50")
    ollama["delay"] = 0.5
    assert lc.classify_local(PROMPT) is None
    n = len(ollama["calls"])
    assert lc.classify_local(PROMPT) is None
    assert len(ollama["calls"]) == n  # second call never left the process


def test_connection_error_falls_back(monkeypatch):
    def boom(model, text, timeout):
        raise ConnectionRefusedError("no ollama")

    monkeypatch.setattr(lc, "_post", boom)
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    assert lc.apply(PROMPT, "query", "simple", "x") == ("query", "simple")


@pytest.mark.parametrize("bad", [
    "", "not json", "[]", "null", "{}",
    '{"task_type":"code","complexity":"complex"}',                      # missing key
    '{"task_type":"code","complexity":"complex","tier":"gpt9"}',          # bad enum
    '{"task_type":"poetry","complexity":"complex","tier":"opus"}',        # bad enum
    '{"task_type":"code","complexity":"complex","tier":"opus","x":1}',    # extra key
    '```json\n{"task_type":"code","complexity":"complex","tier":"opus"}\n```',  # not strict
    '{"task_type":"code","complexity":"complex","tier":"opus"',           # truncated
])
def test_malformed_json_falls_back(monkeypatch, ollama, bad):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    ollama["reply"] = bad
    assert lc.classify_local(PROMPT) is None
    assert lc.apply(PROMPT, "query", "simple", "x") == ("query", "simple")
    out = asyncio.run(classify(PROMPT, policy=GATEWAY_POLICY))
    assert out.method != "local"


# ── privacy and shape ─────────────────────────────────────────────────────────

def test_no_prompt_text_is_persisted(monkeypatch, ollama):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    asyncio.run(classify(PROMPT, policy=GATEWAY_POLICY))
    lc.apply(PROMPT, "query", "simple", "hook")
    assert _log_rows(), "nothing was logged, so the check below would be vacuous"
    assert MARKER not in _all_home_text()
    assert _log_rows()[0]["prompt_chars"] == len(PROMPT)


def test_prompt_is_truncated_head_and_tail():
    long = "HEAD" + "x" * 5000 + "TAIL"
    t = lc.truncate(long)
    assert len(t) <= lc.MAX_PROMPT_CHARS and t.startswith("HEAD") and t.endswith("TAIL")
    assert lc.truncate("short") == "short"


def test_request_carries_schema_keepalive_and_truncated_text(monkeypatch):
    sent = {}

    class R:
        def read(self):
            return json.dumps({"message": {"content": _reply()}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        sent["body"] = json.loads(req.data)
        sent["timeout"] = timeout
        return R()

    monkeypatch.setattr(lc.urllib.request, "urlopen", fake_urlopen)
    v = lc.classify_local("A" * 9000, timeout_s=1.5)
    assert v and v.tier == "opus" and v.complexity == "complex"
    b = sent["body"]
    assert b["keep_alive"] == "600s" and b["format"] == lc.SCHEMA and b["think"] is False
    assert len(b["messages"][1]["content"]) <= lc.MAX_PROMPT_CHARS and sent["timeout"] == 1.5


def test_on_is_the_first_entry_of_the_classifier_chain(monkeypatch, ollama):
    from llm_router import classifier

    monkeypatch.undo()  # drop the autouse no_cloud stub: this test uses the real chain function
    monkeypatch.setattr(lc, "_post", lambda m, t, to: _reply("query", "simple", "haiku"))
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    monkeypatch.setattr(lc, "_cool_until", 0.0)
    r = asyncio.run(classifier.classify_complexity("what does this function return? unique-" + str(time.time())))
    assert r.classifier_model.startswith("ollama/") and r.complexity.value == "simple"
    assert r.classifier_cost_usd == 0.0
