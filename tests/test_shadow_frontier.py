"""GE4 Frontier shadow (PLAN v16 GE4, OD-4 = A): ``llm_router.shadow_frontier``.

Every Frontier call here is a fake (an async replay function, a mocked Anthropic, a fake
``claude -p`` runner), so no test makes a network call or spends quota. Covered: the flag
(off = 0 calls, no files), every cap (calls/day, input tokens/day, session/weekly quota,
stale/unknown quota, sample rate, one in flight), the proxy wiring (haiku_rewrite rows only,
the client's original bytes, the response never waits on the replay), no text in any ledger,
the 14-day text retention, and the blind judge whose verdicts ``haiku_guard`` reads.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import time
from datetime import datetime, timezone

import httpx
import pytest

from llm_router import failopen, paths
from llm_router import shadow_frontier as sf
from llm_router.proxy import backends as pb
from llm_router.proxy import haiku_guard
from llm_router.proxy import tiers as pt
from tests.test_proxy_tiers import HAIKU, OPUS, _app, _first, _no_system_reminders, _post, _req, _sse

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc).timestamp()
DAY = "2026-10-08"
CANARY_PROMPT = "CANARY-PROMPT-7f3a9c"
CANARY_CHEAP = "CANARY-CHEAP-41d2e8"
CANARY_FRONTIER = "CANARY-FRONTIER-b07c55"
RAW = json.dumps({"model": OPUS, "max_tokens": 100, "stream": True,
                  "messages": [{"role": "user", "content": [{"type": "text", "text": f"fix it {CANARY_PROMPT}"}]}]
                  }).encode()
CHEAP_USAGE = {"input_tokens": 100, "cache_read_input_tokens": 800, "cache_creation_input_tokens": 0,
               "output_tokens": 20}


def _sse_text(model: str, text: str, usage: dict) -> bytes:
    events = [
        {"type": "message_start", "message": {"id": "msg_f", "model": model, "content": [], "usage": usage}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 33}},
        {"type": "message_stop"},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


FRONTIER_USAGE = {"input_tokens": 7, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 50,
                  "output_tokens": 1}
CHEAP_REPLY = _sse_text(HAIKU, f"cheap says {CANARY_CHEAP}", CHEAP_USAGE)


class FakeReplay:
    def __init__(self, status=200, *, gate: asyncio.Event | None = None, exc: Exception | None = None):
        self.calls: list[bytes] = []
        self.status, self.gate, self.exc = status, gate, exc

    async def __call__(self, content: bytes) -> sf.Replay:
        self.calls.append(content)
        if self.gate is not None:
            await self.gate.wait()
        if self.exc is not None:
            raise self.exc
        return sf.Replay(self.status, _sse_text(OPUS, f"frontier says {CANARY_FRONTIER}", FRONTIER_USAGE),
                         "text/event-stream")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(h))
    monkeypatch.delenv("LLM_ROUTER_HAIKU_GUARD_SHADOW_VERDICTS", raising=False)
    monkeypatch.delenv(sf.ENV_FLAG, raising=False)
    return h


@pytest.fixture
def on(home, monkeypatch):
    monkeypatch.setenv(sf.ENV_FLAG, "on")
    return home


def _usage(session=10.0, weekly=20.0, *, updated=NOW, **extra):
    p = paths.state_path("usage.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    row = {"session_pct": session, "weekly_pct": weekly, "updated_at": updated, **extra}
    p.write_text(json.dumps({k: v for k, v in row.items() if v is not None}))


def _ledger() -> list[dict]:
    return haiku_guard.read_jsonl(sf.ledger_path())


def _seed_ledger(rows: list[dict]) -> None:
    for r in rows:
        sf._append_private(sf.ledger_path(), r)


def _shadow(rng=0.0, clock=NOW) -> sf.FrontierShadow:
    vals = list(rng) if isinstance(rng, (list, tuple)) else None
    return sf.FrontierShadow(rng=(lambda: vals.pop(0)) if vals is not None else (lambda: rng),
                             clock=lambda: clock)


async def _sample(fs, replay, *, served=HAIKU, requested=OPUS, usage=CHEAP_USAGE, status=200, task_id="msg_1"):
    task = fs.maybe_sample(RAW, served, requested, sf.DOOR_PROXY, task_id, replay=replay, cheap_status=status,
                           cheap_reply=CHEAP_REPLY, cheap_ctype="text/event-stream", cheap_usage=usage)
    await fs.drain()
    return task


# ── the switch ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [None, "", "off", "0", "false", "no", "maybe"])
async def test_flag_off_means_no_task_no_frontier_call_and_no_file(home, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv(sf.ENV_FLAG, value)
    _usage()
    replay = FakeReplay()
    assert await _sample(_shadow(), replay) is None
    assert replay.calls == []
    assert not sf.shadow_dir().exists()


@pytest.mark.parametrize("value", ["on", "1", "true", "YES"])
def test_flag_values_that_switch_it_on(monkeypatch, value):
    monkeypatch.setenv(sf.ENV_FLAG, value)
    assert sf.enabled() is True


def test_the_flag_is_registered():
    from llm_router import env_registry
    assert sf.ENV_FLAG in env_registry.ENV_REGISTRY


# ── replay, ledger, pair store ──────────────────────────────────────────────


async def test_replays_the_original_bytes_and_sums_tokens_from_the_replays_own_usage(on):
    _usage()
    replay = FakeReplay()
    await _sample(_shadow(), replay)
    assert replay.calls == [RAW]  # unmodified original body, once
    (row,) = _ledger()
    assert row["outcome"] == sf.OUT_REPLAYED and row["reason"] is None
    assert row["input_tokens"] == 7 + 1000 + 50  # from the replay's usage
    assert row["est_input_tokens"] == 900  # the admission estimate, from the Haiku call
    assert row["output_tokens"] == 33 and row["frontier_status"] == 200
    assert (row["served"], row["requested"], row["door"], row["task_id"]) == (HAIKU, OPUS, "proxy", "msg_1")
    assert row["body_sha"] == sf.body_sha(RAW) and row["day"] == DAY
    (pair,) = haiku_guard.read_jsonl(sf.pairs_dir() / f"{DAY}.jsonl")
    assert CANARY_CHEAP in pair["cheap_response"] and CANARY_FRONTIER in pair["frontier_response"]
    assert CANARY_PROMPT in pair["request_context"]
    assert sf.day_spend(DAY) == (1, 1057)


async def test_not_rewritten_and_failed_cheap_calls_are_skipped_without_a_call(on):
    _usage()
    replay = FakeReplay()
    await _sample(_shadow(), replay, served=OPUS, requested=OPUS)
    await _sample(_shadow(), replay, status=400)
    assert replay.calls == []
    assert [r["reason"] for r in _ledger()] == [sf.R_NOT_REWRITTEN, sf.R_CHEAP_FAILED]


async def test_a_failed_replay_is_a_ledger_row_counted_against_the_caps_and_stores_no_pair(on):
    _usage()
    await _sample(_shadow(), FakeReplay(exc=httpx.ReadTimeout("slow")))
    await _sample(_shadow(), FakeReplay(status=529))
    rows = _ledger()
    assert [(r["outcome"], r["reason"]) for r in rows] == [(sf.OUT_REPLAY_FAILED, "ReadTimeout"),
                                                          (sf.OUT_REPLAY_FAILED, "status")]
    assert rows[1]["frontier_status"] == 529
    assert sf.day_spend(DAY) == (2, 1800)  # unknown spend counts at the admission estimate
    assert not sf.pairs_dir().exists()


# ── caps ────────────────────────────────────────────────────────────────────


def _spent(n: int, tokens_each: int = 1, day: str = DAY, outcome: str = sf.OUT_REPLAYED) -> list[dict]:
    return [{"day": day, "outcome": outcome, "input_tokens": tokens_each} for _ in range(n)]


async def test_at_most_20_frontier_calls_per_utc_day(on):
    _usage()
    _seed_ledger(_spent(19) + _spent(5, day="2026-10-07") + [{"day": DAY, "outcome": "skipped"}] * 3)
    replay = FakeReplay()
    await _sample(_shadow(), replay)  # the 20th call of the day
    assert len(replay.calls) == 1
    await _sample(_shadow(), replay)  # the 21st is refused
    assert len(replay.calls) == 1
    assert _ledger()[-1]["reason"] == sf.R_CAP_CALLS
    assert sf.day_spend(DAY)[0] == sf.MAX_CALLS_PER_DAY == 20


async def test_failed_replays_count_toward_the_daily_call_cap(on):
    _usage()
    _seed_ledger(_spent(20, outcome=sf.OUT_REPLAY_FAILED))
    replay = FakeReplay()
    await _sample(_shadow(), replay)
    assert replay.calls == [] and _ledger()[-1]["reason"] == sf.R_CAP_CALLS


async def test_at_most_400k_input_tokens_per_utc_day_including_the_next_call(on):
    _usage()
    _seed_ledger(_spent(1, 390_000))
    replay = FakeReplay()
    await _sample(_shadow(), replay, usage={"input_tokens": 7_408})  # x1.35 = 10_001
    assert replay.calls == [] and _ledger()[-1]["reason"] == sf.R_CAP_TOKENS
    await _sample(_shadow(), replay, usage={"input_tokens": 7_407})  # x1.35 = 10_000: lands exactly on 400k
    assert len(replay.calls) == 1
    assert sf.ADMIT_SAFETY == 1.35
    assert sf.MAX_INPUT_TOKENS_PER_DAY == 400_000


async def test_unknown_input_size_is_not_replayed(on):
    _usage()
    replay = FakeReplay()
    await _sample(_shadow(), replay, usage=None)
    assert replay.calls == [] and _ledger()[-1]["reason"] == sf.R_SIZE_UNKNOWN


@pytest.mark.parametrize("session,weekly,updated,extra,want", [
    (80.0, 10.0, NOW, {}, sf.R_QUOTA_SESSION),
    (79.9, 84.9, NOW, {}, None),
    (10.0, 85.0, NOW, {}, sf.R_QUOTA_WEEKLY),
    (10.0, 10.0, NOW - 1801, {}, sf.R_QUOTA_STALE),
    (10.0, 10.0, NOW - 1799, {}, None),
    (10.0, None, NOW, {}, sf.R_QUOTA_UNKNOWN),
    (None, 10.0, NOW, {}, sf.R_QUOTA_UNKNOWN),
    (10.0, 10.0, None, {}, sf.R_QUOTA_UNKNOWN),
    (10.0, 10.0, NOW, {"pending": True}, sf.R_QUOTA_UNKNOWN),
    (10.0, 10.0, NOW, {"is_fallback": True}, sf.R_QUOTA_UNKNOWN),
])
async def test_quota_guards(on, session, weekly, updated, extra, want):
    _usage(session, weekly, updated=updated, **extra)
    replay = FakeReplay()
    await _sample(_shadow(), replay)
    assert len(replay.calls) == (1 if want is None else 0)
    assert _ledger()[-1]["reason"] == want


async def test_no_usage_file_means_no_shadow(on):
    replay = FakeReplay()
    await _sample(_shadow(), replay)
    assert replay.calls == [] and _ledger()[-1]["reason"] == sf.R_QUOTA_UNKNOWN


async def test_sample_rate_is_100_percent_for_14_days_then_2_percent(on):
    _usage()
    replay = FakeReplay()
    await _sample(_shadow(rng=0.999), replay)  # first run: started.json written now, rate 1.0
    assert len(replay.calls) == 1
    assert json.loads((sf.shadow_dir() / "started.json").read_text())["ts"] == NOW
    day13 = NOW + 13.9 * 86400
    _usage(updated=day13)
    await _sample(_shadow(rng=0.999, clock=day13), replay)
    assert len(replay.calls) == 2
    day15 = NOW + 15 * 86400
    _usage(updated=day15)
    await _sample(_shadow(rng=0.02, clock=day15), replay)
    assert len(replay.calls) == 2 and _ledger()[-1]["reason"] == sf.R_NOT_SAMPLED
    await _sample(_shadow(rng=0.0199, clock=day15), replay)
    assert len(replay.calls) == 3
    assert (sf.FULL_RATE, sf.STEADY_RATE) == (1.0, 0.02)


async def test_at_most_one_replay_in_flight(on):
    _usage()
    gate = asyncio.Event()
    replay = FakeReplay(gate=gate)
    fs = _shadow()
    first = fs.maybe_sample(RAW, HAIKU, OPUS, "proxy", "a", replay=replay, cheap_reply=CHEAP_REPLY,
                            cheap_ctype="text/event-stream", cheap_usage=CHEAP_USAGE)
    for _ in range(200):
        if replay.calls:
            break
        await asyncio.sleep(0.01)
    second = fs.maybe_sample(RAW, HAIKU, OPUS, "proxy", "b", replay=replay, cheap_usage=CHEAP_USAGE)
    await second
    assert len(replay.calls) == 1
    assert [(r["task_id"], r["reason"]) for r in _ledger()] == [("b", sf.R_IN_FLIGHT)]
    gate.set()
    await first
    await _sample(fs, replay, task_id="c")  # released: the next one goes through
    assert len(replay.calls) == 2


async def test_maybe_sample_never_raises_and_releases_the_slot(on, monkeypatch):
    _usage()
    fs = _shadow()

    class Boom:
        def create_task(self, coro):
            coro.close()
            raise RuntimeError("no loop")
    monkeypatch.setattr(sf.asyncio, "get_running_loop", lambda: Boom())
    before = failopen.snapshot().by_code.get("LR-FO-SHADOW-FRONTIER-START", 0)
    assert fs.maybe_sample(RAW, HAIKU, OPUS, "proxy", "x", replay=FakeReplay(), cheap_usage=CHEAP_USAGE) is None
    assert failopen.snapshot().by_code.get("LR-FO-SHADOW-FRONTIER-START", 0) == before + 1
    assert fs._busy is False


# ── retention and privacy ───────────────────────────────────────────────────


async def test_pair_text_is_0600_and_deleted_after_14_days(on):
    _usage()
    sf.pairs_dir().mkdir(parents=True)
    old, edge = sf.pairs_dir() / "2026-09-23.jsonl", sf.pairs_dir() / "2026-09-24.jsonl"
    old.write_text("{}\n")
    edge.write_text("{}\n")
    await _sample(_shadow(), FakeReplay())
    assert not old.exists() and edge.exists()  # 15 days old goes, 14 days old stays
    today = sf.pairs_dir() / f"{DAY}.jsonl"
    assert stat.S_IMODE(today.stat().st_mode) == 0o600
    assert stat.S_IMODE(sf.ledger_path().stat().st_mode) == 0o600


def _judge_ok(a=True, b=False, cost=0.01, model="claude-sonnet-5-5"):
    calls: list[tuple[str, str]] = []

    def run(brief, item):
        calls.append((brief, item))
        return {"result": json.dumps({"acceptable_A": a, "acceptable_B": b, "cannot_judge": False}),
                "cost_usd": cost, "model_ok": "sonnet" in model}
    run.calls = calls
    return run


async def test_no_prompt_or_response_text_in_any_ledger(on):
    _usage()
    await _sample(_shadow(), FakeReplay())
    run = _judge_ok()
    assert sf.judge_pending(run=run, rng=lambda: 0.1, clock=lambda: NOW)["verdicts"] == 1
    ledgers = [sf.ledger_path(), sf.judge_ledger_path(), sf.verdicts_path()]
    for path in ledgers:
        text = path.read_text()
        assert text.strip(), path  # each ledger has a row, so the check is not vacuous
        for canary in (CANARY_PROMPT, CANARY_CHEAP, CANARY_FRONTIER, "fix it", "says"):
            assert canary not in text, (path.name, canary)
    # the text lives only in the pair store, which the judge reads
    pair_text = (sf.pairs_dir() / f"{DAY}.jsonl").read_text()
    assert CANARY_PROMPT in pair_text and CANARY_CHEAP in pair_text and CANARY_FRONTIER in pair_text
    (_, item), = run.calls
    assert CANARY_PROMPT in item and HAIKU not in item and OPUS not in item  # blind: no model ids


# ── blind judge ─────────────────────────────────────────────────────────────


def _pairs(n: int, ts0: float = NOW) -> None:
    for i in range(n):
        sf._append_private(sf.pairs_dir() / f"{DAY}.jsonl", {
            "ts": ts0 + i, "task_id": f"t{i}", "door": "proxy", "served": HAIKU, "requested": OPUS,
            "body_sha": f"{i:064x}", "request_context": f"ask {i}", "cheap_response": f"cheap {i}",
            "frontier_response": f"frontier {i}"})


def test_judge_shuffles_a_b_and_maps_the_labels_back(on):
    _usage()
    _pairs(2)
    run = _judge_ok(a=True, b=False)
    out = sf.judge_pending(run=run, rng=iter([0.1, 0.9]).__next__, clock=lambda: NOW)
    assert out == {"status": "ok", "judged": 2, "verdicts": 2, "cannot_judge": 0, "failed": 0}
    (_, item0), (_, item1) = run.calls
    assert item0.index("cheap 0") < item0.index("frontier 0")  # cheap is A
    assert item1.index("frontier 1") < item1.index("cheap 1")  # cheap is B
    v0, v1 = haiku_guard.read_jsonl(sf.verdicts_path())
    assert (v0["cheap_label"], v0["acceptable"], v0["frontier_acceptable"]) == ("A", True, False)
    assert (v1["cheap_label"], v1["acceptable"], v1["frontier_acceptable"]) == ("B", False, True)
    assert v0["ts"] == NOW and v0["cannot_judge"] is False and v0["judge_model"] == "sonnet"


def test_verdicts_have_the_shape_haiku_guard_reads(on):
    _usage()
    _pairs(20)
    results = iter([True] * 17 + [False] * 3)

    def run(brief, item):
        ok = next(results)
        return {"result": json.dumps({"acceptable_A": ok, "acceptable_B": ok, "cannot_judge": False}),
                "cost_usd": 0.0, "model_ok": True}

    out = sf.judge_pending(run=run, rng=lambda: 0.1, clock=lambda: NOW)
    assert out["verdicts"] == 20 and out["status"] == "ok"
    rows = haiku_guard.read_jsonl(haiku_guard.shadow_verdicts_path())
    trig = haiku_guard.shadow_trigger(rows, until=NOW + 3600)
    assert (trig["n"], trig["k"], trig["evaluable"], trig["tripped"]) == (20, 17, True, True)  # 17/20 < 26/30


def test_cannot_judge_rows_are_counted_and_left_out_of_n(on):
    _usage()
    _pairs(3)

    def run(brief, item):
        cj = "ask 0" in item
        return {"result": json.dumps({"acceptable_A": None if cj else True, "acceptable_B": None if cj else True,
                                      "cannot_judge": cj}), "cost_usd": 0.0, "model_ok": True}
    out = sf.judge_pending(run=run, rng=lambda: 0.1, clock=lambda: NOW)
    assert out["cannot_judge"] == 1 and out["verdicts"] == 3
    trig = haiku_guard.shadow_trigger(haiku_guard.read_jsonl(sf.verdicts_path()), until=NOW + 60)
    assert (trig["n"], trig["k"], trig["cannot_judge"]) == (2, 2, 1)


def test_judge_flag_off_and_quota_pause_make_no_call(home, monkeypatch):
    _usage()
    _pairs(2)
    run = _judge_ok()
    assert sf.judge_pending(run=run, clock=lambda: NOW)["status"] == "off"
    monkeypatch.setenv(sf.ENV_FLAG, "on")
    _usage(session=80.0)
    assert sf.judge_pending(run=run, clock=lambda: NOW)["status"] == sf.R_QUOTA_SESSION
    _usage(weekly=85.0)
    assert sf.judge_pending(run=run, clock=lambda: NOW)["status"] == sf.R_QUOTA_WEEKLY
    _usage(updated=NOW - 1801)
    assert sf.judge_pending(run=run, clock=lambda: NOW)["status"] == sf.R_QUOTA_STALE
    assert run.calls == []


def test_judge_daily_cap_and_one_attempt_per_pair(on):
    _usage()
    _pairs(25)
    run = _judge_ok()
    out = sf.judge_pending(run=run, rng=lambda: 0.1, clock=lambda: NOW)
    assert out["status"] == sf.R_CAP_CALLS and len(run.calls) == sf.JUDGE_MAX_CALLS_PER_DAY == 20
    assert sf.judge_pending(run=run, rng=lambda: 0.1, clock=lambda: NOW)["status"] == sf.R_CAP_CALLS
    assert len(run.calls) == 20  # still capped today
    _usage(updated=NOW + 86400)
    out = sf.judge_pending(run=run, rng=lambda: 0.1, clock=lambda: NOW + 86400)
    assert len(run.calls) == 25 and out["judged"] == 5  # next day: only the 5 never tried


def test_a_wrong_judge_model_or_unparsable_reply_writes_no_verdict(on):
    _usage()
    _pairs(2)
    replies = iter([{"result": '{"acceptable_A": true, "acceptable_B": true}', "cost_usd": 0.02, "model_ok": False},
                    {"result": "I think A is fine", "cost_usd": 0.02, "model_ok": True}])
    out = sf.judge_pending(run=lambda b, i: next(replies), rng=lambda: 0.1, clock=lambda: NOW)
    assert out["verdicts"] == 0 and out["failed"] == 2
    assert not sf.verdicts_path().exists()
    assert [r["outcome"] for r in haiku_guard.read_jsonl(sf.judge_ledger_path())] == ["model_mismatch", "unparsed"]


def test_claude_judge_is_isolated_never_persists_and_bypasses_the_proxy(on, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8787")
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, **kw)
        out = {"result": '{"acceptable_A": true, "acceptable_B": false, "cannot_judge": false}',
               "total_cost_usd": 0.03, "modelUsage": {"claude-sonnet-5-5": {}}}
        return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")
    monkeypatch.setattr(sf.subprocess, "run", fake_run)
    res = sf.claude_judge(sf.JUDGE_BRIEF, "ITEM")
    assert res == {"result": '{"acceptable_A": true, "acceptable_B": false, "cannot_judge": false}',
                   "cost_usd": 0.03, "model_ok": True}
    argv = seen["argv"]
    assert argv[:2] == ["claude", "-p"] and "--no-session-persistence" in argv
    assert argv[argv.index("--setting-sources") + 1] == "project,local"
    assert argv[argv.index("--tools") + 1] == "" and "--strict-mcp-config" in argv
    assert argv[argv.index("--model") + 1] == "sonnet" and argv[-2:] == ["--", "ITEM"]
    assert "ANTHROPIC_BASE_URL" not in seen["env"] and os.environ["ANTHROPIC_BASE_URL"]
    assert seen["env"]["LLM_ROUTER_SESSION_KIND"] == "research"
    assert seen["stdin"] is subprocess.DEVNULL
    assert str(seen["cwd"]).startswith(str(sf.shadow_dir()))

    def other_model(argv, **kw):
        out = {"result": "{}", "total_cost_usd": 0.0, "modelUsage": {"claude-haiku-4-5": {}}}
        return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")
    monkeypatch.setattr(sf.subprocess, "run", other_model)
    assert sf.claude_judge("b", "i")["model_ok"] is False


# ── proxy wiring ────────────────────────────────────────────────────────────


@pytest.fixture
def simple(monkeypatch):
    async def choose(text, pinned, *, anthropic=False):
        return {"task_type": "query", "complexity": "simple", "chain_head": [], "model": None}
    monkeypatch.setattr(pb, "tier_classify", choose)


class SlowFrontierUpstream:
    """Mocked Anthropic. The third request (the shadow replay) waits on ``gate``."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.gate = asyncio.Event()

    async def __call__(self, request):
        self.requests.append(request)
        if len(self.requests) >= 3:
            await self.gate.wait()
        model = json.loads(request.content)["model"]
        return httpx.Response(200, stream=httpx.ByteStream(_sse(model)),
                              headers={"content-type": "text/event-stream"})


