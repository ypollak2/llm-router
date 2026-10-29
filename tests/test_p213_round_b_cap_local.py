"""P2.13-a round B (UN-02): stored `cap`, `only local for <task>` and `no paid API` rules
steer the chains.

Doors: MCP/router (`_build_and_filter_chain`), hook (`hooks.chain_builder.build_chain`),
proxy non-Claude (`proxy.backends.policy_chain` / `choose_model`). Rules go through the real
grammar + store into a tmp `routing.yaml`; calls are recorded through the real
`cost.log_routing_decision` writer into a tmp usage.db (only the provenance stamp is forced to
`runtime`, since the writer stamps every row written under pytest as `test`).
"""
from __future__ import annotations

import time

import pytest

from llm_router import map_reader, prefer_grammar, user_rules
from llm_router.types import Complexity, RoutingProfile, TaskType

GEM, OAI, LOCAL, CODEX = "gemini/gemini-2.5-flash", "openai/gpt-4o-mini", "ollama/qwen3:8b", "codex/gpt-5.4"
BASE = [LOCAL, GEM, OAI]
CODE_PROMPT = "write a python function that reverses a list"
QA_PROMPT = "what is the capital of France?"


def store(*sentences: str) -> None:
    for s in sentences:
        parsed = prefer_grammar.parse(s)
        assert parsed.refusal is None, parsed.refusal
        user_rules.add_rules(parsed.rules, s)


def provs(chain):
    return [m.split("/")[0] for m in chain]


async def record(provider: str, model: str, n: int = 1, *, runtime: bool = True, monkeypatch=None) -> None:
    """*n* router-executed calls through the real decision writer."""
    from llm_router import cost

    if runtime:
        monkeypatch.setattr(cost, "_write_provenance", lambda: cost.PROVENANCE_RUNTIME)
    for _ in range(n):
        await cost.log_routing_decision(
            prompt="p", task_type="code", profile="balanced", classifier_type="heuristic",
            classifier_model=None, classifier_confidence=None, classifier_latency_ms=None,
            complexity="simple", recommended_model=model, base_model=model, was_downshifted=False,
            budget_pct_used=None, quality_mode=None, final_model=model, final_provider=provider,
            success=True, input_tokens=10, output_tokens=10, cost_usd=0.0, latency_ms=5.0,
            reason_code="routed")


