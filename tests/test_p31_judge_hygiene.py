"""P3.1-4: judge queue retention, spend ledger and caps, F93 local/cloud split."""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from unittest.mock import AsyncMock, patch

import pytest

from llm_router import failopen, judge, judge_hygiene, judge_ledger, judge_select, judge_sampler
from llm_router.judge_select import LocalModel, select_judge
from llm_router.types import LLMResponse

SECRET = "sk-ant-" + "x" * 30
PROSE = "the password was hunter2play"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "1.0")
    for var in ("LLM_ROUTER_JUDGE_HAIKU_FALLBACK", "LLM_ROUTER_JUDGE_HAIKU_MAX_CALLS",
                "LLM_ROUTER_JUDGE_HAIKU_DAILY_VOLUME", "LLM_ROUTER_JUDGE_QUEUE_MAX_AGE_DAYS"):
        monkeypatch.delenv(var, raising=False)
    judge_sampler._pending_skips.clear()
    failopen.reset_cache()
    return tmp_path


def _enq(i=1, prompt="what is 2+2", response="4", door="mcp", model="m"):
    return judge.enqueue_for_grading(
        routing_decision_id=i, prompt=prompt, response=response,
        task_type="query", answering_model=model, door=door,
    )


def _queue(home):
    p = home / "judge_queue.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _all_text(home) -> str:
    return "".join(p.read_text(errors="ignore") for p in home.rglob("*") if p.is_file())


# ── retention ───────────────────────────────────────────────────────────────

def test_planted_secret_entry_is_skipped_whole(home):
    assert _enq(1, prompt=f"deploy with {SECRET} please") is False
    assert _enq(2, response=f"your key is {SECRET}") is False
    assert _queue(home) == []
    assert _enq(3) is True  # anti-vacuity: a clean entry still lands
    assert len(_queue(home)) == 1
    assert SECRET not in _all_text(home)


def test_text_is_scrubbed_redacted_and_capped(home):
    banner = "answer\n⚡ ROUTE: query/simple → try llm [via heuristic]\nmore"
    assert _enq(1, prompt=f"{PROSE}. {banner}", response="z" * 20_000)
    (e,) = _queue(home)
    assert "ROUTE:" not in e["prompt"] and "hunter2play" not in e["prompt"]
    assert len(e["response"]) <= judge_hygiene.TEXT_CAP and len(e["prompt"]) <= judge_hygiene.TEXT_CAP


def test_queue_file_mode_is_0600_and_stays_so(home):
    _enq(1)
    q = home / "judge_queue.jsonl"
    assert (q.stat().st_mode & 0o777) == 0o600
    os.chmod(q, 0o644)  # an older, laxer file is tightened on the next append
    _enq(2)
    assert (q.stat().st_mode & 0o777) == 0o600
    judge._requeue(q, [{"id": "x", "queued_at": time.time()}])
    assert (q.stat().st_mode & 0o777) == 0o600
    judge_hygiene.rewrite_private(q, ["{}"])
    assert (q.stat().st_mode & 0o777) == 0o600


def _write_queue(home, entries):
    q = home / "judge_queue.jsonl"
    q.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return q


def test_ttl_purge_removes_old_unclaimed_entries(home):
    now = time.time()
    day = 86_400
    q = _write_queue(home, [
        {"routing_decision_id": 1, "queued_at": now - 5 * day},
        {"routing_decision_id": 2, "queued_at": now - 1 * day},
        {"routing_decision_id": 3},  # no age: cannot be shown to be fresh
    ])
    assert judge_hygiene.purge_expired(q, now) == 2
    assert [e["routing_decision_id"] for e in _queue(home)] == [2]
    assert (q.stat().st_mode & 0o777) == 0o600