async def test_proxy_flag_off_makes_no_replay(home, tmp_path, simple):
    _usage(updated=time.time())
    up = SlowFrontierUpstream()
    app = _app(tmp_path, up, policy_overrides={"haiku_rewrite": True})
    assert app.state.frontier_shadow is None
    await _post(app, _first())
    r = await _post(app, _no_system_reminders(_req()))
    assert r.status_code == 200 and HAIKU.encode() in r.content
    await asyncio.sleep(0.05)
    assert len(up.requests) == 2
    assert not sf.shadow_dir().exists()


async def test_proxy_replays_haiku_rewrite_rows_in_the_background_without_delaying_the_response(on, tmp_path, simple):
    _usage(updated=time.time())
    up = SlowFrontierUpstream()
    app = _app(tmp_path, up, policy_overrides={"haiku_rewrite": True})
    await _post(app, _first())  # first call: exempt, served as asked, never shadowed
    body = _no_system_reminders(_req())
    # The client has its whole Haiku reply while the replay is still blocked upstream (a
    # relay that waited on the replay would time out here instead of hanging the suite).
    r = await asyncio.wait_for(_post(app, body), 10)
    assert r.status_code == 200 and HAIKU.encode() in r.content
    for _ in range(300):
        if len(up.requests) >= 3:
            break
        await asyncio.sleep(0.01)
    assert len(up.requests) == 3
    replayed = up.requests[2]
    assert replayed.content == json.dumps(body).encode()  # the client's own bytes, unmodified
    assert json.loads(replayed.content)["model"] == OPUS
    assert replayed.headers["authorization"].startswith("Bearer sk-ant-oat01-")
    assert not sf.ledger_path().exists()  # still in flight
    up.gate.set()
    await app.state.frontier_shadow.drain()
    (row,) = _ledger()
    proxy_row = [x for x in haiku_guard.read_jsonl(tmp_path / "proxy_calls.jsonl")][-1]
    assert proxy_row["tier_reason"] == pt.REASON_HAIKU_REWRITE
    assert (row["outcome"], row["served"], row["requested"], row["task_id"]) == (
        sf.OUT_REPLAYED, HAIKU, OPUS, proxy_row["msg_id"])
    assert row["input_tokens"] == 5 + 30_000 + 1_000  # the replay's own usage
    assert len(haiku_guard.read_jsonl(tmp_path / "proxy_calls.jsonl")) == 2  # the replay is not a proxy call