def put_map(rows: list[tuple[str, str, dict]]) -> None:
    """A fresh balanced map (filters only, never reorders) with the given (chain_id, kind, quota)."""
    from llm_router import resource_map as rm

    res = [{"resource": c, "kind": k, "chain_id": c, "status": "connected", "effective_priority": i + 1,
            "capabilities": ["generate"], "quota": q} for i, (c, k, q) in enumerate(rows)]
    rm.write_map({"schema": rm.SCHEMA, "policy": "balanced", "resources": res,
                  "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())})


@pytest.fixture
def doors(monkeypatch):
    import llm_router.dynamic_routing as dr
    from llm_router.config import RouterConfig
    from llm_router.hooks import chain_builder as hook_cb
    from llm_router.hooks.direct_executor import ModelSpec
    from llm_router.proxy import backends

    monkeypatch.setattr(dr, "get_dynamic_model_chain", lambda profile, task: list(BASE))
    monkeypatch.setattr(hook_cb, "_ollama_models", lambda *a, **k: ([ModelSpec("ollama", "qwen3:8b")], []))
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setattr(backends, "_chain_cache", {})
    monkeypatch.setattr(RouterConfig, "all_ollama_models", lambda self: [LOCAL])
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "qwen3:8b")
    monkeypatch.delenv("OLLAMA_API_BASE", raising=False)

    async def router_chain(task=TaskType.CODE):
        from llm_router.config import get_config
        from llm_router.router import _build_and_filter_chain
        return await _build_and_filter_chain(
            task, RoutingProfile.BALANCED, None, "simple", Complexity.SIMPLE, get_config())

    def hook_chain():
        return [f"{m.provider}/{m.model}" for m in hook_cb.build_chain("simple", "green", "code")]

    async def proxy_chain(prompt=CODE_PROMPT):
        return (await backends.policy_chain(prompt))[2]

    return router_chain, hook_chain, proxy_chain


# ── cap: N calls per rolling 24 h ────────────────────────────────────────────

async def test_day_cap_drops_the_provider_at_every_door_once_reached(doors, temp_db, monkeypatch):
    router_chain, hook_chain, proxy_chain = doors
    store("cap gemini 2/day")
    await record("gemini", GEM, 1, monkeypatch=monkeypatch)
    for chain in (await router_chain(), hook_chain(), await proxy_chain()):
        assert "gemini" in provs(chain), chain          # 1 of 2 used: still allowed
    await record("gemini", GEM, 1, monkeypatch=monkeypatch)
    for chain in (await router_chain(), hook_chain(), await proxy_chain()):
        assert "gemini" not in provs(chain), chain      # 2 of 2 used: the third call goes elsewhere
        assert "ollama" in provs(chain) and "openai" in provs(chain), chain


async def test_day_cap_counts_both_ledger_spellings_and_rolls_after_24h(temp_db, monkeypatch):
    """The ledger holds `codex/gpt-5.4` and bare `gpt-5.4` for the same model; both count."""
    store("cap codex 2/day")
    await record("codex", "codex/gpt-5.4", 1, monkeypatch=monkeypatch)
    await record("codex", "gpt-5.4", 1, monkeypatch=monkeypatch)
    chain = [LOCAL, CODEX, OAI]
    assert map_reader.apply_to_chain(chain) == [LOCAL, OAI]
    rules = user_rules.effective_rules()
    assert [r["kind"] for r in user_rules.cap_hits(rules)] == ["cap"]
    assert user_rules.cap_hits(rules, now=time.time() + 25 * 3600) == []   # window rolled


async def test_day_cap_on_a_model_glob_leaves_the_rest_of_the_provider(temp_db, monkeypatch):
    store("cap claude opus 1/day")
    await record("anthropic", "anthropic/claude-opus-4-5", 1, monkeypatch=monkeypatch)
    chain = ["anthropic/claude-opus-4-5", "anthropic/claude-sonnet-4-5", LOCAL]
    assert map_reader.apply_to_chain(chain) == ["anthropic/claude-sonnet-4-5", LOCAL]


async def test_test_provenance_rows_do_not_count_toward_a_cap(temp_db, monkeypatch):
    store("cap gemini 1/day")
    await record("gemini", GEM, 3, runtime=False)        # the writer stamps these `test`
    assert map_reader.apply_to_chain(BASE) == BASE


async def test_proxy_chain_cache_rebuilds_when_a_cap_is_hit(doors, temp_db, monkeypatch):
    """Map (none) and routing.yaml unchanged between the two calls: only the ledger moved."""
    _, _, proxy_chain = doors
    store("cap gemini 1/day")
    tok = (map_reader.token(), user_rules.routing_path().stat().st_mtime_ns)
    first = await proxy_chain()
    await record("gemini", GEM, 1, monkeypatch=monkeypatch)
    assert (map_reader.token(), user_rules.routing_path().stat().st_mtime_ns) == tok
    second = await proxy_chain()
    assert "gemini" in provs(first) and "gemini" not in provs(second)


async def test_proxy_choose_model_skips_a_capped_provider(doors, temp_db, monkeypatch):
    from llm_router.proxy import backends

    store("cap openai 1/day")
    before = await backends.choose_model(CODE_PROMPT, None)
    await record("openai", OAI, 1, monkeypatch=monkeypatch)
    after = await backends.choose_model(CODE_PROMPT, None)
    assert "openai" in provs(before["chain_head"]), before
    assert "openai" not in provs(after["chain_head"]), after


def test_unreadable_ledger_fails_open_and_is_recorded(monkeypatch, tmp_path):
    from llm_router import failopen

    bad = tmp_path / "test_usage.db"
    bad.write_bytes(b"this is not a sqlite database" * 64)
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(bad))
    seen = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **kw: seen.append(code))
    store("cap gemini 1/day")
    assert map_reader.apply_to_chain(BASE) == BASE
    assert "CHZ-FO-USER-RULES-READ" in seen


def test_no_ledger_is_zero_use_not_a_failure(monkeypatch, tmp_path):
    from llm_router import failopen

    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "absent_test_usage.db"))
    seen = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **kw: seen.append(code))
    store("cap gemini 1/day")
    assert map_reader.apply_to_chain(BASE) == BASE
    assert seen == []


# ── cap: percent of quota ────────────────────────────────────────────────────

