"""P2.13-a round B (UN-03): 20 everyday wordings x every door that enforces `prefer` rules.

Each wording must (1) parse to the intended canonical rule, (2) be stored through the MCP
admin door (`llm_router_admin(action="prefer")`'s two-step confirm), and (3) change the chain,
against that scenario's no-rule baseline, at the router/MCP door (`_build_and_filter_chain`,
also the SDK path), the hook (`chain_builder.build_chain`) and the proxy non-Claude choice
(`backends.policy_chain`), to exactly the chain the canonical spelling of the same rule gives
at that door. The proxy Claude-tier clause is not part of this matrix (xfail in
test_p213_round_b_cap_local.py; `proxy/tiers.py` is owned elsewhere).

The wordings are written the way people write them, not copied from the grammar's keyword
list; the ones the grammar still refuses are strict xfails at the bottom, not dropped.
"""
from __future__ import annotations

import json
import re
import time

import pytest

from llm_router import prefer_grammar, user_rules
from llm_router.types import Complexity, RoutingProfile, TaskType

GEM, OAI, LOCAL = "gemini/gemini-2.5-flash", "openai/gpt-4o-mini", "ollama/qwen3:8b"
BASE = [LOCAL, GEM, OAI]
CODE_PROMPT = "write a python function that reverses a list"


def _t(*providers: str) -> dict:
    return {"providers": list(providers), "model": None}


GEMINI, OPENAI, OLLAMA = _t("gemini", "gemini_cli"), _t("openai"), _t("ollama")
ORDER = {"kind": "order", "first": OPENAI, "then": OLLAMA}
BAN = {"kind": "ban", "target": GEMINI}
NO_PAID = {"kind": "no_paid_api"}
DAY_CAP = {"kind": "cap", "target": GEMINI, "unit": "day", "n": 2}
PCT_CAP = {"kind": "cap", "target": GEMINI, "unit": "pct", "n": 80}
LOCAL_CODE = {"kind": "local_only", "task": "code"}

# (wording, intended rule, canonical spelling of that rule)
PHRASINGS = [
    ("Please put openai ahead of ollama", ORDER, "openai before ollama"),
    ("use openai before ollama", ORDER, "openai before ollama"),
    ("openai first, then ollama", ORDER, "openai before ollama"),
    ("אופנאיי לפני מקומי", ORDER, "openai before ollama"),
    ("Never use Gemini.", BAN, "no gemini"),
    ("I don't want gemini", BAN, "no gemini"),
    ("stop using gemini", BAN, "no gemini"),
    ("בלי ג'מיני", BAN, "no gemini"),
    ("don't use paid APIs", NO_PAID, "no paid API"),
    ("no paid APIs please", NO_PAID, "no paid API"),
    ("בלי תשלום", NO_PAID, "no paid API"),
    ("limit gemini to two calls a day", DAY_CAP, "cap gemini 2/day"),
    ("cap gemini at 2 per day", DAY_CAP, "cap gemini 2/day"),
    ("הגבל את גמיני ל-2 ביום", DAY_CAP, "cap gemini 2/day"),
    ("cap gemini at eighty percent", PCT_CAP, "cap gemini 80%"),
    ("keep gemini under 80%", PCT_CAP, "cap gemini 80%"),
    ("הגבל את gemini ל-80%", PCT_CAP, "cap gemini 80%"),
    ("keep code local", LOCAL_CODE, "only local for code"),
    ("only use local models for coding", LOCAL_CODE, "only local for code"),
    ("רק מקומי לקוד", LOCAL_CODE, "only local for code"),
]


def provs(chain):
    return [m.split("/")[0] for m in chain]


def _store_via_canonical(sentence: str) -> None:
    parsed = prefer_grammar.parse(sentence)
    assert parsed.refusal is None, parsed.refusal
    user_rules.add_rules(parsed.rules, sentence)


def _store_via_mcp_admin(text: str) -> str:
    """The two-step MCP door: step 1 echoes + token, step 2 stores."""
    from llm_router.commands.prefer import admin_prefer

    step1 = admin_prefer(text)
    token = re.search(r'"confirm": "([0-9a-f]{12})"', step1)
    assert token, step1
    step2 = admin_prefer(json.dumps({"text": text, "confirm": token.group(1)}, ensure_ascii=False))
    assert "Stored p" in step2, step2
    return step1


@pytest.fixture
def doors(monkeypatch):
    import llm_router.dynamic_routing as dr
    from llm_router.config import RouterConfig, get_config
    from llm_router.hooks import chain_builder as hook_cb
    from llm_router.hooks.direct_executor import ModelSpec
    from llm_router.proxy import backends
    from llm_router.router import _build_and_filter_chain

    monkeypatch.setattr(dr, "get_dynamic_model_chain", lambda profile, task: list(BASE))
    monkeypatch.setattr(hook_cb, "_ollama_models", lambda *a, **k: ([ModelSpec("ollama", "qwen3:8b")], []))
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setattr(backends, "_chain_cache", {})
    monkeypatch.setattr(RouterConfig, "all_ollama_models", lambda self: [LOCAL])
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "qwen3:8b")
    monkeypatch.delenv("OLLAMA_API_BASE", raising=False)

    async def chains() -> dict[str, list[str]]:
        return {
            "router": await _build_and_filter_chain(
                TaskType.CODE, RoutingProfile.BALANCED, None, "simple", Complexity.SIMPLE, get_config()),
            "hook": [f"{m.provider}/{m.model}" for m in hook_cb.build_chain("simple", "green", "code")],
            "proxy": (await backends.policy_chain(CODE_PROMPT))[2],
        }

    return chains