async def test_proxy_does_not_shadow_rows_that_are_not_haiku_rewrite(on, tmp_path, simple):
    _usage(updated=time.time())
    up = SlowFrontierUpstream()
    app = _app(tmp_path, up)  # haiku_rewrite off: the simple turn goes to the Sonnet floor
    await _post(app, _first())
    r = await _post(app, _req())
    assert r.status_code == 200
    await app.state.frontier_shadow.drain()
    assert len(up.requests) == 2
    assert not sf.ledger_path().exists()


class RefuseHaikuUpstream:
    """Mocked Anthropic that 400s any Haiku request and serves the rest."""

    def __init__(self):
        self.models: list[str] = []

    async def __call__(self, request):
        model = json.loads(request.content)["model"]
        self.models.append(model)
        if "haiku" in model:
            return httpx.Response(400, json={"type": "error", "error": {"message": "no"}})
        return httpx.Response(200, stream=httpx.ByteStream(_sse(model)),
                              headers={"content-type": "text/event-stream"})


async def test_a_haiku_rewrite_refused_and_retried_on_the_original_is_not_a_haiku_vs_frontier_pair(on, tmp_path, simple):
    """The row keeps tier_reason=haiku_rewrite but Opus answered: no replay, no pair."""
    _usage(updated=time.time())
    up = RefuseHaikuUpstream()
    app = _app(tmp_path, up, policy_overrides={"haiku_rewrite": True})
    await _post(app, _first())
    r = await _post(app, _no_system_reminders(_req()))
    assert r.status_code == 200
    await app.state.frontier_shadow.drain()
    assert "haiku" in up.models[1] and up.models[2] == OPUS and len(up.models) == 3  # refused Haiku, retry on Opus, no replay
    proxy_row = haiku_guard.read_jsonl(tmp_path / "proxy_calls.jsonl")[-1]
    assert proxy_row["tier_reason"] == pt.REASON_HAIKU_REWRITE and proxy_row["tier_retry"]["status"] == 400
    assert proxy_row["served_model"] == OPUS
    (row,) = _ledger()
    assert (row["outcome"], row["reason"]) == (sf.OUT_SKIPPED, sf.R_NOT_REWRITTEN)
    assert not sf.pairs_dir().exists()


