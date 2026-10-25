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
           "judge_brief_sha256": sf.JUDGE_BRIEF_SHA256, "lane": lane, "task_group": group, "rate_phase": phase}
    row.update(kw)
    if lane is None:
        for k in ("lane", "task_group", "rate_phase", "judge_brief_sha256"):
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
                   "prev_tool_class": "technical_op", "conf": 0.7}
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


def _pairs(k_a, k_f, n=60, *, group="tech_op", start=0):
    """n decided pairs with exactly k_a Haiku-acceptable and k_f Frontier-acceptable (spread over the rows)."""
    return [_verdict(start + i, group=group, a=i < k_a, f=i < k_f) for i in range(n)]


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
    sf.verdicts_path().unlink()
    _write_verdicts(_pairs(60, 57))
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


def test_writer_refuses_a_batch_when_cannot_judge_is_over_20_percent(on):
    _write_verdicts(_pairs(60, 60, n=60) + [_verdict(200 + i, cj=True) for i in range(16)])   # 16/76 = 21%
    out = lqw.write_all(now=NOW)
    assert out["groups"]["tech_op"]["status"] == "cannot_judge_share" and out["written"] == 0
    assert not paths.state_path(lane_quality.FILE).exists()


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
    assert again["written"] == 0 and again["groups"]["tech_op"]["status"] == "already_written"
    assert len(lane_quality.read_rows()) == 1
    f = paths.state_path(lane_quality.FILE)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    _write_verdicts([_verdict(900)])                                                  # a 61st pair: a new batch
    assert lqw.write_all(now=NOW + 10)["written"] == 1 and len(lane_quality.read_rows()) == 2


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
        ("proxy", "haiku", "tech_op"): {"kind": "shadow_pairs", "n": 60, "ts": rows[0]["ts"], "source": rows[0]["source"]}}
    # a newer failing batch un-samples it (the latest batch decides)
    worse = dict(rows[0], ts=NOW + 5, gap_pts=9.0, source="newer")
    assert lane_quality.sampled_lanes(rows + [worse], NOW + 10) == {}


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


def test_haiku_block_reason_is_unchanged_and_exposes_its_estimate():
    body = _body()
    reason, tokens = pt._haiku_block(body, False, None)
    assert reason == pt.haiku_block_reason(body) == pt.HAIKU_BLOCK_NONE and tokens == pt._approx_context_tokens(body)
    media = _body(extra_user=[{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}}])
    assert pt._haiku_block(media, False, None) == (pt.HAIKU_BLOCK_MEDIA, None)


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
