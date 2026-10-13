"""D-83: the 7 classifier doors agree on the engine path in decide mode (PLAN v16 P1.6-a, P1.6-i).

The frozen corpus measured two splits at 4c964be9 (labels and lengths only): agent-route and
gemini-cli folded the engine's ``deep_reasoning`` into ``complex`` while the other doors reported it
verbatim, and the proxy classified ``steps.tier_text`` (harness blocks stripped, 3,000-char tail)
while the other doors classified the raw prompt. Each split is reproduced here through the real
site loaders of ``scripts/door_agreement.py``. Synthetic prompts only: the corpus never enters a test.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from llm_router import engine
from llm_router.proxy import steps

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "door_agreement.py"
HOOKS = Path(engine.__file__).parent / "hooks"
SITES = ("hook", "gateway", "proxy", "mcp", "agent_route", "gemini_cli", "router")

PROOF = ("Prove rigorously, step by step, that the square root of 2 is irrational, and derive the "
         "general theorem for any non-square integer.")
FILLER = "please review these notes about the garden "
HUMAN_1950 = (FILLER * 60)[:1950]
# A slash-command echo in front of 1,950 human chars: 2,112 raw chars (complex by length at a door
# that reads the raw text), 1,950 once stripped (moderate at the proxy before the fix).
HARNESS = ("<command-message>notes is running</command-message>\n<command-args>" + "x " * 40
           + "</command-args>\n" + HUMAN_1950)
# A reasoning trigger in the head, beyond the proxy's 3,000-char tail.
LONG = PROOF + " " + FILLER * 90


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DECIDE_CACHE", "memory")
    monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", "decide")
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.delenv("LLM_ROUTER_ENGINE_ABSTAIN", raising=False)
    monkeypatch.delenv("LLM_ROUTER_ENGINE_LEXICAL", raising=False)
    engine.clear_cache(persistent=False)
    engine._db.clear()
    yield
    engine.clear_cache(persistent=False)
    engine._db.clear()


@pytest.fixture(scope="module")
def da():
    spec = importlib.util.spec_from_file_location("_door_agreement_d83", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def classifiers(da, monkeypatch):
    from llm_router import router

    for k, v in da._HOOK_NO_LLM_ENV.items():
        monkeypatch.setenv(k, v)
    # The proxy loader replaces the chain build for the whole process; put it back after the test.
    monkeypatch.setattr(router, "_build_and_filter_chain", router._build_and_filter_chain)
    return {name: da.SITES[name].load() for name in SITES}


def _load_hook(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HOOKS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _body(text: str) -> dict:
    return {"messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]}


def _measure(da, classifiers, prompts):
    hook = classifiers["hook"]
    hook.engine_path.clear()
    out = da.measure(prompts, classifiers, engine_path=hook.engine_path)
    assert out["engine_path"]["n"] == len(prompts), "a fixture left the hook's engine path"
    return out


def _complexities(classifiers, prompt) -> dict[str, str]:
    return {name: fn(prompt)[1] for name, fn in classifiers.items()}


def test_deep_reasoning_is_reported_verbatim_at_every_door(da, classifiers):
    assert engine.decide(PROOF, door="mcp").complexity == "deep_reasoning"  # the fixture's precondition
    got = _complexities(classifiers, PROOF)
    assert set(got.values()) == {"deep_reasoning"}, got
    out = _measure(da, classifiers, [PROOF])
    assert out["engine_path"]["all_sites_tier_disagree"] == 0
    assert out["engine_path"]["all_sites_disagree"] == 0


def test_the_three_row_hooks_still_route_deep_reasoning_as_complex():
    """Routing unchanged: the fold moved from the decision to the profile/hint table lookup."""
    agent = _load_hook("_d83_agent_route", "agent-route.py")
    gemini = _load_hook("_d83_gemini_cli", "gemini-cli-auto-route.py")
    assert agent._engine_decision(PROOF)[1] == "deep_reasoning"
    assert agent._classify(PROOF)[1] == "complex"
    assert asyncio.run(gemini.engine_complexity(PROOF))[0] == "deep_reasoning"
    assert asyncio.run(gemini._complexity(PROOF))[0] == "complex"


def test_harness_block_does_not_split_the_proxy(da, classifiers):
    assert len(HARNESS) > 2000 and steps.tier_text(_body(HARNESS)) == HUMAN_1950  # preconditions
    got = _complexities(classifiers, HARNESS)
    assert len(set(got.values())) == 1, got
    assert engine.decide(HARNESS, door="mcp").complexity == engine.decide(HUMAN_1950, door="mcp").complexity
    out = _measure(da, classifiers, [HARNESS])
    assert out["engine_path"]["all_sites_tier_disagree"] == 0
    assert out["engine_path"]["all_sites_disagree"] == 0


def test_tail_truncation_does_not_split_the_proxy(da, classifiers):
    assert len(LONG) > 3000 and PROOF not in steps.tier_text(_body(LONG))  # preconditions
    got = _complexities(classifiers, LONG)
    assert len(set(got.values())) == 1, got
    out = _measure(da, classifiers, [LONG])
    assert out["engine_path"]["all_sites_tier_disagree"] == 0
    assert out["engine_path"]["all_sites_disagree"] == 0


def test_secret_outside_the_canonical_text_still_sets_local_only():
    key = "sk-ant-" + "A1b2C3d4" * 6
    in_block = "explain the capital of Portugal\n<system-reminder>\nkey " + key + "\n</system-reminder>"
    in_head = key + " " + FILLER * 100
    for p in (in_block, in_head):
        assert key not in engine.canonical_text(p)  # the secret is outside what the class is computed on
        assert engine.decide(p, door="gateway").local_only is True


def test_canonical_text_is_the_proxys_tier_text_and_idempotent():
    for raw in (HARNESS, LONG, PROOF):
        c = engine.canonical_text(raw)
        assert c == steps.tier_text(_body(raw))
        assert engine.canonical_text(c) == c