# ── review fixes on #339 ────────────────────────────────────────────────────


def _old_pair_file(day="2026-09-01"):
    sf.pairs_dir().mkdir(parents=True, exist_ok=True)
    f = sf.pairs_dir() / f"{day}.jsonl"
    f.write_text('{"request_context": "secret"}\n')
    return f


def test_judge_prunes_old_pairs_even_when_the_flag_is_off(home):
    f = _old_pair_file()
    assert sf.judge_pending(clock=lambda: NOW)["status"] == "off"
    assert not f.exists()


def test_judge_prunes_old_pairs_even_when_quota_is_paused(on):
    _usage(session=95.0)
    f = _old_pair_file()
    assert sf.judge_pending(clock=lambda: NOW)["status"] == sf.R_QUOTA_SESSION
    assert not f.exists()


async def test_proxy_start_prunes_old_pairs_with_the_flag_off(home, tmp_path):
    f = _old_pair_file()
    fresh = sf.pairs_dir() / f"{DAY}.jsonl"
    fresh.write_text("{}\n")
    ledger_old = {"day": "2026-09-01", "outcome": "skipped"}
    _seed_ledger([ledger_old])
    app = _app(tmp_path, SlowFrontierUpstream())
    assert app.state.frontier_shadow is None
    async with app.router.lifespan_context(app):
        pass
    assert not f.exists()
    assert _ledger() == []


