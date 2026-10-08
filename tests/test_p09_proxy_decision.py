"""PLAN v16 P0.9 task 6: the proxy's tier decision stays under the PRD's 50 ms
heuristic bar, and the ledger names where its time went.

Live, rules-only turn-first decisions took 1.2-2.2 s on 3 of 6 rows [PL]. The
plan's first hypothesis was an uncached quota-file read. It was not: the quota
read is one ``stat()`` per call (``proxy.quota_pressure._load``). The cause was
the classifier the decision called, ``backends.choose_model``, which builds a
provider chain (``router._build_and_filter_chain``: usage.db queries, dynamic
routing, the Ollama model list) on every (task_type, complexity) cache miss.
Measured on a copy of the live usage.db (2026-10-07): the first build 21.6 s,
each later new key 67-107 ms; ``classify_signals`` itself 0.1-0.2 ms. The tier
decision reads only the class, so it now classifies without building a chain.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from llm_router.proxy import backends as pb
from llm_router.proxy import quota_pressure
from llm_router.proxy import tiers as pt
from llm_router.proxy.cache_cost import Stickiness
from tests.test_proxy_tiers import SID, Upstream, _app, _first, _post, _raw_policy, _req, _rows

TEXTS = [
    "explain how the cache works in this module",
    "write a function that parses ISO dates and add tests",
    "research the latest release notes for the http client",
    "fix the typo in the README",
    "design a migration plan for the billing database across three services",
]


@pytest.fixture
def slow_chain(monkeypatch):
    """A provider-chain build that takes 1 s, like a cold one live."""
    from llm_router import router

    calls = []

    async def build(*args, **kwargs):
        calls.append(args)
        await asyncio.sleep(1.0)
        return ["anthropic/claude-sonnet-5-5", "ollama/q:1"]

    monkeypatch.setattr(router, "_build_and_filter_chain", build)
    monkeypatch.setattr(pb, "_chain_cache", {})
    # Warm the rules classifier's imports and lazy tables outside the timed span,
    # as the proxy's own warm_up does at start.
    from llm_router.classify import GATEWAY_POLICY, classify_signals

    classify_signals("warm up", GATEWAY_POLICY)
    return calls


async def test_a_turn_first_decision_never_waits_on_a_chain_build(slow_chain, monkeypatch):
    """The call count is the deterministic guard. The 50 ms bar is checked on the
    fastest of 5 cold decisions (empty chain cache each time), so a loaded runner's
    one-off stall does not fail it, while a decision that waits on the 1 s build
    would fail all 5."""
    policy = pt.ClaudeTierPolicy.from_dict(_raw_policy())  # the real default classifier
    times, d = [], None
    for _ in range(5):
        monkeypatch.setattr(pb, "_chain_cache", {})
        t = time.perf_counter()
        d = await policy.decide(_req(), SID, Stickiness())
        times.append((time.perf_counter() - t) * 1000.0)
    assert d.task_type and d.complexity
    assert slow_chain == [], "the tier decision built a provider chain"
    assert min(times) < 50.0, f"fastest decision {min(times):.0f} ms (PRD heuristic bar 50 ms) {d.phases_ms}"


async def test_tier_classify_gives_the_same_class_as_choose_model(monkeypatch):
    from llm_router import router

    async def build(*args, **kwargs):
        return ["anthropic/claude-sonnet-5-5", "ollama/q:1"]

    monkeypatch.setattr(router, "_build_and_filter_chain", build)
    monkeypatch.setattr(pb, "_chain_cache", {})
    for text in TEXTS:
        fast = await pb.tier_classify(text, None, anthropic=True)
        full = await pb.choose_model(text, None, anthropic=True)
        assert (fast["task_type"], fast["complexity"]) == (full["task_type"], full["complexity"]), text
        # choose_model built and cached the chain; tier_classify now reads it.
        again = await pb.tier_classify(text, None, anthropic=True)
        assert again["chain_head"] == full["chain_head"] and again["model"] == full["model"]


async def test_tier_classify_reports_no_chain_rather_than_building_one(slow_chain):
    out = await pb.tier_classify(TEXTS[0], None, anthropic=True)
    assert out["chain_head"] == [] and out["model"] is None
    assert slow_chain == []


async def test_the_decision_names_its_phases(slow_chain):
    policy = pt.ClaudeTierPolicy.from_dict(_raw_policy())
    d = await policy.decide(_req(), SID, Stickiness())
    assert set(pt.DECISION_PHASES) <= set(d.phases_ms), d.phases_ms
    assert all(isinstance(v, float) and v >= 0 for v in d.phases_ms.values())


async def test_an_early_keep_names_only_the_phases_that_ran():
    policy = pt.ClaudeTierPolicy.from_dict(_raw_policy())
    body = _req(model="gpt-5.5")  # not a Claude tier: kept before anything else runs
    d = await policy.decide(body, SID, Stickiness())
    assert d.reason == pt.REASON_UNKNOWN_MODEL
    assert set(d.phases_ms) == {"quota_read"}


async def test_the_ledger_row_carries_tier_phases_ms_names_and_numbers_only(tmp_path, monkeypatch):
    async def choose(text, pinned, *, anthropic=False):
        return {"task_type": "code", "complexity": "moderate", "chain_head": [], "model": None}

    monkeypatch.setattr(pb, "tier_classify", choose)
    app = _app(tmp_path, Upstream())
    assert (await _post(app, _first())).status_code == 200
    assert (await _post(app, _req())).status_code == 200
    first, second = _rows(tmp_path)
    assert set(second["tier_phases_ms"]) >= set(pt.DECISION_PHASES), second["tier_phases_ms"]
    assert all(isinstance(k, str) and isinstance(v, (int, float)) for k, v in second["tier_phases_ms"].items())
    assert "classify" not in first["tier_phases_ms"]  # first call: kept, never classified


async def test_a_slow_chain_build_does_not_reach_tier_decision_s(tmp_path, slow_chain):
    """End to end through the proxy: the row's own decision time does not include
    the 1 s chain build. The bound is 500 ms, not the 50 ms bar, so one loaded-runner
    stall cannot fail it; the 50 ms bar is checked (fastest of 5) above."""
    app = _app(tmp_path, Upstream())
    await _post(app, _first())
    await _post(app, _req())
    row = _rows(tmp_path)[-1]
    decision_ms = sum(row["tier_phases_ms"][k] for k in pt.DECISION_PHASES if k in row["tier_phases_ms"])
    assert row["tier_decision_s"] < 0.5 and decision_ms < 500.0, row["tier_phases_ms"]
    assert slow_chain == []


def test_the_quota_read_is_one_stat_while_usage_json_is_unchanged(tmp_path, monkeypatch):
    """The plan's first hypothesis, checked: an unchanged usage.json is parsed
    once, so the quota read is not where the 1-2 s went."""
    usage = tmp_path / "usage.json"
    usage.write_text('{"session_pct": 40, "weekly_pct": 70, "updated_at": %d}' % int(time.time()))
    monkeypatch.setattr(quota_pressure, "_cache", {})
    reads = []
    real = type(usage).read_text

    def counting(self, *a, **k):
        reads.append(self)
        return real(self, *a, **k)

    monkeypatch.setattr(type(usage), "read_text", counting)
    for _ in range(50):
        assert quota_pressure.read(usage).state == quota_pressure.STATE_OK
    assert len(reads) == 1
