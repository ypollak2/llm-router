"""Lane A PR3 (plan W1-4; D-285, D-292, D-293): the Frontier shadow sampler (fill phase, then 2%), the lane tags,
the ``lane_quality`` shadow_pairs writer, the KPI split, and the carry-ins of the #731 review (S1-S3, N1-N5,
trip visibility). DEFAULT OFF: with the lane off there is no sampler, no shadow call and no new file.

Anthropic is a mocked upstream and every Frontier call is a fake: no test makes a network call.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import stat
import time
from pathlib import Path

import httpx
import pytest

from llm_router import eligibility, lane_a_shadow, lane_quality, lane_quality_writer as lqw, paths
from llm_router import quality_breaker as qb
from llm_router import offload_share as osh
from llm_router import shadow_frontier as sf
from llm_router.proxy import haiku_guard, lane_a, lane_a_breaker as lb, ledger
from llm_router.proxy import tiers as pt
from llm_router.proxy.cache_cost import Stickiness
from tests._o3_fixture import NOW as KPI_NOW, proxy_row
from tests.test_proxy_lane_a import HAIKU, LANE_ON, OFF_LANE, OPUS, _app, _body, _policy, _post, _raw, _rules
from tests.test_proxy_lane_a_breaker import SSE, Clock, Upstream, _breaker, _good_sse, _sse
from tests.test_shadow_frontier import CANARY_CHEAP, CANARY_PROMPT, CHEAP_REPLY, CHEAP_USAGE, NOW, RAW, FakeReplay

DAY = "2026-10-08"
BR = lane_a.BreakerConfig()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(h))
    monkeypatch.delenv("LLM_ROUTER_HAIKU_GUARD_SHADOW_VERDICTS", raising=False)
    monkeypatch.delenv(sf.ENV_FLAG, raising=False)
    monkeypatch.delenv(lane_a.ENV_SWITCH, raising=False)
    return h


@pytest.fixture
def on(home, monkeypatch):
    monkeypatch.setenv(sf.ENV_FLAG, "on")
    return home


def _usage(updated=None, session=10.0, weekly=20.0):
    p = paths.state_path("usage.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"session_pct": session, "weekly_pct": weekly,
                             "updated_at": NOW if updated is None else updated}))


def _ctx(session="sess-a", n=3, group="tech_op", **kw):
    return {"lane": "A", "session_id": session, "msg_count": n, "task_group": group,
            "prev_tool_class": "technical_op", "conf": 0.67, **kw}


def _shadow(rng=0.0) -> sf.FrontierShadow:
    return sf.FrontierShadow(rng=lambda: rng, clock=lambda: NOW)


async def _sample(fs, replay, lane=None, *, task_id="msg_1", served=HAIKU, requested=OPUS, usage=CHEAP_USAGE):
    fs.maybe_sample(RAW, served, requested, sf.DOOR_PROXY, task_id, replay=replay, cheap_status=200,
                    cheap_reply=CHEAP_REPLY, cheap_ctype="text/event-stream", cheap_usage=usage, lane=lane)
    await fs.drain()


def _ledger() -> list[dict]:
    return haiku_guard.read_jsonl(sf.ledger_path())


def _verdict(i, *, group="tech_op", a=True, f=True, cj=False, ts=None, phase="fill", lane="A", **kw):
    row = {"ts": NOW - 100 - i if ts is None else ts, "task_id": f"t{i}", "acceptable": None if cj else a,
           "frontier_acceptable": None if cj else f, "cannot_judge": cj, "judge_model": "sonnet",
           "judge_brief_sha256": sf.JUDGE_BRIEF_SHA256, "lane": lane, "task_group": group, "rate_phase": phase,
           "served_body_sha": "sha-x", "replay_body_sha": "sha-x"}
    row.update(kw)
    if lane is None:
        for k in ("lane", "task_group", "rate_phase", "judge_brief_sha256", "served_body_sha", "replay_body_sha"):
            row.pop(k)
    return row


def _write_verdicts(rows):
    for r in rows:
        sf._append_private(sf.verdicts_path(), r)


def _step_with_key(below, group_rate=0.02, session="sess-a"):
    """A msg_count whose deterministic key is below (True) or at/above (False) ``group_rate``."""
    for n in range(10_000):
        u = lane_a_shadow.sample_key(session, n)
        if (u < group_rate) == below:
            return n
    raise AssertionError("no key found")


# ── the sampler: key, rate, fill then 2% ─────────────────────────────────────

def test_constants_are_the_prereg_values():
    c = lane_a_shadow.CONSTANTS
    assert (c["FILL_RATE"], c["STEADY_RATE"], c["FILL_UNTIL_DECIDED_PAIRS"]) == (1.0, 0.02, 60)
    assert (c["LANE_A_CALLS_PER_DAY"], c["LANE_A_INPUT_TOKENS_PER_DAY"]) == (12, 400_000)  # D-293
    assert (sf.MAX_CALLS_PER_DAY, sf.MAX_INPUT_TOKENS_PER_DAY) == (20, 400_000)  # the global cap is NOT raised
    assert c["LANE_A_CALLS_PER_DAY"] <= sf.MAX_CALLS_PER_DAY
    assert c["LANE_A_INPUT_TOKENS_PER_DAY"] <= sf.MAX_INPUT_TOKENS_PER_DAY


def test_phase_flips_at_exactly_the_60th_decided_pair():
    assert lane_a_shadow.phase_for(0) == (1.0, "fill")
    assert lane_a_shadow.phase_for(59) == (1.0, "fill")
    assert lane_a_shadow.phase_for(60) == (0.02, "steady")
    assert lane_a_shadow.phase_for(500) == (0.02, "steady")


def test_the_key_is_deterministic_content_blind_and_needs_both_ids():
    a = lane_a_shadow.sample_key("s", 7)
    assert a == lane_a_shadow.sample_key("s", 7) and 0.0 <= a < 1.0
    assert a != lane_a_shadow.sample_key("s", 8) and a != lane_a_shadow.sample_key("t", 7)
    assert a != lane_a_shadow.sample_key("s", 7, salt="other")
    for bad in ((None, 3), ("", 3), ("s", None), ("s", True), ("s", -1), ("s", "3")):
        assert lane_a_shadow.sample_key(*bad) is None


def test_steady_rate_is_within_half_a_point_at_10k_steps():
    hits = sum(lane_a_shadow.sample_key("sess-rate", n) < 0.02 for n in range(10_000))
    assert abs(hits / 10_000 - 0.02) <= 0.005, hits


def test_decided_pairs_count_only_lane_a_tagged_decided_rows_of_the_group_in_the_window():
    since, until = NOW - 14 * 86400.0, NOW
    rows = [_verdict(1), _verdict(2, group="code"), _verdict(3, cj=True), _verdict(4, lane=None),
            _verdict(5, phase=None), _verdict(6, ts=since - 1), _verdict(7, a=False, f=False),
            dict(_verdict(8), acceptable=None)]
    assert lane_a_shadow.count_decided(rows, "tech_op", since=since, until=until) == 2
    assert lane_a_shadow.count_decided(rows, "code", since=since, until=until) == 1


async def test_fill_reaches_60_decided_pairs_per_task_group_then_drops_to_2_percent(on):
    _usage()
    # 59 decided pairs for tech_op: still fill, every step is replayed.
    _write_verdicts([_verdict(i) for i in range(59)])
    rep = FakeReplay()
    fs = _shadow()
    for n in (3, 5, 7, 9):
        await _sample(fs, rep, _ctx(n=n), task_id=f"m{n}")
    assert len(rep.calls) == 4
    rows = _ledger()
    assert [(r["outcome"], r["rate_phase"], r["rate"]) for r in rows] == [("replayed", "fill", 1.0)] * 4
    # the 60th decided pair: steady. A key at or above 2% is skipped, one below it is replayed.
    _write_verdicts([_verdict(59)])
    skip_n, hit_n = _step_with_key(False), _step_with_key(True)
    await _sample(fs, rep, _ctx(n=skip_n), task_id="skip")
    await _sample(fs, rep, _ctx(n=hit_n), task_id="hit")
    tail = _ledger()[-2:]
    assert (tail[0]["outcome"], tail[0]["reason"], tail[0]["rate_phase"], tail[0]["rate"]) == (
        "skipped", "not_sampled", "steady", 0.02)
    assert (tail[1]["outcome"], tail[1]["rate_phase"]) == ("replayed", "steady")
    assert len(rep.calls) == 5
    # another task group has its own count: still fill
    await _sample(fs, rep, _ctx(n=skip_n, group="code"), task_id="other")
    last = _ledger()[-1]
    assert (last["task_group"], last["outcome"], last["rate_phase"]) == ("code", "replayed", "fill")


async def test_a_verdict_read_failure_keeps_the_phase_it_last_read(on, monkeypatch):
    _usage()
    _write_verdicts([_verdict(i) for i in range(60)])
    fs, rep = _shadow(), FakeReplay()
    n = _step_with_key(False)
    await _sample(fs, rep, _ctx(n=n), task_id="a")
    assert _ledger()[-1]["rate_phase"] == "steady"
    monkeypatch.setattr(lane_a_shadow, "read_verdict_rows", lambda p: None)  # unreadable now
    await _sample(fs, rep, _ctx(n=n), task_id="b")
    last = _ledger()[-1]
    assert (last["rate_phase"], last["reason"]) == ("steady", "not_sampled")  # not reset to the fill rate
    assert rep.calls == []


async def test_a_step_without_a_key_is_skipped_not_guessed(on):
    _usage()
    rep = FakeReplay()
    await _sample(_shadow(), rep, _ctx(session=None), task_id="nokey")
    assert rep.calls == [] and _ledger()[0]["reason"] == "no_key"


# ── the bucket: 12 calls / 400k tokens inside the global cap ─────────────────

def _seed(n, lane, *, tokens=1000, day=DAY):
    for i in range(n):
        sf._append_private(sf.ledger_path(), {"day": day, "outcome": "replayed", "input_tokens": tokens,
                                              "task_id": f"seed{lane}{i}", **({"lane": "A"} if lane else {})})


async def test_the_lane_a_bucket_stops_at_12_calls_a_day_inside_the_global_cap(on):
    _usage()
    _seed(11, True)
    rep, fs = FakeReplay(), _shadow()
    await _sample(fs, rep, _ctx(), task_id="twelfth")
    assert len(rep.calls) == 1 and _ledger()[-1]["outcome"] == "replayed"
    await _sample(fs, rep, _ctx(n=5), task_id="thirteenth")
    last = _ledger()[-1]
    assert (last["outcome"], last["reason"], last["lane"]) == ("skipped", "lane_a_bucket", "A")
    assert len(rep.calls) == 1


async def test_the_arm_keeps_its_own_room_and_the_global_cap_still_binds_both(on):
    _usage()
    _seed(12, True)          # lane A used its whole bucket
    _seed(7, False)          # 19 of the global 20
    rep, fs = FakeReplay(), _shadow()
    await _sample(fs, rep, None, task_id="arm1")                      # the arm may take call 20
    assert _ledger()[-1]["outcome"] == "replayed" and "lane" not in _ledger()[-1]
    await _sample(fs, rep, None, task_id="arm2")                      # and not 21
    assert _ledger()[-1]["reason"] == "cap_calls"
    await _sample(fs, rep, _ctx(), task_id="lane")                    # lane A: the global cap is reported first
    assert _ledger()[-1]["reason"] == "cap_calls"
    assert len(rep.calls) == 1


async def test_global_cap_wins_over_the_bucket_when_lane_a_is_below_12(on):
    _usage()
    _seed(10, True)
    _seed(10, False)         # 20 calls today: global cap, lane A has only used 10
    rep = FakeReplay()
    await _sample(_shadow(), rep, _ctx(), task_id="x")
    assert _ledger()[-1]["reason"] == "cap_calls" and rep.calls == []


async def test_the_lane_a_token_bucket_counts_lane_a_replays_only(on, monkeypatch):
    _usage()
    monkeypatch.setattr(lane_a_shadow, "BUCKET_INPUT_TOKENS_PER_DAY", 100_000)  # below the global cap, to see it bind
    _seed(1, True, tokens=99_000)
    _seed(1, False, tokens=250_000)                   # the arm's tokens do not use up lane A's bucket
    rep = FakeReplay()
    await _sample(_shadow(), rep, _ctx(), task_id="y", usage={"input_tokens": 1000})  # 1,350 est > 1,000 left
    assert (_ledger()[-1]["reason"], rep.calls) == ("lane_a_bucket", [])
    await _sample(_shadow(), rep, _ctx(n=9), task_id="z", usage={"input_tokens": 500})  # 675 <= 1,000 left
    assert _ledger()[-1]["outcome"] == "replayed"


# ── never runs unless lane A SERVED the step ─────────────────────────────────

def test_a_lane_context_exists_only_for_a_step_lane_a_served():
    body = _body(sid="s")
    served = {"lane_a_reason": "served", "session_id": "s", "prev_tool_class": "technical_op",
              "tier_task_type": "query", "lane_a_conf": 0.7}
    ctx = lane_a_shadow.context_from_row(served, body)
    assert ctx == {"lane": "A", "session_id": "s", "msg_count": 3, "task_group": "tech_op",
                   "prev_tool_class": "technical_op", "conf": 0.7, "served_body_sha": None}
    for reason in (None, "eligible", "retried_original", "off", "breaker_open_reserved", "error", "low_conf"):
        assert lane_a_shadow.context_from_row(dict(served, lane_a_reason=reason), body) is None, reason
    assert "prompt" not in json.dumps(ctx).lower() and CANARY_PROMPT not in json.dumps(ctx)


async def test_a_non_lane_context_is_treated_as_the_arm_and_never_as_lane_a(on):
    _usage()
    rep = FakeReplay()
    await _sample(_shadow(), rep, {"lane": "B", "task_group": "x"}, task_id="m")
    row = _ledger()[0]
    assert "lane" not in row and "rate_phase" not in row


def test_the_task_group_follows_the_s1_rule():
    for tt in ("query", "code", "generate", None):
        for prev in ("technical_op", "read", None):
            assert lane_a_shadow.task_group_of(tt, prev) == osh._task_group(tt, prev)


async def test_flag_off_means_no_lane_a_task_no_call_and_no_file(home):
    _usage()
    rep, fs = FakeReplay(), _shadow()
    assert fs.maybe_sample(RAW, HAIKU, OPUS, sf.DOOR_PROXY, "m", replay=rep, cheap_reply=CHEAP_REPLY,
                           cheap_ctype="text/event-stream", cheap_usage=CHEAP_USAGE, lane=_ctx()) is None
    await fs.drain()
    assert rep.calls == [] and not sf.shadow_dir().exists()


# ── the proxy: tags, background, off ─────────────────────────────────────────

def _usage_events():
    ev = _good_sse()
    ev[0] = {"type": "message_start", "message": {"id": "m", "content": [], "usage": {
        "input_tokens": 100, "cache_read_input_tokens": 800, "cache_creation_input_tokens": 0, "output_tokens": 1}}}
    return ev


GOOD = _sse(_usage_events())


class UpstreamU(Upstream):
    """``Upstream`` whose good replies carry usage (the shadow needs the input size to admit a replay)."""

    def __call__(self, request):
        self.requests.append(request)
        model = json.loads(request.content)["model"]
        if "haiku" in model and self.haiku != 200:
            return httpx.Response(self.haiku, stream=httpx.ByteStream(b'{"type": "error"}'),
                                  headers={"content-type": "application/json"})
        return httpx.Response(200, stream=httpx.ByteStream(GOOD), headers={"content-type": SSE})


class Slow(Upstream):
    """Like ``Upstream`` but the Opus replay (the second Opus-bound call) waits on ``gate``."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.gate = asyncio.Event()

    async def __call__(self, request):
        self.requests.append(request)
        model = json.loads(request.content)["model"]
        if "haiku" not in model:
            await self.gate.wait()
        return httpx.Response(200, stream=httpx.ByteStream(GOOD), headers={"content-type": SSE})