def test_ttl_follows_a_shorter_persist_ttl(home):
    now = time.time()
    q = _write_queue(home, [{"routing_decision_id": 1, "queued_at": now - 2 * 86_400}])
    assert judge_hygiene.max_age_seconds() == 3 * 86_400
    with patch("llm_router.config_lite.config_value", return_value=1):
        assert judge_hygiene.max_age_seconds() == 86_400
        assert judge_hygiene.purge_expired(q, now) == 1
    assert _queue(home) == []


def test_enqueue_and_drain_purge_old_entries(home):
    old = {"routing_decision_id": 9, "queued_at": time.time() - 9 * 86_400, "prompt": "old", "response": "r"}
    _write_queue(home, [old])
    assert _enq(1)
    assert [e["routing_decision_id"] for e in _queue(home)] == [1]  # purged at enqueue
    _write_queue(home, [old, {**old, "routing_decision_id": 2, "queued_at": time.time()}])
    grade = AsyncMock(return_value="graded")
    with patch.object(judge, "_grade_one", grade):
        result = asyncio.run(judge.drain_queue())
    assert [c.args[0]["routing_decision_id"] for c in grade.call_args_list] == [2]
    assert result["graded"] == 1


def test_entries_are_deleted_at_claim(home):
    _enq(1)
    _enq(2)
    with patch.object(judge, "_grade_one", AsyncMock(return_value="graded")):
        asyncio.run(judge.drain_queue())
    assert _queue(home) == []
    assert not [p for p in home.iterdir() if ".draining." in p.name]


def test_large_queue_is_not_reread_per_enqueue(home):
    q = home / "judge_queue.jsonl"
    stride = judge._CHECK_STRIDE_BYTES
    pad = "p" * (4 * stride + 8000)
    q.write_text(json.dumps({"queued_at": time.time(), "pad": pad}) + "\n")
    with patch.object(judge_hygiene, "purge_expired") as purge:
        for i in range(5):
            assert _enq(i + 1)
    purge.assert_not_called()


@pytest.mark.timing
def test_enqueue_p95_under_5ms_on_a_2000_entry_queue(home):
    now = time.time()
    body = "x" * 800
    q = home / "judge_queue.jsonl"
    q.write_text("".join(
        json.dumps({"routing_decision_id": i, "queued_at": now, "prompt": body, "response": body,
                    "task_type": "query", "answering_model": "m", "door": "mcp"}) + "\n"
        for i in range(2000)))
    times = []
    for i in range(150):
        t = time.perf_counter()
        _enq(10_000 + i)
        times.append((time.perf_counter() - t) * 1000)
    p95 = statistics.quantiles(times, n=20)[18]
    assert p95 < 5.0, f"enqueue p95 {p95:.2f} ms on a 2000-entry queue"


# ── spend ───────────────────────────────────────────────────────────────────

HAIKU = "cfg-haiku-model"


def _quota(home, session=10, weekly=20, age=0):
    (home / "usage.json").write_text(json.dumps(
        {"session_pct": session, "weekly_pct": weekly, "updated_at": time.time() - age}))


def _resp(content='{"correctness": 1, "relevance": 1, "completeness": 1}', cost=0.002, tin=100, tout=20):
    return LLMResponse(content=content, model="m", input_tokens=tin, output_tokens=tout,
                       cost_usd=cost, latency_ms=1.0, provider="p")


@pytest.fixture
def haiku_world(home, monkeypatch):
    """A local answer with no second local model; Haiku configured, flag on, quota fresh."""
    monkeypatch.setenv("LLM_ROUTER_JUDGE_HAIKU_FALLBACK", "1")
    monkeypatch.setenv("LLM_ROUTER_JUDGE_MODEL", HAIKU)
    _quota(home)
    inv = [LocalModel(name="ollama/only")]
    with patch.object(judge_select, "local_inventory", return_value=inv), \
         patch.object(judge, "_store_judge_score", AsyncMock()):
        yield home


def _item(i=1, **kw):
    return {"routing_decision_id": i, "prompt": "PROMPT-TEXT", "response": "ANSWER-TEXT",
            "task_type": "query", "answering_model": "ollama/only", "door": "mcp", **kw}