@pytest.mark.parametrize("quota, capped", [
    ({"pressure": {"value": 0.85, "provenance": "measured", "as_of": None}}, True),
    ({"pressure": {"value": 0.30, "provenance": "measured", "as_of": None},
      "weekly_pressure": {"value": 0.90, "provenance": "measured", "as_of": None}}, True),
    ({"pressure": {"value": 0.50, "provenance": "estimated", "as_of": None}}, False),
    ({"pressure": {"value": 0.95, "provenance": "default", "as_of": None}}, False),   # unknown, not hit
    ({}, False),
])
def test_pct_cap_reads_the_maps_quota_pressure(quota, capped):
    put_map([("ollama/qwen3:8b", "local", {}), ("codex/codex-subscription", "subscription", quota),
             ("openai/api", "api", {})])
    store("cap codex 80%")
    out = map_reader.apply_to_chain([LOCAL, CODEX, OAI])
    assert ("codex" not in provs(out)) is capped, out
    assert "ollama" in provs(out) and "openai" in provs(out)


def test_pct_cap_without_a_map_is_not_hit():
    store("cap codex 80%")
    assert map_reader.token() is None
    assert map_reader.apply_to_chain([LOCAL, CODEX]) == [LOCAL, CODEX]


# ── only local for <task> ────────────────────────────────────────────────────

async def test_only_local_for_code_leaves_only_loopback_entries_at_every_door(doors):
    router_chain, hook_chain, proxy_chain = doors
    store("only local for code")
    for chain in (await router_chain(TaskType.CODE), hook_chain(), await proxy_chain(CODE_PROMPT)):
        assert chain and provs(chain) == ["ollama"] * len(chain), chain


async def test_only_local_for_code_does_not_touch_a_query_chain(doors):
    router_chain, _, proxy_chain = doors
    base = [await router_chain(TaskType.QUERY), await proxy_chain(QA_PROMPT)]
    store("only local for code")
    after = [await router_chain(TaskType.QUERY), await proxy_chain(QA_PROMPT)]
    assert after == base and all("openai" in provs(c) for c in after), after


async def test_only_local_with_no_loopback_model_gives_the_empty_chain(doors, monkeypatch):
    """A remote OLLAMA_API_BASE means no entry stays on this machine: the existing
    nothing-routable path (an empty chain, the floor filtered too) takes over."""
    router_chain, hook_chain, proxy_chain = doors
    monkeypatch.setenv("OLLAMA_API_BASE", "http://gpu-box.example:11434")
    store("only local for code")
    assert await router_chain(TaskType.CODE) == []
    assert hook_chain() == []
    assert await proxy_chain(CODE_PROMPT) == []


def test_only_local_drops_an_ollama_cloud_tag():
    store("only local for code")
    chain = ["ollama/gpt-oss:120b-cloud", LOCAL, OAI]
    assert map_reader.apply_to_chain(chain, task="code") == [LOCAL]
    assert map_reader.apply_to_chain(chain, task="query") == chain


# ── no paid API ──────────────────────────────────────────────────────────────

async def test_no_paid_api_drops_api_key_providers_at_every_door(doors, monkeypatch):
    router_chain, hook_chain, proxy_chain = doors
    store("no paid API")
    for chain in (await router_chain(), hook_chain(), await proxy_chain()):
        assert provs(chain) == ["ollama"] * len(chain) and chain, chain


def test_no_paid_api_keeps_local_and_subscriptions_and_claude_only_in_subscription_mode(monkeypatch):
    store("no paid API")
    chain = [LOCAL, CODEX, "gemini_cli/gemini-2.5-pro", "anthropic/claude-sonnet-4-5", GEM, OAI,
             "deepseek/deepseek-chat"]
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "false")
    import llm_router.config as config_module
    monkeypatch.setattr(config_module, "_config", None)
    assert map_reader.apply_to_chain(chain) == [LOCAL, CODEX, "gemini_cli/gemini-2.5-pro"]
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
    monkeypatch.setattr(config_module, "_config", None)
    assert map_reader.apply_to_chain(chain) == [LOCAL, CODEX, "gemini_cli/gemini-2.5-pro",
                                                "anthropic/claude-sonnet-4-5"]


def test_every_registry_provider_is_a_paid_api(monkeypatch):
    """The definition is pinned to the provider registry: each entry there is reached with an
    API key, so each is paid (anthropic only outside the subscription mode)."""
    from llm_router.tools.setup import _PROVIDER_REGISTRY

    monkeypatch.delenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", raising=False)
    monkeypatch.setitem(__import__("sys").modules, "llm_router.config", None)
    assert len(_PROVIDER_REGISTRY) >= 5
    assert all(user_rules.is_paid_api(p) for p in _PROVIDER_REGISTRY), \
        [p for p in _PROVIDER_REGISTRY if not user_rules.is_paid_api(p)]
    assert not any(user_rules.is_paid_api(p) for p in user_rules.UNPAID_PROVIDERS)