def test_prune_at_start_without_a_shadow_dir_creates_nothing(home):
    sf.prune_at_start(NOW)
    assert not sf.shadow_dir().exists()


def test_ledger_rows_older_than_retention_are_pruned_and_fresh_rows_kept(on):
    keep = [{"day": "2026-09-24", "outcome": "skipped"}, {"day": DAY, "outcome": "replayed", "input_tokens": 5}]
    _seed_ledger([{"day": "2026-09-23", "outcome": "skipped"}] + keep)
    assert sf.prune_ledger(NOW) == 1
    assert _ledger() == keep
    assert stat.S_IMODE(sf.ledger_path().stat().st_mode) == 0o600
    assert sf.prune_ledger(NOW) == 0


def _cut_sse():
    return CHEAP_REPLY[:CHEAP_REPLY.rindex(b"event: message_delta")]  # no stop_reason, no message_stop


def _sse_without_message_stop():
    return CHEAP_REPLY[:CHEAP_REPLY.rindex(b"event: message_stop")]


JSON_OK = json.dumps({"id": "m", "stop_reason": "end_turn", "content": [{"type": "text", "text": "x"}],
                      "usage": CHEAP_USAGE}).encode()


@pytest.mark.parametrize("reply,ctype,complete", [
    (CHEAP_REPLY, "text/event-stream", True),
    (_cut_sse(), "text/event-stream", False),
    (_sse_without_message_stop(), "text/event-stream", False),
    (b"", "text/event-stream", False),
    (JSON_OK, "application/json", True),
    (JSON_OK[:-20], "application/json", False),
    (json.dumps({"id": "m", "stop_reason": None, "usage": CHEAP_USAGE}).encode(), "application/json", False),
    (b"", "application/json", False),
])
async def test_an_incomplete_cheap_reply_is_not_replayed_or_paired(on, reply, ctype, complete):
    _usage()
    replay = FakeReplay()
    fs = _shadow()
    fs.maybe_sample(RAW, HAIKU, OPUS, sf.DOOR_PROXY, "msg_1", replay=replay, cheap_status=200,
                    cheap_reply=reply, cheap_ctype=ctype, cheap_usage=CHEAP_USAGE)
    await fs.drain()
    assert sf.reply_complete(reply, ctype) is complete
    if complete:
        assert len(replay.calls) == 1 and _ledger()[-1]["outcome"] == sf.OUT_REPLAYED
    else:
        assert replay.calls == []
        assert _ledger()[-1]["reason"] == sf.R_CHEAP_INCOMPLETE
        assert not sf.pairs_dir().exists()


async def test_full_rate_window_is_exactly_14_days(on):
    _usage()
    replay = FakeReplay()
    await _sample(_shadow(rng=0.999), replay)  # writes started.json at NOW
    edge = NOW + sf.FULL_RATE_DAYS * 86400
    assert sf.FULL_RATE_DAYS == 14
    assert sf.sample_rate(edge - 1) == sf.FULL_RATE
    assert sf.sample_rate(edge) == sf.STEADY_RATE  # day 14 exactly is already steady


def test_two_pair_rows_with_one_task_id_are_judged_once(on):
    _usage()
    _pairs(1)
    _pairs(1, ts0=NOW + 5)  # same task_id "t0", later ts
    run = _judge_ok()
    out = sf.judge_pending(run=run, rng=lambda: 0.1, clock=lambda: NOW)
    assert len(run.calls) == 1 and out["judged"] == 1
    assert len(haiku_guard.read_jsonl(sf.judge_ledger_path())) == 1