def _grade(item):
    return asyncio.run(judge._grade_one(item))


def _ledger():
    return judge_ledger.read_rows()


def test_the_21st_haiku_call_in_a_day_is_stopped(haiku_world):
    call = AsyncMock(return_value=_resp())
    with patch.object(judge, "call_llm", call):
        results = [_grade(_item(i)) for i in range(1, 22)]
    assert call.await_count == 20
    assert results[:20] == ["graded"] * 20 and results[20] == "ungraded"
    assert _ledger()[-1]["outcome"] == "cap_calls" and _ledger()[-1]["judge_kind"] == "subscription"
    assert judge_ledger.usage_today()["haiku_calls"] == 20


def test_token_cap_stops_before_the_call(haiku_world, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_HAIKU_DAILY_VOLUME", "123")
    call = AsyncMock(return_value=_resp())
    with patch.object(judge, "call_llm", call):
        assert _grade(_item(1)) == "graded"      # 120 tokens used
        assert _grade(_item(2)) == "ungraded"    # 120 used + 5 estimated passes 123
    assert call.await_count == 1 and _ledger()[-1]["outcome"] == "cap_tokens"


@pytest.mark.parametrize("kw", [
    {"age": 7200},                     # stale reading
    {"session": 80}, {"weekly": 85},   # paused at the thresholds
])
def test_stale_or_paused_quota_makes_no_call(haiku_world, kw):
    _quota(haiku_world, **kw)
    call = AsyncMock(return_value=_resp())
    with patch.object(judge, "call_llm", call):
        assert _grade(_item()) == "ungraded"
    call.assert_not_awaited()
    assert _ledger()[-1]["outcome"] == "quota_skip"


def test_unknown_quota_makes_no_call(haiku_world):
    (haiku_world / "usage.json").unlink()
    call = AsyncMock(return_value=_resp())
    with patch.object(judge, "call_llm", call):
        assert _grade(_item()) == "ungraded"
    call.assert_not_awaited()
    assert _ledger()[-1]["outcome"] == "quota_skip"


def test_privacy_gate_fails_closed_without_the_flag(haiku_world, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_JUDGE_HAIKU_FALLBACK")
    sel = judge_select.Selection(HAIKU, judge_select.KIND_SUBSCRIPTION, judge_select.STATUS_SELECTED)
    call = AsyncMock(return_value=_resp())
    with patch.object(judge, "call_llm", call), patch.object(judge_select, "select_for", return_value=sel), \
         patch.object(judge_select, "kind_of", return_value=judge_select.KIND_SUBSCRIPTION):
        assert _grade(_item()) == "ungraded"
    call.assert_not_awaited()
    assert _ledger()[-1]["outcome"] == "privacy_off"
    assert judge_select.haiku_fallback_enabled() is False


def test_judge_call_strips_the_proxy_base_url_and_restores_it(haiku_world, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:1234")
    seen = {}

    async def fake(**kw):
        seen["url"] = os.environ.get("ANTHROPIC_BASE_URL")
        return _resp()

    with patch.object(judge, "call_llm", fake):
        assert _grade(_item()) == "graded"
    assert seen["url"] is None and os.environ["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:1234"


def test_ledger_rows_hold_no_text_and_unknowns_stay_none(haiku_world):
    with patch.object(judge, "call_llm", AsyncMock(return_value=_resp(cost=0.002))):
        _grade(_item(1))
    with patch.object(judge, "call_llm", AsyncMock(side_effect=RuntimeError("PROMPT-TEXT boom"))):
        assert _grade(_item(2)) == "failed"
    ok, bad = _ledger()
    keys = {"ts", "door", "answering_model", "judge_model", "judge_kind", "tokens", "cost_usd", "outcome"}
    assert set(ok) == set(bad) == keys
    assert (ok["tokens"], ok["outcome"], ok["judge_model"]) == (120, "graded", HAIKU)
    assert bad["tokens"] is None and bad["cost_usd"] is None and bad["outcome"] == "failed"
    text = _all_text(haiku_world)
    for name in ("judge_ledger.jsonl", "judge_results.jsonl"):
        assert "PROMPT-TEXT" not in (haiku_world / name).read_text()
        assert "ANSWER-TEXT" not in (haiku_world / name).read_text()
    assert "ANSWER-TEXT" not in text


def test_measured_local_call_is_zero_cost_and_unmeasured_is_none(home):
    inv = [LocalModel(name="ollama/a"), LocalModel(name="ollama/b", params=9e9)]
    item = _item(answering_model="ollama/a")
    with patch.object(judge_select, "local_inventory", return_value=inv), \
         patch.object(judge, "_store_judge_score", AsyncMock()), \
         patch.object(judge, "call_llm", AsyncMock(return_value=_resp(cost=0.0))):
        assert _grade(item) == "graded"
    row = _ledger()[-1]
    assert (row["judge_kind"], row["cost_usd"], row["judge_model"]) == ("local", 0.0, "ollama/b")
    assert judge_ledger.usage_today() == {"haiku_calls": 0, "haiku_tokens": 0, "local_calls": 1}


def test_status_text_shows_calls_tokens_and_caps(haiku_world):
    with patch.object(judge, "call_llm", AsyncMock(return_value=_resp())):
        _grade(_item())
    text = judge_ledger.status_text()
    assert "haiku calls 1/20" in text and "haiku tokens 120/400000" in text and "fallback on" in text


def test_ledger_is_capped(home, monkeypatch):
    monkeypatch.setattr(judge_ledger, "_TRIM_BYTES", 2000)
    monkeypatch.setattr(judge_ledger, "MAX_ROWS", 10)
    for i in range(40):
        judge_ledger.record(door="mcp", answering_model="m", judge_model="j", judge_kind="local",
                            tokens=i, cost_usd=0.0, outcome="graded")
    rows = judge_ledger.read_rows()
    assert 10 <= len(rows) < 40 and rows[-1]["tokens"] == 39
    assert ((home / "judge_ledger.jsonl").stat().st_mode & 0o777) == 0o600


# ── failopen carries no prompt text ─────────────────────────────────────────

def test_no_prompt_text_in_any_failopen_record(home):
    marker = "PRIVATE-PROMPT-MARKER"
    _enq(1, prompt=f"{marker} {SECRET}")                       # secret skip
    with patch("llm_router.persist_redaction.persist_redact", side_effect=ValueError(marker)):
        _enq(2, prompt=marker)                                 # redaction failure
    with patch.object(judge_hygiene, "append_private", side_effect=OSError(marker)):
        _enq(3, prompt=marker)                                 # write failure
    store = failopen.store_path()
    raw = store.read_text()
    codes = {json.loads(x)["c"] for x in raw.splitlines()}
    assert {"CHZ-FO-JUDGE-QUEUE-SECRET-SKIP", "CHZ-FO-JUDGE-QUEUE-REDACT", "CHZ-FO-JUDGE-QUEUE-WRITE"} <= codes
    assert marker not in raw and SECRET not in raw


# ── F93: a cloud answerer sharing a local model's name is not local ─────────

def test_cloud_answerer_named_like_an_installed_local_model_never_reaches_haiku():
    inv = [LocalModel(name="ollama/foo")]
    kw = dict(haiku_model="cfg-haiku", haiku_enabled=True)
    assert select_judge("openai/foo", inv, **kw).status == judge_select.STATUS_NO_JUDGE
    assert select_judge("anthropic/foo:latest", inv, **kw).status == judge_select.STATUS_NO_JUDGE
    # the same name as a genuine local answer still gets the fallback
    assert select_judge("ollama/foo", inv, **kw).judge_kind == judge_select.KIND_SUBSCRIPTION
    # and a cloud answer is still judged by another local model, never by the namesake
    two = inv + [LocalModel(name="ollama/bar")]
    assert select_judge("openai/foo", two, **kw).judge_model == "ollama/bar"