# ── pins and the excluded proxy clause ───────────────────────────────────────

async def test_pinned_chain_still_loses_capped_paid_and_non_local_entries(temp_db, monkeypatch):
    """Exclusions apply to a pin like a ban (UN-01); only reordering is skipped."""
    store("cap gemini 1/day")
    await record("gemini", GEM, 1, monkeypatch=monkeypatch)
    assert map_reader.apply_to_chain([OAI, GEM, LOCAL], reorder=False) == [OAI, LOCAL]
    store("only local for code")
    assert map_reader.apply_to_chain([OAI, GEM, LOCAL], reorder=False, task="code") == [LOCAL]
    user_rules.write_rules([])
    store("no paid API")
    assert map_reader.apply_to_chain([OAI, CODEX, LOCAL], reorder=False) == [CODEX, LOCAL]


async def test_proxy_choose_model_holds_a_configured_pin_to_the_rules(doors, temp_db, monkeypatch):
    """`choose_model(text, pinned)` used to return the pin unfiltered: a ban, a hit cap and
    only-local now keep the call on Claude (model None), as at the router door. The chain
    itself (qwen3:8b) stays allowed, so only the pin check can produce None."""
    from llm_router.proxy import backends

    pin, cloud_pin = "ollama/pinned:7b", "ollama/pinned:7b-cloud"
    glob = {"providers": ["ollama"], "model": "pinned*"}
    assert (await backends.choose_model(CODE_PROMPT, pin))["model"] == pin               # control
    assert (await backends.choose_model(CODE_PROMPT, cloud_pin))["model"] == cloud_pin   # control
    user_rules.write_rules([{"id": "p1", "kind": "ban", "target": glob, "text": "t"}])
    assert (await backends.choose_model(CODE_PROMPT, pin))["model"] is None
    user_rules.write_rules([{"id": "p1", "kind": "cap", "target": glob, "unit": "day", "n": 1, "text": "t"}])
    assert (await backends.choose_model(CODE_PROMPT, pin))["model"] == pin               # 0 of 1 used
    await record("ollama", pin, 1, monkeypatch=monkeypatch)
    assert (await backends.choose_model(CODE_PROMPT, pin))["model"] is None
    user_rules.write_rules([])
    store("only local for code")
    assert (await backends.choose_model(CODE_PROMPT, cloud_pin))["model"] is None
    assert (await backends.choose_model(CODE_PROMPT, pin))["model"] == pin