async def _wait_for(pred, n=300):
    for _ in range(n):
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_a_lane_a_serve_is_tagged_replayed_in_the_background_and_paired_with_tags(on, tmp_path, monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    _usage(updated=time.time())
    up = UpstreamU()
    app = _app(tmp_path, _raw(), up)
    body = _body(sid="sess-px")
    r = await _post(app, body)
    assert r.status_code == 200
    await app.state.frontier_shadow.drain()
    assert [json.loads(q.content)["model"] for q in up.requests] == [HAIKU, OPUS]
    assert up.requests[1].content == json.dumps(body).encode()                     # the client's own bytes
    (row,) = ledger.read_rows(tmp_path / "proxy_calls.jsonl")
    assert (row["lane_a_reason"], row["tier_reason"], row["lane"]) == ("served", "haiku_rewrite", "A")
    (srow,) = _ledger()
    assert (srow["outcome"], srow["lane"], srow["task_group"], srow["rate_phase"], srow["rate"]) == (
        "replayed", "A", "tech_op", "fill", 1.0)
    assert (srow["session_id"], srow["msg_count"], srow["task_id"]) == ("sess-px", 3, row["msg_id"])
    pairs = [json.loads(line) for f in sf.pairs_dir().glob("*.jsonl") for line in f.read_text().splitlines()]
    assert len(pairs) == 1 and pairs[0]["lane"] == "A" and pairs[0]["task_group"] == "tech_op"
    assert stat.S_IMODE(next(sf.pairs_dir().glob("*.jsonl")).stat().st_mode) == 0o600


@pytest.mark.timing
async def test_a_stalled_frontier_replay_does_not_delay_the_client_reply(on, tmp_path, monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    _usage(updated=time.time())
    up = Slow()
    app = _app(tmp_path, _raw(), up)
    t0 = time.monotonic()
    r = await asyncio.wait_for(_post(app, _body(sid="sess-slow")), 10)   # would hang if the reply waited on the replay
    dt = time.monotonic() - t0
    assert r.status_code == 200 and dt < 2.0
    assert await _wait_for(lambda: len(up.requests) == 2)               # the replay started, and is still blocked
    assert not sf.ledger_path().exists()
    up.gate.set()
    await app.state.frontier_shadow.drain()
    assert _ledger()[0]["outcome"] == "replayed"


async def test_a_retried_lane_a_step_is_not_tagged_and_not_paired(on, tmp_path, monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    _usage(updated=time.time())
    up = UpstreamU(503)
    app = _app(tmp_path, _raw(), up)
    assert (await _post(app, _body(sid="sess-r"))).status_code == 200
    await app.state.frontier_shadow.drain()
    (row,) = ledger.read_rows(tmp_path / "proxy_calls.jsonl")
    assert row["lane_a_reason"] == lane_a.R_RETRIED and "lane" not in row
    assert [json.loads(q.content)["model"] for q in up.requests] == [HAIKU, OPUS]   # no replay
    assert [r["reason"] for r in _ledger()] == ["not_rewritten"]


@pytest.mark.parametrize("lane,env", [(None, "on"), (OFF_LANE, "on"), (LANE_ON, "off"), (LANE_ON, None)])
async def test_lane_off_has_no_sampler_no_shadow_call_and_no_new_file(on, tmp_path, monkeypatch, lane, env):
    """Shadow flag ON, lane not fully on: nothing replays, no shadow/lane_quality/breaker file appears, and
    the continuation rows carry no ``lane`` tag."""
    if env is None:
        monkeypatch.delenv(lane_a.ENV_SWITCH, raising=False)
    else:
        monkeypatch.setenv(lane_a.ENV_SWITCH, env)
    _usage(updated=time.time())
    up = UpstreamU()
    app = _app(tmp_path, _raw(lane), up)
    before = {str(p.relative_to(on)) for p in on.rglob("*")} if on.exists() else set()
    for b in (_body(sid="s1"), _body("what is a mutex?", prev="Grep", sid="s2"), _body(prev="Edit", sid="s3"),
              _body(continuation=False, sid="s4")):
        assert (await _post(app, copy.deepcopy(b))).status_code == 200
    await app.state.frontier_shadow.drain()
    await asyncio.sleep(0.05)
    assert all(json.loads(q.content)["model"] == OPUS for q in up.requests) and len(up.requests) == 4
    rows = ledger.read_rows(tmp_path / "proxy_calls.jsonl")
    assert all("lane" not in r for r in rows)
    files = {str(p.relative_to(on)) for p in on.rglob("*")} - before
    assert not [f for f in files if "shadow_frontier" in f or "lane_quality" in f or "lane_a_breaker" in f], files
    assert not sf.shadow_dir().exists()


GOLDEN = Path(__file__).parent / "fixtures" / "proxy" / "golden_lane_off_main.json"


def _golden_module():
    import importlib.util
    path = Path(__file__).parent / "fixtures" / "proxy" / "gen_lane_off_golden.py"
    spec = importlib.util.spec_from_file_location("gen_lane_off_golden_pr3", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("shadow", [None, "on"])
@pytest.mark.parametrize("lane,env", [(None, "on"), (OFF_LANE, "on"), (LANE_ON, None), (LANE_ON, "off")])
async def test_lane_off_is_still_byte_identical_to_the_main_golden_with_the_shadow_flag_on_or_off(
        home, monkeypatch, lane, env, shadow):
    if env is None:
        monkeypatch.delenv(lane_a.ENV_SWITCH, raising=False)
    else:
        monkeypatch.setenv(lane_a.ENV_SWITCH, env)
    if shadow:
        monkeypatch.setenv(sf.ENV_FLAG, shadow)
        _usage(updated=time.time())
    gen = _golden_module()
    golden = json.loads(GOLDEN.read_text())
    assert sum(len(v["upstream_requests"]) for v in golden.values()) == 10
    assert await gen.run(gen.policy(lane)) == golden
    assert not sf.shadow_dir().exists()


# ── judge and verdict rows carry the lane tags ───────────────────────────────

def _pair(task_id, **kw):
    row = {"ts": NOW - 50, "task_id": task_id, "door": "proxy", "served": HAIKU, "requested": OPUS, "body_sha": "b",
           "request_context": "ctx", "cheap_response": "ch", "frontier_response": "fr"}
    row.update(kw)
    return row


def test_a_judged_lane_a_pair_writes_a_tagged_verdict_in_an_order_derived_from_its_id(on):
    _usage()
    sf._append_private(sf.pairs_dir() / f"{DAY}.jsonl", _pair("L1", lane="A", task_group="tech_op", rate_phase="fill",
                                                            rate=1.0, session_id="s", msg_count=3, conf=0.7,
                                                            prev_tool_class="technical_op"))
    sf._append_private(sf.pairs_dir() / f"{DAY}.jsonl", _pair("ARM1"))
    items = []

    def run(brief, item):
        items.append(item)
        return {"result": json.dumps({"acceptable_A": True, "acceptable_B": False, "cannot_judge": False}),
                "cost_usd": 0.01, "model_ok": True}
    out = sf.judge_pending(run=run, rng=lambda: 0.99, clock=lambda: NOW, usage_path=paths.state_path("usage.json"))
    assert out["verdicts"] == 2
    rows = {r["task_id"]: r for r in haiku_guard.read_jsonl(sf.verdicts_path())}
    lane_row, arm_row = rows["L1"], rows["ARM1"]
    assert (lane_row["lane"], lane_row["task_group"], lane_row["rate_phase"], lane_row["session_id"]) == (
        "A", "tech_op", "fill", "s")
    assert lane_row["judge_brief_sha256"] == sf.JUDGE_BRIEF_SHA256 and lane_row["judge_model"] == "sonnet"
    want_cheap_first = lane_a_shadow.pair_order_key("L1") < 0.5   # NOT the injected rng (0.99 -> cheap second)
    assert lane_row["cheap_label"] == ("A" if want_cheap_first else "B")
    assert lane_row["acceptable"] is (True if want_cheap_first else False)
    assert "lane" not in arm_row and "judge_brief_sha256" not in arm_row and arm_row["cheap_label"] == "B"  # arm: rng


def test_the_judge_brief_hash_is_pinned():
    import hashlib
    assert sf.JUDGE_BRIEF_SHA256 == hashlib.sha256(sf.JUDGE_BRIEF.encode()).hexdigest()
    # the prereg draft d1 quotes this digest for the brief at origin/main b29f6366
    assert sf.JUDGE_BRIEF_SHA256 == "c78ec06eb37e60e4188ae850afbe7895ae72efb2442633ea5ab57df13553d61b"


async def test_no_prompt_or_answer_text_in_any_lane_a_ledger_row(on):
    _usage()
    await _sample(_shadow(), FakeReplay(), _ctx(), task_id="txt")
    blob = sf.ledger_path().read_text()
    assert CANARY_PROMPT not in blob and CANARY_CHEAP not in blob


# ── the lane_quality shadow_pairs writer ─────────────────────────────────────

def _z_wilson(k, n, z=1.959964):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def _hand_lb(k1, n1, k2, n2):
    """Newcombe method 10 lower bound of p1 - p2, written out independently of eligibility.py."""
    p1, p2 = k1 / n1, k2 / n2
    l1, _ = _z_wilson(k1, n1)
    _, u2 = _z_wilson(k2, n2)
    return (p1 - p2) - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)


def _pairs(k_a, k_f, n=60, *, group="tech_op", start=0, ts=None):
    """n decided pairs with exactly k_a Haiku-acceptable and k_f Frontier-acceptable (spread over the rows).
    ``ts``: stamp row i at ``ts + i`` (default: just before NOW)."""
    return [_verdict(start + i, group=group, a=i < k_a, f=i < k_f, **({} if ts is None else {"ts": ts + i}))
            for i in range(n)]


@pytest.mark.parametrize("k_a,k_f,passes", [(59, 60, True), (60, 60, True), (57, 58, False)])
def test_writer_math_on_a_known_2x2_matches_a_hand_computation(on, k_a, k_f, passes):
    """(57, 58) has a 1.7-point gap and still fails: at n = 60 the Newcombe lower bound binds first (-10.7)."""
    _write_verdicts(_pairs(k_a, k_f))
    out = lqw.write_all(now=NOW)
    assert out["written"] == 1 and out["groups"]["tech_op"]["status"] == "written"
    (row,) = lane_quality.read_rows()
    assert (row["door"], row["served_class"], row["task_group"], row["kind"], row["n"]) == (
        "proxy", "haiku", "tech_op", "shadow_pairs", 60)
    assert row["gap_pts"] == pytest.approx((k_f / 60 - k_a / 60) * 100)         # positive = Haiku worse
    assert row["newcombe_lb"] == pytest.approx(_hand_lb(k_a, 60, k_f, 60) * 100, abs=1e-4)
    ev = eligibility.evidence_from_counts(k_a, 60, k_f, 60)
    assert (row["gap_pts"], row["newcombe_lb"]) == (ev.gap_pts, ev.lb_pts)
    assert (row["k_laneA"], row["k_frontier"], row["lane"]) == (k_a, k_f, "A")
    assert lane_quality.batch_passed(row) is passes
    assert (eligibility.grade({"n": 60, "gap_pts": row["gap_pts"], "lb_pts": row["newcombe_lb"]}) == "eligible") is passes
    assert bool(lane_quality.sampled_lanes([row], NOW)) is passes


def test_writer_gap_sign_and_a_failing_batch_unsamples_the_lane(on):
    _write_verdicts(_pairs(54, 56))                      # 54/60 vs 56/60: gap 3.33 > 3 -> failed
    lqw.write_all(now=NOW)
    (row,) = lane_quality.read_rows()
    assert row["gap_pts"] == pytest.approx(3.3333, abs=1e-3) and lane_quality.batch_passed(row) is False
    assert lane_quality.sampled_lanes([row], NOW) == {}
    # Haiku BETTER than the Frontier is a negative gap and passes
    _write_verdicts(_pairs(60, 57, group="code", start=1000))        # another task group: its own window
    lqw.write_all(now=NOW + 1)
    assert lane_quality.read_rows()[-1]["gap_pts"] == pytest.approx(-5.0)


def test_writer_emits_at_exactly_60_and_not_at_59(on):
    _write_verdicts(_pairs(59, 59, n=59))
    out = lqw.write_all(now=NOW)
    assert out["written"] == 0 and out["groups"]["tech_op"]["status"] == "n_below_min"
    assert not paths.state_path(lane_quality.FILE).exists()                      # nothing, not even an empty file
    _write_verdicts(_pairs(1, 1, n=1, start=59))
    assert lqw.write_all(now=NOW)["written"] == 1
    assert lane_quality.read_rows()[-1]["n"] == 60


def test_writer_counts_exactly_the_decided_pairs_and_drops_none(on):
    rows = _pairs(40, 50, n=70)
    _write_verdicts(rows)
    lqw.write_all(now=NOW)
    (row,) = lane_quality.read_rows()
    assert (row["n"], row["k_laneA"], row["k_frontier"]) == (70, 40, 50)


def test_writer_excludes_cannot_judge_and_reports_the_share(on):
    _write_verdicts(_pairs(58, 58, n=60) + [_verdict(100 + i, cj=True) for i in range(10)])
    out = lqw.write_all(now=NOW)
    (row,) = lane_quality.read_rows()
    assert row["n"] == 60 and row["cannot_judge"] == 10                         # not in n, not counted acceptable
    assert row["cannot_judge_share"] == pytest.approx(10 / 70, abs=1e-4)
    assert (row["k_laneA"], row["k_frontier"]) == (58, 58)
    assert out["groups"]["tech_op"]["cannot_judge"] == 10


def test_writer_does_not_close_the_window_when_cannot_judge_is_over_20_percent(on):
    _write_verdicts(_pairs(60, 60, n=60) + [_verdict(200 + i, cj=True) for i in range(16)])   # 16/76 = 21%
    out = lqw.write_all(now=NOW)
    # D-302: n >= 60 at over 20% cannot_judge is a diagnostic row; the window stays open
    assert out["groups"]["tech_op"]["status"] == "diagnostic" and out["written"] == 0 and out["diagnostic"] == 1
    (row,) = lane_quality.read_rows()
    assert (row["window_complete"], row["diagnostic"], row["not_informative_reason"]) == (
        False, True, "cannot_judge_share")
    assert lane_quality.batch_passed(row) is False and lane_quality.sampled_lanes([row], NOW) == {}


def test_writer_refuses_incomplete_and_foreign_rows(on):
    rows = (_pairs(60, 60) + [_verdict(300 + i, phase=None) for i in range(5)]            # no rate_phase
            + [_verdict(400 + i, group=None) for i in range(5)]                             # no task_group
            + [_verdict(500 + i, judge_model="haiku") for i in range(5)]                    # wrong judge
            + [_verdict(600 + i, judge_brief_sha256="0" * 64) for i in range(5)]            # wrong brief
            + [_verdict(700 + i, lane=None) for i in range(50)])                            # the arm's rows
    _write_verdicts(rows)
    out = lqw.write_all(now=NOW)
    (row,) = lane_quality.read_rows()
    assert row["n"] == 60
    assert out["groups"]["tech_op"]["excluded"] == {"missing_tags": 5, "judge_mismatch": 10}
    assert out["groups"]["(untagged)"]["excluded"] == {"missing_tags": 5}


def test_writer_window_and_idempotence_and_private_file(on):
    old = [_verdict(800 + i, ts=NOW - 20 * 86400.0) for i in range(30)]              # outside the 14-day window
    _write_verdicts(_pairs(59, 59) + old)
    assert lqw.write_all(now=NOW)["written"] == 1 and lane_quality.read_rows()[0]["n"] == 60
    again = lqw.write_all(now=NOW + 5)
    assert again["written"] == 0 and again["groups"]["tech_op"]["status"] == "window_closed"
    assert len(lane_quality.read_rows()) == 1
    f = paths.state_path(lane_quality.FILE)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    _write_verdicts([_verdict(900)])                                                  # a 61st pair: window is closed
    assert lqw.write_all(now=NOW + 10)["written"] == 0 and len(lane_quality.read_rows()) == 1


def test_writer_dry_run_and_no_lane_a_rows_create_nothing(on):
    assert lqw.write_all(now=NOW)["groups"] == {}                                     # no verdict file at all
    assert not paths.state_path(lane_quality.FILE).exists() and not sf.shadow_dir().exists()
    _write_verdicts(_pairs(60, 60))
    out = lqw.write_all(now=NOW, dry_run=True)
    assert out["groups"]["tech_op"]["status"] == "would_write" and not paths.state_path(lane_quality.FILE).exists()
    sf.verdicts_path().unlink()
    _write_verdicts([_verdict(1000 + i, lane=None) for i in range(80)])               # arm rows only
    assert lqw.write_all(now=NOW)["groups"] == {} and not paths.state_path(lane_quality.FILE).exists()


def test_writer_round_trip_feeds_s1_numerator_and_the_latest_batch_decides(on):
    """The writer's row is read by ``lane_quality`` (sampled) and by ``offload_share`` (numerator)."""
    _write_verdicts(_pairs(58, 58))
    lqw.write_all(now=NOW)
    rows = lane_quality.read_rows()
    assert lane_quality.sampled_lanes(rows, NOW) == {
        ("proxy", "haiku", "tech_op"): {"kind": "shadow_pairs", "n": 60, "ts": rows[0]["ts"], "source": rows[0]["source"],
                                      "age_days": 0.0}}
    # D-301: the window closes once. A later final row neither un-samples nor reopens the lane ...
    worse = dict(rows[0], ts=NOW + 5, gap_pts=9.0, source="newer")
    assert lane_quality.sampled_lanes(rows + [worse], NOW + 10) == lane_quality.sampled_lanes(rows, NOW + 10) != {}
    # ... and a failing FIRST final row stays the verdict whatever follows
    bad = dict(rows[0], ts=NOW - 5, gap_pts=9.0, source="first")
    assert lane_quality.sampled_lanes([bad] + rows, NOW + 10) == {}


def test_the_writer_cli_runs_after_the_judge_command(on, monkeypatch, capsys):
    _usage()
    _write_verdicts([_verdict(i, ts=time.time() - 100 - i) for i in range(60)])
    monkeypatch.setattr(sf, "judge_pending", lambda **k: {"status": "ok"})
    assert sf.main(["judge"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["lane_quality"]["written"] == 1 and len(lane_quality.read_rows()) == 1
    assert lqw.main(["--dry-run"]) == 0


# ── KPI separation ───────────────────────────────────────────────────────────

SINCE, UNTIL = KPI_NOW - 86400.0, KPI_NOW
T = KPI_NOW - 3000


def _kpi_rows():
    rows = [proxy_row(1, sid="plain", ts=T, tier="sonnet", kind="organic", msg_id="p1", step="continuation"),
            proxy_row(2, sid="arm", ts=T, tier="haiku", kind="organic", msg_id="a1", reason="haiku_rewrite",
                      tier_arm_assignment="treatment", **{"tier_task_type": "query"})]
    for i in range(3):
        rows.append(proxy_row(10 + i, sid=f"la{i}", ts=T + i, tier="haiku", kind="organic", msg_id=f"l{i}",
                              step="continuation", reason="haiku_rewrite", lane_a_reason="served", lane="A",
                              prev_tool_class="technical_op", **{"tier_task_type": "query"}))
    # a retried lane A step: the pinned model answered, no lane tag
    rows.append(proxy_row(20, sid="retried", ts=T, tier="opus", kind="organic", msg_id="r1", step="continuation",
                          lane_a_reason="retried_original", prev_tool_class="technical_op"))
    return rows


def _units(rows, quality):
    return osh.action_units(SINCE, UNTIL, proxy_rows=rows, mcp_rows=[], techops=[], edit_rows=[],
                            kind_of=lambda sid, stamp: "organic", quality_rows=quality, outcome_redos=[])


def _audit_row(group, **kw):
    """A batch row exactly as the writer builds it (64 decided pairs, Haiku 62/64, Frontier 63/64)."""
    counts = {"n": 64, "k_a": 62, "k_f": 63, "cannot_judge": 0, "ids": [f"t{i}" for i in range(64)]}
    row, status = lqw.batch_row(group, counts, now=KPI_NOW - 100, since=KPI_NOW - 14 * 86400.0, until=KPI_NOW - 100)
    assert status == "written"
    return {**row, **kw}


def test_lane_a_is_counted_on_its_own_line_and_never_twice():
    res = _units(_kpi_rows(), [])
    s = osh.summarize_actions(res["actions"])
    assert s["lane_a"]["n"] == 3 and s["not_lane_a"]["n"] == s["action"]["n"] - 3
    assert s["action"]["n"] == s["lane_a"]["n"] + s["not_lane_a"]["n"] == 6
    assert s["action"]["k"] == s["lane_a"]["k"] + s["not_lane_a"]["k"]
    assert s["lane_a"]["lanes"] == ["proxy/haiku/tech_op"] and s["lane_a"]["quality_sampled"] is False
    assert s["lane_a"]["k"] == 0                       # unsampled lane: denominator only (existing S1 rule)
    # the arm's Haiku turn is a different lane and not in the lane A line
    arm = [a for a in res["actions"] if a["session_id"] == "arm"]
    assert arm[0]["lane"] == ("proxy", "haiku", "qa") and arm[0]["lane_tag"] is None


def test_a_passing_writer_batch_puts_lane_a_in_the_s1_numerator_and_s1_is_otherwise_unchanged():
    with_batch = _units(_kpi_rows(), [_audit_row("tech_op")])
    s = osh.summarize_actions(with_batch["actions"])
    assert s["lane_a"]["k"] == 3 and s["lane_a"]["quality_sampled"] is True and s["action"]["k"] == 3
    # the S1 definition did not move: erase the lane tag and the action line is the same
    stripped = [{k: v for k, v in r.items() if k != "lane"} for r in _kpi_rows()]
    plain = osh.summarize_actions(_units(stripped, [_audit_row("tech_op")])["actions"])
    assert (plain["action"]["k"], plain["action"]["n"]) == (s["action"]["k"], s["action"]["n"])
    assert plain["lane_a"]["n"] == 0 and plain["not_lane_a"]["n"] == 6
    # a failing / absent batch keeps the 3 rows in the denominator only
    failing = osh.summarize_actions(_units(_kpi_rows(), [_audit_row("tech_op", gap_pts=9.0)])["actions"])
    assert (failing["lane_a"]["k"], failing["lane_a"]["n"]) == (0, 3)
    # a batch for another task group does not sample lane A's group
    other = osh.summarize_actions(_units(_kpi_rows(), [_audit_row("qa")])["actions"])
    assert other["lane_a"]["k"] == 0 and other["lane_a"]["quality_sampled"] is False


def test_the_persisted_daily_offload_row_schema_is_left_alone():
    """``daily_offload`` rows are the preregistered S1 evidence; PR3 adds no key to them (the split lives in
    ``kpi`` ``offload`` only)."""
    from llm_router import daily_offload
    actions = _units(_kpi_rows(), [_audit_row("tech_op")])["actions"]
    assert set(daily_offload._breakdown(actions)) == {"by_task_group", "by_served_model", "by_lane"}


def test_a_lane_free_ledger_has_an_empty_lane_a_line():
    stripped = [r for r in _kpi_rows() if r.get("lane") != "A"]
    s = osh.summarize_actions(_units(stripped, [])["actions"])
    assert s["lane_a"]["n"] == 0 and s["lane_a"]["value"] is None and s["lane_a"]["quality_sampled"] is None


# ── carry-in S1: the lane-specific header timeout ────────────────────────────

def test_header_timeout_default_is_a_conservative_documented_placeholder_and_strict():
    assert lane_a.DEFAULT_HEADER_TIMEOUT_S == 60.0 and lane_a.LaneAConfig().header_timeout_s == 60.0
    assert lane_a.parse_config({"enabled": True, "header_timeout_s": 12}).header_timeout_s == 12.0
    for bad in (0, -1, True, "5", 601, None):
        with pytest.raises(ValueError, match="header_timeout_s"):
            lane_a.parse_config({"header_timeout_s": bad})
    assert "header_timeout_s" in pt.DEFAULT_POLICY_PATH.read_text()      # documented in the bundled YAML


class HangingHaiku:
    def __init__(self):
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        if "haiku" in json.loads(request.content)["model"]:
            await asyncio.sleep(30)
        return httpx.Response(200, stream=httpx.ByteStream(_sse(_good_sse())), headers={"content-type": SSE})


@pytest.mark.timing
async def test_a_hung_haiku_falls_back_to_the_pinned_model_after_the_lane_header_timeout(home, tmp_path, monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    up = HangingHaiku()
    app = _app(tmp_path, _raw(LANE_ON | {"header_timeout_s": 0.2}), up)
    t0 = time.monotonic()
    r = await asyncio.wait_for(_post(app, _body(sid="sess-hang")), 10)
    assert r.status_code == 200 and time.monotonic() - t0 < 5
    assert [json.loads(q.content)["model"] for q in up.requests] == [HAIKU, OPUS]
    (row,) = ledger.read_rows(tmp_path / "proxy_calls.jsonl")
    assert (row["lane_a_reason"], row["served_model"], row["tier_retry"]["status"]) == (lane_a.R_RETRIED, OPUS, None)


async def test_the_header_timeout_does_not_touch_non_lane_calls(home, tmp_path, monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "off")
    up = Upstream()
    app = _app(tmp_path, _raw(LANE_ON | {"header_timeout_s": 0.001}), up)
    assert (await _post(app, _body(sid="sess-off"))).status_code == 200


# ── carry-in S2: one failure direction ───────────────────────────────────────

async def test_an_internal_admit_error_also_denies_and_records_a_code(tmp_path, monkeypatch):
    codes = []
    real = lb.failopen.record
    monkeypatch.setattr(lb.failopen, "record", lambda code, exc, *a, **k: codes.append(code) or real(code, exc, *a, **k))
    b = _breaker(tmp_path)

    async def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(b, "_ensure_loaded", boom)
    assert await b.admit() == (False, False) and "LR-FO-LANE-A-BREAKER-ADMIT" in codes


async def test_a_locked_state_file_forwards_the_pinned_model_through_the_proxy(home, tmp_path, monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    f = paths.state_path("lane_a_breaker.json")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{corrupt")
    up = Upstream()
    assert (await _post(_app(tmp_path, _raw(), up), _body(sid="s-lock"))).status_code == 200
    assert [json.loads(q.content)["model"] for q in up.requests] == [OPUS]
    (row,) = ledger.read_rows(tmp_path / "proxy_calls.jsonl")
    assert row["lane_a_reason"] == lane_a.R_BREAKER_OPEN


# ── carry-in S3 / N3: the docs ───────────────────────────────────────────────

DOC = (Path(__file__).parent.parent / "docs" / "proxy.md").read_text()


def test_docs_say_eligible_comes_after_the_cost_and_cap_gates_and_before_the_knn_vote_and_the_breaker():
    assert "BEFORE the veto and the cost and cap gates" not in DOC
    assert "after the cost-switch and consecutive-cap gates" in DOC and "before the knn vote and the breaker" in DOC


def test_docs_say_retried_original_covers_5xx_and_timeouts_and_midstream_failures_are_not_retried():
    assert "Haiku refused the body and the pinned model answered |" not in DOC
    assert "5xx" in DOC.split("`served` / `retried_original`")[1].split("\n")[0]
    assert "mid-stream" in DOC and "not retried" in DOC


async def test_eligible_is_after_cost_and_cap_and_before_the_breaker_and_knn(monkeypatch):
    """The behaviour the docs describe: with the lane OFF a step that fails the cost rule reads switch_cost_high
    (not eligible), and the breaker is never asked."""
    monkeypatch.delenv(lane_a.ENV_SWITCH, raising=False)
    monkeypatch.setattr(lb.LaneABreaker, "admit", lambda self: (_ for _ in ()).throw(AssertionError("asked")))
    pol = _policy(OFF_LANE | {"max_switch_usd": 1e-9})
    d = await pol.decide(_body(), "sess-lane-a", Stickiness(), classify=_rules())
    assert d.lane_a_reason == lane_a.R_SWITCH_COST
    d = await _policy(OFF_LANE).decide(_body(), "sess-lane-a", Stickiness(), classify=_rules())
    assert d.lane_a_reason == lane_a.R_ELIGIBLE


# ── carry-in N1: dead code ───────────────────────────────────────────────────

def test_the_dead_lane_feed_reply_in_the_thinking_branch_is_gone():
    src = (Path(__file__).parent.parent / "src" / "llm_router" / "proxy" / "server.py").read_text()
    assert "lf.reply(up.status_code, text.encode()" not in src


# ── carry-in N2: 401 / 413 are not breaker failures ──────────────────────────

@pytest.mark.parametrize("status,counts", [(401, False), (413, False), (400, True), (429, True), (500, True)])
async def test_runtime_counts_only_real_haiku_failures(tmp_path, status, counts):
    rt = lb.LaneARuntime(BR, tmp_path / "b.json")
    kind = rt.reply(status, b"x", "application/json", session_id="s", model=HAIKU, session_kind="organic", probe=False)
    assert kind == f"http_{status}"
    assert len(rt.breaker.failures) == (1 if counts else 0)


async def test_a_401_from_haiku_never_trips_the_breaker_and_the_client_sees_it(home, tmp_path, monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    up = UpstreamU(401)
    app = _app(tmp_path, _raw(), up)
    for i in range(7):                                # more than trip_failures (5)
        r = await _post(app, _body(sid=f"s{i}"))
        assert r.status_code == 401
    assert [json.loads(q.content)["model"] for q in up.requests] == [HAIKU] * 7   # still serving: never opened
    rows = ledger.read_rows(tmp_path / "proxy_calls.jsonl")
    assert {r["lane_a_reason"] for r in rows} == {lane_a.R_SERVED}


async def test_a_401_on_a_probe_frees_the_probe_slot_and_changes_nothing_else(tmp_path):
    clk = Clock()
    b = _breaker(tmp_path, clk)
    b._open("x", 10.0)
    b._loaded = True
    clk.t += 11
    assert await b.admit() == (True, True)
    rt = lb.LaneARuntime(BR, tmp_path / "x.json", clk)
    rt.breaker = b
    rt.reply(401, b"x", "application/json", session_id="s", model=HAIKU, session_kind=None, probe=True,
             probe_id=b.probe_seq)
    assert b.state == lb.HALF_OPEN and b.probe_at is None            # slot free, not closed, not reopened
    assert await b.admit() == (True, True)


# ── carry-in N4: no second body serialization ────────────────────────────────

@pytest.mark.parametrize("lane", [OFF_LANE, LANE_ON])
async def test_the_context_size_is_estimated_once_per_decision(home, monkeypatch, lane):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on" if lane is LANE_ON else "off")
    calls = []
    real = pt._approx_context_tokens
    monkeypatch.setattr(pt, "_approx_context_tokens", lambda b: calls.append(1) or real(b))
    d = await _policy(lane).decide(_body(), "sess-lane-a", Stickiness(), classify=_rules())
    assert d.lane_a_reason in (lane_a.R_ELIGIBLE, lane_a.R_SERVED, lane_a.R_BREAKER_OPEN)
    assert len(calls) == 1, "the cache rule must reuse the body check's estimate, not serialize the body again"


def test_the_haiku_block_keeps_the_context_estimate_its_check_made():
    body = _body()
    block = pt._HaikuBlock(body, False, pt._Phases(), None)
    assert block.tokens is None
    assert block() == pt.HAIKU_BLOCK_NONE and block.tokens == pt._approx_context_tokens(body)
    media = _body(extra_user=[{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}}])
    blocked = pt._HaikuBlock(media, False, pt._Phases(), None)
    assert blocked() == pt.HAIKU_BLOCK_MEDIA and blocked.tokens is None        # stopped before the estimate


# ── carry-in N5: a late probe outcome with a stale id ────────────────────────

async def test_a_late_outcome_of_a_timed_out_probe_cannot_settle_the_next_probe(tmp_path):
    clk = Clock()
    b = _breaker(tmp_path, clk)
    await b._ensure_loaded()
    b._open("x", 10.0)
    clk.t += 11
    assert await b.admit() == (True, True)
    first = b.probe_seq
    clk.t += BR.probe_timeout_s + 1                       # the first probe never answered
    assert await b.admit() == (True, True)
    second = b.probe_seq
    assert second == first + 1
    b.record_success(True, first)                         # late: ignored
    assert b.state == lb.HALF_OPEN
    b.record_failure("http_500", True, first)             # late: ignored
    assert b.state == lb.HALF_OPEN and b.failures == []
    b.record_success(True, second)                        # the current probe closes it
    assert b.state == lb.CLOSED


async def test_the_probe_id_travels_from_admit_to_the_served_step(home, monkeypatch, tmp_path):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    pol = _policy(LANE_ON)
    pol.lane_runtime.breaker.path = tmp_path / "b.json"
    br = pol.lane_runtime.breaker
    await br._ensure_loaded()
    br._open("x", 0.0)
    d = await pol.decide(_body(), "sess-lane-a", Stickiness(), classify=_rules())
    assert d.detail == lane_a.DETAIL_PROBE and d.lane_a_probe_id == br.probe_seq == 1


# ── trip visibility: quality_breaker open_classes and the Stop-line ──────────

def _write_state(state, **kw):
    f = paths.state_path(lb.STATE_FILE)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"key": lb.KEY, "state": state, "units": [[1, False]] * 3, **kw}))


def test_the_pinned_literals_match_the_breaker():
    assert (qb.LANE_A_STATE_FILE, qb.LANE_A_KEY) == (lb.STATE_FILE, lb.KEY)


@pytest.mark.parametrize("state", ["open", "half_open"])
def test_a_lane_a_trip_shows_in_open_classes_and_the_stop_line(home, state):
    _write_state(state)
    rows = [r for r in qb.open_classes() if r["key"] == lb.KEY]
    assert rows == [{"key": "proxy_haiku:lane_a", "state": state, "failure_rate": None, "n": 3, "unknown": 0}]
    assert qb.stop_line_summary() == "breaker: 1 class off"


def test_a_closed_absent_or_unreadable_lane_a_breaker_shows_nothing_and_is_never_created(home):
    assert qb.open_classes() == [] and qb.stop_line_summary() is None
    assert not paths.state_path(lb.STATE_FILE).exists()                      # read-only: not created
    _write_state("closed")
    assert qb.open_classes() == []
    paths.state_path(lb.STATE_FILE).write_text("{corrupt")
    assert qb.open_classes() == []
    paths.state_path(lb.STATE_FILE).write_text(json.dumps({"key": "other", "state": "open"}))
    assert qb.open_classes() == []


def test_reading_the_trip_changes_no_file(home):
    _write_state("open")
    f = paths.state_path(lb.STATE_FILE)
    before = (f.read_bytes(), f.stat().st_mtime_ns, sorted(os.listdir(f.parent)))
    qb.open_classes()
    qb.stop_line_summary()
    assert (f.read_bytes(), f.stat().st_mtime_ns, sorted(os.listdir(f.parent))) == before


# ═══ Repair round 1 (review of #732): SHOULD-1..6 and the D-301 window ═══════════════════════════════════════

def _lane_pair(i, **kw):
    return _pair(f"J{i}", lane="A", task_group="tech_op", rate_phase="fill", rate=1.0, session_id=f"s{i}",
                 msg_count=i, conf=0.7, prev_tool_class="technical_op", served_body_sha="sha-x",
                 replay_body_sha="sha-x", **kw)


def _judge(run, pairs):
    _usage()
    for p in pairs:
        sf._append_private(sf.pairs_dir() / f"{DAY}.jsonl", p)
    return sf.judge_pending(run=run, rng=lambda: 0.99, clock=lambda: NOW, usage_path=paths.state_path("usage.json"))


def _boom(brief, item):
    raise TimeoutError("judge timed out")


def _junk(brief, item):
    return {"result": "I think A is better", "cost_usd": 0.0, "model_ok": True}


def _wrong_model(brief, item):
    return {"result": json.dumps({"acceptable_A": True, "acceptable_B": True, "cannot_judge": False}),
            "cost_usd": 0.0, "model_ok": False}


# ── SHOULD-1: judge failures are cannot_judge for the lane ───────────────────

@pytest.mark.parametrize("run,reason", [(_boom, "TimeoutError"), (_junk, "unparsed"), (_wrong_model, "model_mismatch")])
def test_a_lane_a_judge_failure_is_a_cannot_judge_verdict_row_with_a_reason(on, run, reason):
    out = _judge(run, [_lane_pair(1)])
    assert out["failed"] == 1 and out["verdicts"] == 0
    (row,) = haiku_guard.read_jsonl(sf.verdicts_path())
    assert (row["cannot_judge"], row["acceptable"], row["frontier_acceptable"], row["cannot_judge_reason"]) == (
        True, None, None, reason)
    assert (row["lane"], row["task_group"], row["judge_brief_sha256"]) == ("A", "tech_op", sf.JUDGE_BRIEF_SHA256)
    assert not {"cheap_response", "frontier_response", "request_context"} & set(row)
    tally = lqw.tally([row], since=0, until=NOW + 1)
    assert tally["tech_op"]["cannot_judge"] == 1 and tally["tech_op"]["n"] == 0


@pytest.mark.parametrize("run", [_boom, _junk, _wrong_model])
def test_the_arm_judge_failures_write_no_verdict_row_as_before(on, run):
    out = _judge(run, [_pair("ARM1")])
    assert out["failed"] == 1 and not sf.verdicts_path().exists()
    assert len(haiku_guard.read_jsonl(sf.judge_ledger_path())) == 1


def test_over_20_percent_judge_failures_leave_the_window_open_and_nothing_is_counted(on):
    """60 decided pairs and 16 judge failures (21%): a diagnostic row only (D-302), S1 gets nothing yet."""
    _write_verdicts(_pairs(60, 60, n=60))
    _judge(_boom, [_lane_pair(500 + i) for i in range(16)])
    out = lqw.write_all(now=NOW)
    assert out["groups"]["tech_op"]["cannot_judge"] == 16 and out["groups"]["tech_op"]["n"] == 60
    (row,) = lane_quality.read_rows()
    assert (row["informative"], row["not_informative_reason"], row["window_complete"]) == (
        False, "cannot_judge_share", False)
    assert row["cannot_judge_share"] == pytest.approx(16 / 76, abs=1e-4)
    assert lane_quality.sampled_lanes([row], NOW) == {}
    # at 20% exactly (15 of 75) it is informative and counts
    sf.verdicts_path().unlink()
    sf.judge_ledger_path().unlink()
    paths.state_path(lane_quality.FILE).unlink()
    for f in sf.pairs_dir().glob("*.jsonl"):
        f.unlink()
    _write_verdicts(_pairs(60, 60, n=60))
    _judge(_boom, [_lane_pair(600 + i) for i in range(15)])
    lqw.write_all(now=NOW)
    (ok,) = lane_quality.read_rows()
    assert ok["informative"] is True and ok["cannot_judge"] == 15 and lane_quality.sampled_lanes([ok], NOW)


# ── SHOULD-5: a cannot_judge row that carries boolean labels is still cannot_judge ───────────────────────────

def test_a_cannot_judge_row_with_boolean_labels_is_not_decided(on):
    odd = [_verdict(2000 + i, cj=True, acceptable=True, frontier_acceptable=True) for i in range(5)]
    assert all(r["cannot_judge"] is True and r["acceptable"] is True for r in odd)
    assert not lane_a_shadow.is_decided(odd[0])
    t = lqw.tally(_pairs(60, 60) + odd, since=0, until=NOW + 1)["tech_op"]
    assert (t["n"], t["k_a"], t["k_f"], t["cannot_judge"]) == (60, 60, 60, 5)
    assert lane_a_shadow.count_decided(odd, "tech_op", since=0, until=NOW + 1) == 0   # and not for the phase flip


# ── SHOULD-2: one pair per step ──────────────────────────────────────────────

def test_60_verdict_rows_for_one_step_give_n_equal_1(on):
    rows = [_verdict(i, session_id="sess-1", msg_count=7, ts=NOW - 1000 + i) for i in range(60)]
    t = lqw.tally(rows, since=0, until=NOW + 1)["tech_op"]
    assert t["n"] == 1 and t["excluded"] == {"duplicate_step": 59} and t["ids"] == ["t0"]   # the FIRST verdict wins
    _write_verdicts(rows)
    out = lqw.write_all(now=NOW)
    assert out["written"] == 0 and out["groups"]["tech_op"]["status"] == "n_below_min"
    assert lane_a_shadow.count_decided(rows, "tech_op", since=0, until=NOW + 1) == 1       # nor does it flip the phase


def test_the_first_verdict_of_a_step_wins_whatever_the_file_order(on):
    late_first = [_verdict(1, session_id="s", msg_count=1, ts=NOW - 10, a=False), _verdict(2, session_id="s",
                  msg_count=1, ts=NOW - 50, a=True)]
    t = lqw.tally(late_first, since=0, until=NOW + 1)["tech_op"]
    assert (t["n"], t["k_a"], t["ids"]) == (1, 1, ["t2"])


async def test_the_sampler_never_replays_the_same_step_twice(on):
    _usage()
    rep, fs = FakeReplay(), _shadow()
    await _sample(fs, rep, _ctx(n=3), task_id="first")
    await _sample(fs, rep, _ctx(n=3), task_id="resent")           # same session and msg_count, new msg_id
    await _sample(fs, rep, _ctx(n=5), task_id="next")
    assert len(rep.calls) == 2
    assert [(r["task_id"], r["outcome"], r.get("reason")) for r in _ledger()] == [
        ("first", "replayed", None), ("resent", "skipped", "step_seen"), ("next", "replayed", None)]


async def test_a_failed_replay_also_uses_up_the_step(on):
    _usage()
    fs = _shadow()
    await _sample(fs, FakeReplay(exc=RuntimeError("x")), _ctx(n=3), task_id="a")
    rep = FakeReplay()
    await _sample(fs, rep, _ctx(n=3), task_id="b")
    assert rep.calls == [] and _ledger()[-1]["reason"] == "step_seen"


# ── SHOULD-3 (coordinator): lane A has its own trigger; the arm's is untouched ──────────────────────────

ARM_ROWS = [{"ts": 1000 + i, "acceptable": i % 3 != 0, "cannot_judge": i == 5} for i in range(30)]
ARM_TRIGGER_ON_MAIN = {"bar": "< 26/30 at n>=20", "cannot_judge": 1, "evaluable": True, "k": 19, "min_n": 20, "n": 29,
                       "name": "shadow", "tripped": True, "value": "acceptable 19/29 = 65.5% (Wilson 95% 0.473-0.801)",
                       "wilson95": [0.47345103949857975, 0.8005928156337425]}


def test_arm_only_shadow_trigger_is_byte_identical_to_main():
    """ARM_TRIGGER_ON_MAIN was produced by running origin/main (2c5a876e) on ARM_ROWS."""
    assert haiku_guard.shadow_trigger(ARM_ROWS, until=2000) == ARM_TRIGGER_ON_MAIN
    assert json.dumps(haiku_guard.shadow_trigger(ARM_ROWS, until=2000), sort_keys=True) == json.dumps(
        ARM_TRIGGER_ON_MAIN, sort_keys=True)


def _bad_lane_rows(n=40):
    return [_verdict(3000 + i, a=False, f=True, ts=1500 + i) for i in range(n)]


def test_lane_a_failures_alone_do_not_trip_the_arm_trigger_and_do_not_move_it():
    lane = _bad_lane_rows()
    alone = haiku_guard.shadow_trigger(lane, until=2000)
    assert (alone["n"], alone["tripped"], alone["evaluable"]) == (0, False, False)
    assert haiku_guard.shadow_trigger(ARM_ROWS + lane, until=2000) == ARM_TRIGGER_ON_MAIN   # arm number unmoved
    good_arm = [{"ts": 1000 + i, "acceptable": True} for i in range(30)]
    assert haiku_guard.shadow_trigger(good_arm + lane, until=2000)["tripped"] is False       # a strong arm is not sunk
    ev = haiku_guard.evaluate([], since=0, until=2000, shadow_rows=lane, audits=[])
    assert "shadow" not in ev["tripped"]


def test_lane_a_quality_trigger_reads_lane_a_rows_only_at_26_of_30():
    lane = _bad_lane_rows(20)
    t = haiku_guard.lane_a_shadow_trigger(lane + [{"ts": 1600, "acceptable": False}] * 30, until=2000)
    assert (t["name"], t["n"], t["tripped"]) == ("lane_a_shadow", 20, True)                  # arm rows are ignored
    assert haiku_guard.lane_a_shadow_trigger(_bad_lane_rows(19), until=2000)["tripped"] is False   # n < 20
    ok = [_verdict(4000 + i, a=i != 0, ts=1500 + i) for i in range(30)]                      # 29/30
    assert haiku_guard.lane_a_shadow_trigger(ok, until=2000)["tripped"] is False


def test_statusline_and_session_report_source_excludes_lane_a_and_lane_a_has_its_own_summary(on):
    _write_verdicts([_verdict(i, a=False, ts=NOW - 100) for i in range(5)]
                    + [_verdict(10 + i, a=True, lane=None, ts=NOW - 100) for i in range(4)])
    arm, lane = sf.verdict_summary(now=NOW), sf.verdict_summary(now=NOW, lane="A")
    assert (arm["n"], arm["k"]) == (4, 4) and (lane["n"], lane["k"]) == (5, 0)


async def test_the_lane_a_breaker_opens_on_lane_a_shadow_quality_and_the_arm_cannot_open_it(on, tmp_path):
    clock = Clock(NOW)
    b = lb.LaneABreaker(BR, tmp_path / "b.json", clock, quality_fn=lb.shadow_quality_reason)
    _write_verdicts([_verdict(i, a=False, lane=None) for i in range(30)])                    # a failing ARM
    assert (await b.admit()) == (True, False)
    clock.t = NOW + lb.QUALITY_POLL_S + 1
    _write_verdicts([_verdict(100 + i, a=i < 10) for i in range(20)])                        # lane A 10/20
    assert (await b.admit()) == (False, False)
    assert b.state == "open" and b.reason.startswith("shadow_quality:") and b.cooldown_s == BR.quality_cooldown_s


async def test_the_quality_watch_polls_rarely_and_never_without_a_quality_fn(on, tmp_path):
    calls = []
    clock = Clock(NOW)
    b = lb.LaneABreaker(BR, tmp_path / "b.json", clock, quality_fn=lambda br, now: calls.append(now))
    for _ in range(3):
        assert (await b.admit()) == (True, False)
    assert len(calls) == 1
    clock.t += lb.QUALITY_POLL_S
    await b.admit()
    assert len(calls) == 2
    plain = _breaker(tmp_path / "x")
    assert (await plain.admit()) == (True, False) and plain.quality_fn is None


async def test_after_a_probe_closes_the_breaker_only_newer_verdicts_count(on, tmp_path):
    clock = Clock(NOW)
    b = lb.LaneABreaker(BR, tmp_path / "b.json", clock, quality_fn=lb.shadow_quality_reason)
    _write_verdicts([_verdict(i, a=False, ts=NOW - 500) for i in range(20)])
    assert (await b.admit()) == (False, False)
    clock.t = NOW + BR.quality_cooldown_s + 1
    assert (await b.admit()) == (True, True)
    b.record_success(True, b.probe_seq)
    assert b.state == "closed" and b.quality_floor == clock.t
    clock.t += lb.QUALITY_POLL_S + 1
    assert (await b.admit()) == (True, False)                      # the old verdicts cannot reopen it
    await b.flush()
    assert json.loads((tmp_path / "b.json").read_text())["quality_floor"] == b.quality_floor


# ── SHOULD-4: body sha pairing check ─────────────────────────────────────────

async def test_replay_http_reports_the_sha_of_the_bytes_it_sent():
    seen = []

    def handler(req):
        seen.append(req.content)
        return httpx.Response(200, stream=httpx.ByteStream(b"{}"), headers={"content-type": "application/json"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        rep = await sf.replay_http(c, "http://x/v1/messages", {}, b'{"a": 1}')
    assert rep.sent_sha == sf.body_sha(seen[0]) == sf.body_sha(b'{"a": 1}')


def test_the_writer_excludes_and_counts_a_pair_whose_replay_sha_differs_or_is_missing(on):
    rows = (_pairs(60, 60) + [_verdict(5000 + i, replay_body_sha="other") for i in range(3)]
            + [_verdict(5100 + i, replay_body_sha=None) for i in range(2)]
            + [_verdict(5200 + i, served_body_sha=None, replay_body_sha=None) for i in range(2)])
    t = lqw.tally(rows, since=0, until=NOW + 1)["tech_op"]
    assert t["n"] == 60 and t["excluded"] == {"body_sha_mismatch": 7}
    assert lane_a_shadow.judged_by_prereg(rows[0])


async def test_a_served_lane_a_row_and_its_pair_carry_the_same_body_sha(on, tmp_path, monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    _usage(updated=time.time())
    up = UpstreamU()
    app = _app(tmp_path, _raw(), up)
    body = _body(sid="sess-sha")
    assert (await _post(app, body)).status_code == 200
    await app.state.frontier_shadow.drain()
    sent = up.requests[1].content
    (row,) = ledger.read_rows(tmp_path / "proxy_calls.jsonl")
    pair = [json.loads(line) for f in sf.pairs_dir().glob("*.jsonl") for line in f.read_text().splitlines()][0]
    assert row["lane_a_body_sha"] == sf.body_sha(sent)
    assert pair["served_body_sha"] == pair["replay_body_sha"] == pair["body_sha"] == row["lane_a_body_sha"]
    assert _ledger()[0]["served_body_sha"] == row["lane_a_body_sha"]
    # the owner flip is recorded by the first request that saw all four switches on, 0600, once
    flip = lane_a_shadow.read_flip()
    assert flip is not None and stat.S_IMODE(lane_a_shadow.flip_path().stat().st_mode) == 0o600
    assert lane_a_shadow.record_flip(flip + 99) is False and lane_a_shadow.read_flip() == flip
    assert lane_a_shadow.read_flip_row()["switches"] == dict(yaml_enabled=True, env_on=True, haiku_rewrite=True,
                                                             shadow_on=True)


# ── SHOULD-6 / D-301: the window closes once; only the final row counts ─────────────────────────────────

def test_constants_hold_the_d301_window_values():
    c = lane_a_shadow.CONSTANTS
    assert (c["FINAL_WINDOW_PAIRS"], c["FINAL_WINDOW_DAYS"]) == (60, 28)


def test_the_60th_decided_pair_writes_the_final_row_and_the_59th_does_not(on):
    _write_verdicts(_pairs(59, 59, n=59))
    assert lqw.write_all(now=NOW)["written"] == 0
    _write_verdicts(_pairs(1, 1, n=1, start=59))
    out = lqw.write_all(now=NOW)
    (row,) = lane_quality.read_rows()
    assert out["groups"]["tech_op"]["close_reason"] == "n_reached" and row["window_complete"] is True
    assert (row["informative"], row["n"], row["close_reason"]) == (True, 60, "n_reached")
    assert row["excluded"] == {} and lane_quality.sampled_lanes([row], NOW)


def test_28_days_after_the_flip_with_fewer_than_60_pairs_is_final_not_informative_and_earns_nothing(on):
    flip = NOW - 29 * 86400.0
    assert lane_a_shadow.record_flip(flip)
    t = flip + 86400.0
    _write_verdicts(_pairs(10, 10, n=10, ts=t) + [_verdict(7000, session_id="s", msg_count=1, ts=t + 50),
                                                  _verdict(7001, session_id="s", msg_count=1, ts=t + 60)])
    out = lqw.write_all(now=NOW)
    (row,) = lane_quality.read_rows()
    assert out["groups"]["tech_op"]["status"] == "not_informative"
    assert (row["window_complete"], row["informative"], row["close_reason"], row["not_informative_reason"]) == (
        True, False, "window_28d", "n_below_min")
    assert row["n"] == 11 and row["gap_pts"] is None and row["flip_ts"] == flip
    assert lane_quality.batch_passed(row) is False and lane_quality.sampled_lanes([row], NOW) == {}
    s = osh.summarize_actions(_units(_kpi_rows(), [row | {"ts": KPI_NOW - 100}])["actions"])
    assert s["lane_a"]["k"] == 0 and s["lane_a"]["quality_sampled"] is False


def test_before_28_days_and_below_60_nothing_is_written(on):
    assert lane_a_shadow.record_flip(NOW - 27 * 86400.0)
    _write_verdicts(_pairs(10, 10, n=10))
    out = lqw.write_all(now=NOW)
    assert out["written"] == 0 and out["groups"]["tech_op"]["status"] == "n_below_min"
    assert not paths.state_path(lane_quality.FILE).exists()


def test_with_no_flip_recorded_the_clock_never_closes_a_window(on):
    assert lane_a_shadow.read_flip() is None
    _write_verdicts(_pairs(10, 10, n=10))
    out = lqw.write_all(now=NOW)                                              # trailing 14 d window; no 28 d close
    assert out["written"] == 0 and out["groups"]["tech_op"]["status"] == "n_below_min"
    assert not paths.state_path(lane_quality.FILE).exists()


def test_the_window_starts_at_the_flip(on):
    lane_a_shadow.record_flip(NOW - 20 * 86400.0)
    _write_verdicts(_pairs(40, 40, n=40) + [_verdict(8000 + i, ts=NOW - 25 * 86400.0) for i in range(30)])
    out = lqw.write_all(now=NOW)
    assert out["groups"]["tech_op"]["n"] == 40 and out["written"] == 0          # 30 rows before the flip do not count


def test_rows_before_the_final_row_are_diagnostic_and_never_counted(on):
    passing = {"ts": NOW - 5, "door": "proxy", "served_class": "haiku", "task_group": "tech_op",
               "kind": "shadow_pairs", "n": 64, "gap_pts": 0.0, "newcombe_lb": -5.0, "source": "diag", "lane": "A"}
    for extra in ({}, {"window_complete": False}, {"window_complete": "true"}, {"window_complete": 1}):
        row = passing | extra
        assert lane_quality.batch_passed(row) is True                       # it would pass if counted ...
        assert lane_quality.sampled_lanes([row], NOW) == {}                 # ... but it is not final
    s = osh.summarize_actions(_units(_kpi_rows(), [_audit_row("tech_op", window_complete=False)])["actions"])
    assert s["lane_a"]["k"] == 0 and s["lane_a"]["quality_sampled"] is False
    final = osh.summarize_actions(_units(_kpi_rows(), [_audit_row("tech_op")])["actions"])
    assert final["lane_a"]["k"] == 3 and final["lane_a"]["quality_sampled"] is True


def test_after_the_final_row_later_batches_do_not_count_and_do_not_reopen(on):
    lane_a_shadow.record_flip(NOW - 40 * 86400.0)
    t = NOW - 35 * 86400.0
    _write_verdicts(_pairs(40, 40, n=40, start=0, ts=t) + _pairs(20, 20, n=20, start=100, ts=t + 100))
    assert lqw.write_all(now=NOW)["written"] == 1
    first = lane_quality.read_rows()[0]
    _write_verdicts(_pairs(0, 60, n=60, start=200))                          # a terrible second batch, and 28 d long gone
    out = lqw.write_all(now=NOW + 3600.0)
    assert out["written"] == 0 and out["groups"]["tech_op"]["status"] == "window_closed"
    assert lane_quality.read_rows() == [first]
    assert lane_quality.sampled_lanes(lane_quality.read_rows(), NOW + 3600.0)       # still sampled, not un-sampled
    # a hand-appended later final row does not replace the first one either
    sf._append_private(paths.state_path(lane_quality.FILE), first | {"ts": NOW + 9, "gap_pts": 9.0, "source": "later"})
    assert lane_quality.sampled_lanes(lane_quality.read_rows(), NOW + 100.0)
    # another task group has its own window
    _write_verdicts(_pairs(60, 60, n=60, group="code", start=400, ts=t + 500))
    assert lqw.write_all(now=NOW + 4000.0)["groups"]["code"]["status"] == "written"


def test_the_flip_file_is_written_only_by_a_served_step_and_with_state_files_perms(on):
    assert not lane_a_shadow.flip_path().exists()
    assert lane_a_shadow.record_flip(NOW) is True
    assert stat.S_IMODE(lane_a_shadow.flip_path().stat().st_mode) == 0o600
    assert lane_a_shadow.read_flip() == NOW
    lane_a_shadow.flip_path().write_text("{corrupt")
    assert lane_a_shadow.read_flip() is None


async def test_the_lane_off_shadow_records_no_flip(on):
    _usage()
    await _sample(_shadow(), FakeReplay(), None, task_id="arm-step")          # the arm: no lane context
    assert not lane_a_shadow.flip_path().exists()


# ── NITs ─────────────────────────────────────────────────────────────────────

def test_the_judge_model_is_imported_not_repeated():
    assert lqw.JUDGE_MODEL is sf.JUDGE_MODEL and lqw.MIN_PAIRS == lane_a_shadow.FINAL_WINDOW_PAIRS


def test_the_phase_count_ignores_rows_the_writer_excludes(on):
    rows = ([_verdict(i) for i in range(30)] + [_verdict(100 + i, judge_model="haiku") for i in range(30)]
            + [_verdict(200 + i, judge_brief_sha256="0" * 64) for i in range(30)])
    assert lane_a_shadow.count_decided(rows, "tech_op", since=0, until=NOW + 1) == 30


def test_excluded_counts_are_written_into_the_row(on):
    _write_verdicts(_pairs(60, 60) + [_verdict(9000 + i, judge_model="haiku") for i in range(4)]
                    + [_verdict(9100, session_id="d", msg_count=1), _verdict(9101, session_id="d", msg_count=1)])
    lqw.write_all(now=NOW)
    (row,) = lane_quality.read_rows()
    assert row["excluded"] == {"duplicate_step": 1, "judge_mismatch": 4}


def test_the_unused_lane_a_breaker_read_state_is_gone():
    assert not hasattr(lb, "read_state")


# ═══ Repair round 2 (D-302 + review of the round-1 head) ═════════════════════════════════════════════════════

DAYS = 86400.0


def _cj_batch(n_decided, n_cj, *, t, start=0):
    return _pairs(n_decided, n_decided, n=n_decided, start=start, ts=t) + [
        _verdict(start + 5000 + i, cj=True, ts=t + 1000 + i) for i in range(n_cj)]


def test_n_60_at_21_percent_cannot_judge_is_non_final_and_the_window_stays_open_then_a_later_batch_closes_it(on):
    flip = NOW - 10 * DAYS
    assert lane_a_shadow.record_flip(flip)
    _write_verdicts(_cj_batch(60, 16, t=flip + 100))                         # 16/76 = 21%
    out = lqw.write_all(now=NOW)
    (diag,) = lane_quality.read_rows()
    assert out["written"] == 0 and out["groups"]["tech_op"]["status"] == "diagnostic"
    assert (diag["window_complete"], diag["diagnostic"], diag["n"], diag["cannot_judge"]) == (False, True, 60, 16)
    assert lane_quality.sampled_lanes([diag], NOW) == {} and lane_quality.final_only([diag]) == []
    assert "tech_op" not in lqw.closed_groups(lane_quality.read_rows())          # the window is still open
    assert lqw.write_all(now=NOW + 1)["diagnostic"] == 0 and len(lane_quality.read_rows()) == 1   # same counts: no repeat row
    # a later batch of judged pairs brings the share to 16/89 = 18% (<= 20%): the window closes informative
    _write_verdicts(_pairs(13, 13, n=13, start=300, ts=flip + 5000))
    out = lqw.write_all(now=NOW + 2)
    final = lane_quality.read_rows()[-1]
    assert out["written"] == 1 and out["groups"]["tech_op"]["status"] == "written"
    assert (final["window_complete"], final["informative"], final["n"], final["cannot_judge"]) == (True, True, 73, 16)
    assert final["close_reason"] == "n_reached" and lane_quality.sampled_lanes(lane_quality.read_rows(), NOW + 3)


def test_28_days_after_the_flip_with_the_last_batch_above_20_percent_is_not_informative(on):
    flip = NOW - 29 * DAYS
    assert lane_a_shadow.record_flip(flip)
    _write_verdicts(_cj_batch(70, 20, t=flip + DAYS))                         # 20/90 = 22%, n well above 60
    out = lqw.write_all(now=NOW)
    (row,) = lane_quality.read_rows()
    assert out["groups"]["tech_op"]["status"] == "not_informative"
    assert (row["window_complete"], row["informative"], row["close_reason"], row["not_informative_reason"]) == (
        True, False, "window_28d", "cannot_judge_share")
    assert row["n"] == 70 and row["gap_pts"] is None and lane_quality.sampled_lanes([row], NOW) == {}


def test_28_days_after_the_flip_with_the_last_batch_at_20_percent_and_n_60_is_informative(on):
    flip = NOW - 29 * DAYS
    lane_a_shadow.record_flip(flip)
    _write_verdicts(_cj_batch(60, 15, t=flip + DAYS))                         # exactly 20%
    lqw.write_all(now=NOW)
    (row,) = lane_quality.read_rows()
    assert row["informative"] is True and row["close_reason"] == "n_reached"


SWITCHES = dict(yaml_enabled=True, env_on=True, haiku_rewrite=True, shadow_on=True)


async def test_the_flip_is_written_on_an_ineligible_request_with_all_four_on_and_records_the_values(
        home, tmp_path, monkeypatch):
    monkeypatch.setenv(sf.ENV_FLAG, "on")
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    up = UpstreamU()
    app = _app(tmp_path, _raw(), up)
    assert not lane_a_shadow.flip_path().exists()
    t0 = time.time()
    r = await _post(app, _body(continuation=False, sid="s-first"))             # a turn-first step: lane A cannot serve it
    assert r.status_code == 200
    (row,) = ledger.read_rows(tmp_path / "proxy_calls.jsonl")
    assert row["lane_a_reason"] != "served" and "lane" not in row
    flip = lane_a_shadow.read_flip_row()
    assert flip is not None and t0 - 0.01 <= flip["flip_ts"] <= time.time() + 0.01   # flip_ts is rounded to ms
    assert flip["switches"] == SWITCHES                                          # the four values it saw, auditable
    assert stat.S_IMODE(lane_a_shadow.flip_path().stat().st_mode) == 0o600
    # first write wins: later requests (eligible or not) change nothing
    first = lane_a_shadow.flip_path().read_text()
    for b in (_body(sid="s2"), _body(continuation=False, sid="s3")):
        assert (await _post(app, b)).status_code == 200
    assert lane_a_shadow.flip_path().read_text() == first and lane_a_shadow.read_flip() == flip["flip_ts"]


@pytest.mark.parametrize("off", ["yaml", "env", "rewrite", "shadow"])
async def test_the_flip_is_not_written_with_any_one_switch_off_and_the_state_dir_is_untouched(
        home, tmp_path, monkeypatch, off):
    if off != "shadow":
        monkeypatch.setenv(sf.ENV_FLAG, "on")
    if off != "env":
        monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    raw = _raw(OFF_LANE if off == "yaml" else LANE_ON, **({"haiku_rewrite": False} if off == "rewrite" else {}))
    app = _app(tmp_path, raw, UpstreamU())
    before = {str(p.relative_to(home)) for p in home.rglob("*")} if home.exists() else set()
    for b in (_body(continuation=False, sid="a"), _body(sid="b")):
        assert (await _post(app, b)).status_code == 200
    assert not lane_a_shadow.flip_path().exists()
    after = {str(p.relative_to(home)) for p in home.rglob("*")} if home.exists() else set()
    assert not [f for f in after - before if "lane_a_flip" in f]


@pytest.mark.parametrize("off", list(SWITCHES))
def test_maybe_record_flip_needs_all_four(on, off):
    assert lane_a_shadow.maybe_record_flip(NOW, **{**SWITCHES, off: False}) is False
    assert not lane_a_shadow.flip_path().exists()
    assert lane_a_shadow.maybe_record_flip(NOW, **SWITCHES) is True
    assert lane_a_shadow.maybe_record_flip(NOW + 9, **SWITCHES) is False and lane_a_shadow.read_flip() == NOW


def test_the_old_first_served_step_flip_trigger_is_gone(on):
    assert not hasattr(sf.FrontierShadow, "_record_flip")


async def test_a_served_step_alone_no_longer_stamps_the_flip(on):
    _usage()
    await _sample(_shadow(), FakeReplay(), _ctx(), task_id="served")          # sampler path only: no proxy request
    assert not lane_a_shadow.flip_path().exists()


def test_with_no_flip_the_n_60_close_works_and_the_28_day_close_never_fires(on):
    assert lane_a_shadow.read_flip() is None
    _write_verdicts(_pairs(60, 60, n=60))
    out = lqw.write_all(now=NOW)
    assert out["written"] == 1 and out["flip_ts"] is None
    (row,) = lane_quality.read_rows()
    assert (row["close_reason"], row["informative"], row["flip_ts"]) == ("n_reached", True, None)
    # a far-future clock with only 10 pairs and no flip: still nothing
    paths.state_path(lane_quality.FILE).unlink()
    sf.verdicts_path().unlink()
    _write_verdicts(_pairs(10, 10, n=10, ts=NOW + 400 * DAYS))
    assert lqw.write_all(now=NOW + 401 * DAYS)["written"] == 0
    assert not paths.state_path(lane_quality.FILE).exists()


async def test_the_sampler_stops_for_a_task_group_once_its_window_is_final(on):
    _usage()
    _write_verdicts(_pairs(60, 60, n=60))
    assert lqw.write_all(now=NOW)["written"] == 1                              # tech_op is final
    fs, rep = _shadow(), FakeReplay()
    hit = _step_with_key(True)                                                  # a key the 2% rate WOULD sample
    await _sample(fs, rep, _ctx(n=hit), task_id="after-final")
    last = _ledger()[-1]
    assert (last["outcome"], last["reason"]) == ("skipped", "window_final") and rep.calls == []
    # another, still open task group keeps sampling (fill: every step)
    await _sample(fs, rep, _ctx(n=hit, group="code"), task_id="other-group")
    assert _ledger()[-1]["outcome"] == "replayed" and len(rep.calls) == 1
    # and an open diagnostic row (non-final) does not stop the sampler
    paths.state_path(lane_quality.FILE).unlink()
    sf.verdicts_path().unlink()
    _write_verdicts(_cj_batch(60, 16, t=NOW - 2000))
    assert lqw.write_all(now=NOW)["diagnostic"] == 1
    await _sample(fs, rep, _ctx(n=_step_with_key(True, session="sess-b"), session="sess-b"), task_id="still-open")
    assert _ledger()[-1]["outcome"] == "replayed"


def _final(now=NOW, **kw):
    counts = {"n": 64, "k_a": 62, "k_f": 63, "cannot_judge": 0, "ids": [f"t{i}" for i in range(64)]}
    row, status = lqw.batch_row("tech_op", counts, now=now, since=now - 14 * DAYS, until=now)
    assert status == "written"
    return {**row, **kw}


def test_a_final_row_older_than_28_days_still_counts_and_its_age_is_reported(on):
    row = _final(now=NOW - 90 * DAYS)
    assert lane_quality.sampled_lanes([row], NOW)
    (info,) = lane_quality.sampled_lanes([row], NOW).values()
    assert info["age_days"] == 90.0
    # an audit row keeps the 28-day read cap
    audit = {"ts": NOW - 29 * DAYS, "door": "proxy", "served_class": "haiku", "task_group": "qa", "kind": "audit",
             "n": 30, "acceptable": 30, "source": "a"}
    assert lane_quality.sampled_lanes([audit], NOW) == {}
    # a final row dated after now is not counted yet
    assert lane_quality.sampled_lanes([_final(now=NOW + 60)], NOW) == {}


@pytest.mark.parametrize("key", list(lane_a_shadow.IDENTITY_KEYS))
def test_a_final_row_with_a_changed_identity_is_invalidated_and_not_counted(on, key):
    row = _final()
    assert set(row["identity"]) == set(lane_a_shadow.IDENTITY_KEYS) and lane_quality.sampled_lanes([row], NOW)
    stale = dict(row, identity={**row["identity"], key: "something-else"})
    sampled, invalid = lane_quality.sampled_report([stale], NOW)
    assert sampled == {}
    assert invalid[("proxy", "haiku", "tech_op")]["mismatch"] == [key]
    assert invalid[("proxy", "haiku", "tech_op")]["age_days"] == 0.0
    # the writer does not open a new window by itself
    sf._append_private(paths.state_path(lane_quality.FILE), stale)
    _write_verdicts(_pairs(60, 60, n=60))
    out = lqw.write_all(now=NOW + 5)
    assert out["written"] == 0 and out["groups"]["tech_op"]["status"] == "window_closed"
    # an unstamped final row cannot be shown valid either
    bare = {k: v for k, v in row.items() if k != "identity"}
    assert lane_quality.sampled_lanes([bare], NOW) == {}


def test_the_identities_follow_the_lane_a_config_judge_brief_and_breaker(on, monkeypatch):
    from tests.test_proxy_lane_a import _policy

    base = lane_a_shadow.identities_of(_policy())
    assert base == lane_a_shadow.identities_of(_policy())
    raw = _raw()
    raw["tiers"][0]["model"] = "claude-haiku-5-5"
    changed = {
        "lane_a_model": lane_a_shadow.identities_of(pt.ClaudeTierPolicy.from_dict(raw)),
        "prompt_set_sha": lane_a_shadow.identities_of(_policy(LANE_ON | {"tau": 0.7})),
        "breaker_sha": lane_a_shadow.identities_of(_policy(LANE_ON | {"breaker": {"trip_failures": 7}})),
    }
    for key, ident in changed.items():
        assert [k for k in ident if ident[k] != base[k]] == [key]
    assert lane_a_shadow.identities_of(_policy(LANE_ON | {"prev_tool_classes": ["technical_op", "web"]}))[
        "prompt_set_sha"] != base["prompt_set_sha"]
    monkeypatch.setattr(sf, "JUDGE_BRIEF_SHA256", "f" * 64)
    now_ident = lane_a_shadow.identities_of(_policy())
    assert [k for k in now_ident if now_ident[k] != base[k]] == ["judge_brief_sha"]


def test_the_s1_action_units_report_the_invalidated_lane_and_it_earns_no_credit(on):
    stale = _audit_row("tech_op")
    stale["identity"] = {**stale["identity"], "lane_a_model": "claude-haiku-9"}
    res = _units(_kpi_rows(), [stale | {"ts": KPI_NOW - 40 * DAYS}])
    assert res["sampled"] == {} and list(res["invalidated"]) == [("proxy", "haiku", "tech_op")]
    assert res["invalidated"][("proxy", "haiku", "tech_op")]["mismatch"] == ["lane_a_model"]
    assert osh.summarize_actions(res["actions"])["lane_a"]["k"] == 0
    good = _units(_kpi_rows(), [_audit_row("tech_op") | {"ts": KPI_NOW - 40 * DAYS}])
    assert list(good["sampled"].values())[0]["age_days"] == pytest.approx(40.0, abs=0.1)
    assert osh.summarize_actions(good["actions"])["lane_a"]["k"] == 3 and good["invalidated"] == {}


def test_should_a_a_writer_that_first_runs_at_flip_plus_40_days_clamps_the_pair_window_to_day_28(on):
    flip = NOW - 40 * DAYS
    assert lane_a_shadow.record_flip(flip)
    _write_verdicts(_pairs(50, 50, n=50, ts=flip + 2 * DAYS)                    # 50 pairs before day 28
                    + _pairs(20, 20, n=20, start=500, ts=flip + 29 * DAYS))      # 20 more on day 29
    out = lqw.write_all(now=NOW)
    (row,) = lane_quality.read_rows()
    assert out["groups"]["tech_op"]["n"] == 50 and out["groups"]["tech_op"]["status"] == "not_informative"
    assert (row["n"], row["window_complete"], row["informative"], row["close_reason"],
            row["not_informative_reason"]) == (50, True, False, "window_28d", "n_below_min")
    assert row["until"] == flip + 28 * DAYS
    assert lane_quality.sampled_lanes([row], NOW) == {}
    # an explicit later --until cannot widen the window either
    paths.state_path(lane_quality.FILE).unlink()
    out = lqw.write_all(now=NOW, until=NOW + 5 * DAYS)
    assert out["until"] == flip + 28 * DAYS and out["groups"]["tech_op"]["n"] == 50


def test_nit_2_pairs_from_before_the_flip_are_counted_in_the_written_rows_excluded(on):
    flip = NOW - 20 * DAYS
    lane_a_shadow.record_flip(flip)
    _write_verdicts(_pairs(60, 60, n=60, ts=flip + 100)
                    + [_verdict(6000 + i, ts=flip - 100 - i) for i in range(7)])
    lqw.write_all(now=NOW)
    (row,) = lane_quality.read_rows()
    assert row["n"] == 60 and row["excluded"] == {"before_flip": 7}


async def test_nit_1_a_replay_without_a_sha_leaves_replay_body_sha_none_and_voids_the_pair(on):
    _usage()
    await _sample(_shadow(), FakeReplay(), _ctx(), task_id="no-sha")             # FakeReplay reports no sent_sha
    (pair,) = [json.loads(line) for f in sf.pairs_dir().glob("*.jsonl") for line in f.read_text().splitlines()]
    assert "replay_body_sha" in pair and pair["replay_body_sha"] is None         # not the served-side sha
    verdict = _verdict(1, served_body_sha=pair["served_body_sha"] or "served-sha", replay_body_sha=None)
    t = lqw.tally([verdict], since=0, until=NOW + 1)["tech_op"]
    assert t["n"] == 0 and t["excluded"] == {"body_sha_mismatch": 1}