async def _prepare(rule: dict, monkeypatch) -> None:
    """Make the scenario where *rule* has something to act on (a hit cap needs usage / pressure)."""
    from llm_router import cost
    from llm_router import resource_map as rm

    if rule == DAY_CAP:
        monkeypatch.setattr(cost, "_write_provenance", lambda: cost.PROVENANCE_RUNTIME)
        for _ in range(2):
            await cost.log_routing_decision(
                prompt="p", task_type="code", profile="balanced", classifier_type="heuristic",
                classifier_model=None, classifier_confidence=None, classifier_latency_ms=None,
                complexity="simple", recommended_model=GEM, base_model=GEM, was_downshifted=False,
                budget_pct_used=None, quality_mode=None, final_model=GEM, final_provider="gemini",
                success=True, input_tokens=10, output_tokens=10, cost_usd=0.0, latency_ms=5.0,
                reason_code="routed")
    elif rule == PCT_CAP:
        quota = {"pressure": {"value": 0.85, "provenance": "measured", "as_of": None}}
        rows = [("ollama/qwen3:8b", "local", {}), ("gemini/api", "api", quota), ("openai/api", "api", {})]
        res = [{"resource": c, "kind": k, "chain_id": c, "status": "connected", "effective_priority": i + 1,
                "capabilities": ["generate"], "quota": q} for i, (c, k, q) in enumerate(rows)]
        rm.write_map({"schema": rm.SCHEMA, "policy": "balanced", "resources": res,
                      "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())})


def _intended_effect(rule: dict, chain: list[str]) -> bool:
    p = provs(chain)
    kind = rule["kind"]
    if kind == "order":
        return bool(p) and p[0] == "openai" and sorted(p) == sorted(provs(BASE))
    if kind == "ban" or (kind == "cap"):
        return "gemini" not in p and "ollama" in p and "openai" in p
    return bool(p) and set(p) == {"ollama"}        # no_paid_api, local_only(code)


@pytest.mark.parametrize("wording, rule, canonical", PHRASINGS, ids=[w for w, _, _ in PHRASINGS])
async def test_wording_parses_and_is_enforced_identically_at_every_door(
        wording, rule, canonical, doors, temp_db, monkeypatch):
    parsed = prefer_grammar.parse(wording)
    assert parsed.refusal is None, parsed.refusal
    assert parsed.rules == [rule]

    await _prepare(rule, monkeypatch)
    baseline = await doors()
    assert all("gemini" in provs(c) and "openai" in provs(c) and "ollama" in provs(c)
               for c in baseline.values()), baseline

    _store_via_mcp_admin(wording)
    assert [{k: v for k, v in r.items() if k not in ("id", "text")} for r in user_rules.load_rules()] == [rule]
    got = await doors()
    for door, chain in got.items():
        assert chain != baseline[door], (door, chain)
        assert _intended_effect(rule, chain), (door, chain)

    user_rules.write_rules([])
    assert await doors() == baseline                           # removing the rule restores every door
    _store_via_canonical(canonical)
    assert await doors() == got                                # same rule, same chains, spelling aside


def test_the_twenty_wordings_are_distinct_and_cover_every_rule_kind_in_both_languages():
    assert len(PHRASINGS) == 20 and len({prefer_grammar.normalize(w) for w, _, _ in PHRASINGS}) == 20
    assert {r["kind"] for _, r, _ in PHRASINGS} == {"order", "ban", "no_paid_api", "cap", "local_only"}
    assert {r["unit"] for _, r, _ in PHRASINGS if r["kind"] == "cap"} == {"day", "pct"}
    hebrew = [w for w, _, _ in PHRASINGS if re.search("[א-ת]", w)]
    assert len(hebrew) >= 5
    # None of the wordings is simply a canonical spelling the grammar's own examples use.
    assert not {prefer_grammar.normalize(w) for w, _, _ in PHRASINGS} & {
        prefer_grammar.normalize(e) for e in prefer_grammar.EXAMPLES_EN + prefer_grammar._MORE_EXAMPLES}


# ── wordings the grammar still refuses: pinned as strict xfails, not dropped ──
UNPARSED = [
    ("I'd rather use openai than ollama", ORDER),
    ("gemini is off limits", BAN),
    ("free providers only", NO_PAID),
    ("gemini max 2 per day", DAY_CAP),
    ("don't let gemini go over 80%", PCT_CAP),
    ("code stays local", LOCAL_CODE),
]


@pytest.mark.parametrize("wording, rule", [pytest.param(w, r, marks=pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="UN-03 finding: the deterministic grammar refuses this "
    "everyday wording; free-form parsing is a separate, later item (docs/prefer.md)")) for w, r in UNPARSED],
    ids=[w for w, _ in UNPARSED])
def test_known_unparsed_wording_would_parse_to_the_intended_rule(wording, rule):
    parsed = prefer_grammar.parse(wording)
    assert parsed.refusal is None, parsed.refusal
    assert parsed.rules == [rule]