async def test_proxy_local_agent_pin_is_held_to_the_rules(tmp_path):
    """`--serve local-agent` serves its pin without consulting the policy; a stored ban on that
    pin now forwards the call to Claude (policy_kept) instead of serving it."""
    import json
    from pathlib import Path

    import httpx

    from llm_router.proxy import ledger
    from llm_router.proxy import server as ps
    from llm_router.proxy.translate import from_ollama

    body = json.loads((Path(__file__).parent / "fixtures" / "proxy" / "continuation_request.json").read_text())
    body["messages"] = body["messages"][:2]
    reply = {"message": {"role": "assistant", "content": "",
                         "tool_calls": [{"function": {"name": "Bash", "arguments": {"command": "ls"}}}]},
             "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 5}

    class Backend:
        calls = 0

        async def complete(self, b, timeout_s):
            Backend.calls += 1
            m, err = from_ollama(reply, b)
            return m, err, {"prompt_tokens": 10, "output_tokens": 2}

    async def run(tag):
        upstream = []

        def up(request):
            upstream.append(request)
            return httpx.Response(200, stream=httpx.ByteStream(b'{"type":"message"}'),
                                  headers={"content-type": "application/json"})
        ledger_path = tmp_path / f"{tag}.jsonl"
        config = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=ledger_path,
                                kill_switch=tmp_path / "kill", model="ollama/fake:1", trim="none",
                                serve="local-agent", hedge_s=None, warm_up=False)
        app = ps.build_app(config, client=httpx.AsyncClient(transport=httpx.MockTransport(up)),
                           backend_factory=lambda m: Backend(),
                           breaker_fn=lambda lever, task: type("D", (), {"allowed": True, "reason": "t"})())
        headers = {"authorization": "Bearer sk-ant-oat01-" + "Zq9" * 20, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787") as c:
            await c.post("/v1/messages?beta=true", content=json.dumps(body), headers=headers)
        return upstream, ledger.read_rows(ledger_path)

    upstream, rows = await run("control")
    assert upstream == [] and rows[0]["decision"] == "served" and Backend.calls == 1
    store("never ollama")
    upstream, rows = await run("banned")
    assert len(upstream) == 1 and rows[0]["decision"] != "served" and Backend.calls == 1


def test_no_paid_api_keeps_openai_compat_only_on_a_loopback_base(monkeypatch):
    import llm_router.config as config_module

    store("no paid API")
    chain = [LOCAL, "openai_compat/my-model"]
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setattr(config_module, "_config", None)
    assert map_reader.apply_to_chain(chain) == chain
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://openrouter.example/api/v1")
    monkeypatch.setattr(config_module, "_config", None)
    assert map_reader.apply_to_chain(chain) == [LOCAL]


async def test_cap_reading_is_reused_until_the_ledger_changes_or_the_ttl_ends(temp_db, monkeypatch):
    from llm_router import ledger_view

    store("cap gemini 2/day")
    await record("gemini", GEM, 1, monkeypatch=monkeypatch)
    reads = []
    real = ledger_view.router_executed_models
    monkeypatch.setattr(ledger_view, "router_executed_models", lambda *a, **k: reads.append(1) or real(*a, **k))
    rules = user_rules.effective_rules()
    assert user_rules.cap_hits(rules) == [] and user_rules.cap_hits(rules) == []
    assert len(reads) == 1                                   # second call: cached
    await record("gemini", GEM, 1, monkeypatch=monkeypatch)  # the ledger files change
    assert [r["kind"] for r in user_rules.cap_hits(rules)] == ["cap"] and len(reads) == 2
    clock = [user_rules.time.monotonic() + user_rules.CAP_CACHE_TTL_S + 1]
    monkeypatch.setattr(user_rules.time, "monotonic", lambda: clock[0])
    user_rules.cap_hits(rules)
    assert len(reads) == 3                                   # TTL over: read again


async def test_day_cap_counts_rows_whatever_the_timestamp_spelling(temp_db, monkeypatch):
    """The ledger read pre-filters on the raw timestamp text (so the index can seek); a row
    written with a ``T`` separator or a UTC offset inside the window must still count."""
    import sqlite3
    from datetime import datetime, timedelta, timezone

    store("cap gemini 4/day")
    await record("gemini", GEM, 4, monkeypatch=monkeypatch)
    now = datetime.now(timezone.utc)
    spellings = [(now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),
                 (now - timedelta(hours=2)).astimezone(timezone(timedelta(hours=-10))).isoformat(timespec="seconds"),
                 (now - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"),
                 # 30 min after the window opens, in an offset that sorts before the UTC cutoff as text
                 # (a raw-text lower bound and a date-only pre-filter both lose this row)
                 (now - timedelta(hours=24) + timedelta(minutes=30)).astimezone(
                     timezone(timedelta(hours=-10))).isoformat(timespec="seconds")]
    with sqlite3.connect(temp_db) as con:
        ids = [r[0] for r in con.execute("SELECT id FROM routing_decisions ORDER BY id")]
        for rid, ts in zip(ids, spellings):
            con.execute("UPDATE routing_decisions SET timestamp = ? WHERE id = ?", (ts, rid))
    assert user_rules.cap_hits(user_rules.effective_rules(), now=now.timestamp())


def test_tier_classify_pure_reads_no_ledger(monkeypatch):
    """Lane A's pure classifier is documented side-effect-free, sync and CPU-only: with a day
    cap stored it must not open the ledger or record a fail-open."""
    from llm_router import failopen, ledger_view
    from llm_router.proxy import backends

    store("cap gemini 1/day")
    touched = []
    monkeypatch.setattr(ledger_view, "router_executed_models", lambda *a, **k: touched.append("ledger"))
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **kw: touched.append(code))
    backends.tier_classify_pure(CODE_PROMPT)
    assert touched == []


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="proxy/tiers.py — owned by the lane-A/tiers session: a cap on a "
                   "Claude model at the proxy door is a tier decision made there, which this PR does not touch")
async def test_proxy_tier_decision_honours_a_claude_cap(temp_db, monkeypatch):
    from llm_router.proxy import tiers

    policy = tiers.ClaudeTierPolicy.load(None)
    opus = next(t for t in policy.by_name.values() if "opus" in (t.model or ""))
    store("cap opus 1/day")
    await record("anthropic", f"anthropic/{opus.model}", 1, monkeypatch=monkeypatch)
    assert tiers.tier_model(opus.name) is None           # a capped model must not be served as a tier
