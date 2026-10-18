"""F97: follow-ups from the review of P3.1-4 (#619): legacy queue rows, orphan temp files,
atomic Haiku admission, local-day spend window, unknown tokens, enqueue-time purge."""

from __future__ import annotations

import asyncio
import calendar
import json
import os
import pathlib
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from llm_router import failopen, judge, judge_hygiene, judge_ledger, judge_sampler, judge_select
from llm_router.judge_select import LocalModel

SECRET = "sk-ant-" + "x" * 30
PROSE = "the password was hunter2play"
HAIKU = "cfg-haiku-model"
DAY = 86_400


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "1.0")
    for var in ("LLM_ROUTER_JUDGE_HAIKU_FALLBACK", "LLM_ROUTER_JUDGE_HAIKU_MAX_CALLS",
                "LLM_ROUTER_JUDGE_HAIKU_DAILY_VOLUME", "LLM_ROUTER_JUDGE_QUEUE_MAX_AGE_DAYS",
                "LLM_ROUTER_JUDGE_QUEUE_MAX_ENTRIES"):
        monkeypatch.delenv(var, raising=False)
    judge_sampler._pending_skips.clear()
    failopen.reset_cache()
    return tmp_path


def _enq(i=1, prompt="what is 2+2", response="4"):
    return judge.enqueue_for_grading(
        routing_decision_id=i, prompt=prompt, response=response,
        task_type="query", answering_model="ollama/a", door="mcp",
    )


def _queue(home):
    p = home / "judge_queue.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _all_text(home) -> str:
    return "".join(p.read_text(errors="ignore") for p in home.rglob("*") if p.is_file())


def _fo(code):
    failopen.reset_cache()
    return failopen.snapshot().by_code.get(code)


def _legacy(i, prompt, response, queued_at=None):
    """A row as P3.1-3 and earlier wrote it: raw text, no hygiene marker."""
    return {"door": "mcp", "routing_decision_id": i, "prompt": prompt, "response": response,
            "task_type": "query", "answering_model": "ollama/a",
            "queued_at": time.time() if queued_at is None else queued_at}


def _write_queue(home, entries):
    q = home / "judge_queue.jsonl"
    q.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return q


def _judge_resp(content='{"correctness": 1, "relevance": 1, "completeness": 1}'):
    return SimpleNamespace(content=content, input_tokens=10, output_tokens=2, cost_usd=0.0)


# ── N1: legacy rows are prepared at claim ───────────────────────────────────

def test_legacy_raw_row_with_a_secret_never_reaches_the_judge_and_leaves_disk(home):
    _write_queue(home, [
        _legacy(1, f"deploy with {SECRET} please", "ok"),
        _legacy(2, "what is 2+2", f"your key is {SECRET}"),
        _legacy(3, f"{PROSE}. what is 2+2", "4"),
    ])
    inv = [LocalModel(name="ollama/a"), LocalModel(name="ollama/b", params=9e9)]
    call = AsyncMock(return_value=_judge_resp())
    with patch.object(judge_select, "local_inventory", return_value=inv), \
         patch.object(judge, "_store_judge_score", AsyncMock()), patch.object(judge, "call_llm", call):
        result = asyncio.run(judge.drain_queue())
    sent = json.dumps([c.kwargs["messages"] for c in call.call_args_list])
    assert call.await_count == 1                      # anti-vacuity: the clean-after-redaction row is graded
    assert SECRET not in sent and "hunter2play" not in sent
    assert result["dropped"] == 2 and result["graded"] == 1
    assert SECRET not in _all_text(home) and _queue(home) == []


def test_prepared_rows_are_not_redone_and_unprepared_overflow_is_requeued_prepared(home):
    assert _enq(1)
    assert _queue(home)[0][judge_hygiene.PREPARED_KEY] is True
    _write_queue(home, _queue(home) + [_legacy(2, PROSE, "r"), _legacy(3, "q3", "r3")])
    with patch.object(judge_hygiene, "prepare", wraps=judge_hygiene.prepare) as prep, \
         patch.object(judge, "_grade_one", AsyncMock(return_value="graded")):
        result = asyncio.run(judge.drain_queue(batch_size=1))
    assert prep.call_count == 2                       # the two legacy rows only
    assert result["requeued"] == 2
    left = _queue(home)
    assert all(r[judge_hygiene.PREPARED_KEY] is True for r in left)
    assert "hunter2play" not in json.dumps(left)


def test_sanitize_claimed_drop_is_counted_without_text(home):
    kept, dropped = judge_hygiene.sanitize_claimed([_legacy(1, SECRET, "x"), _legacy(2, "ok", "ok")])
    assert dropped == 1 and [r["routing_decision_id"] for r in kept] == [2]
    assert SECRET not in _all_text(home)
    assert _fo("CHZ-FO-JUDGE-QUEUE-LEGACY-DROPPED") == 1


# ── N7: orphaned temp files ─────────────────────────────────────────────────

def _orphan(home, name, age_s):
    p = home / name
    p.write_text(json.dumps(_legacy(1, "orphan-prompt", "orphan-answer")) + "\n")
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


def test_enqueue_purges_old_orphans_and_keeps_young_ones(home):
    q = home / "judge_queue.jsonl"
    old_rw = _orphan(home, f"{q.name}.rewrite.4242.1", 3_600)
    fresh_rw = _orphan(home, f"{q.name}.rewrite.4242.2", 5)
    ancient_dr = _orphan(home, f"{q.name}.draining.4242.3", 10 * DAY)
    recoverable_dr = _orphan(home, f"{q.name}.draining.4242.4", 3_600)
    other = _orphan(home, "judge_ledger.jsonl", 10 * DAY)   # not a queue temp file
    assert _enq(1)
    assert not old_rw.exists() and not ancient_dr.exists()
    assert fresh_rw.exists() and recoverable_dr.exists() and other.exists()


def test_drain_purges_old_orphans(home):
    q = home / "judge_queue.jsonl"
    old_rw = _orphan(home, f"{q.name}.rewrite.4242.1", 3_600)
    ancient_dr = _orphan(home, f"{q.name}.draining.4242.3", 10 * DAY)
    with patch.object(judge, "_grade_one", AsyncMock(return_value="graded")):
        asyncio.run(judge.drain_queue())
    assert not old_rw.exists() and not ancient_dr.exists()
    assert "orphan-prompt" not in _all_text(home)      # the stranded rows aged out, not graded
    assert _queue(home) == []


def test_purge_orphans_counts_and_records(home):
    q = home / "judge_queue.jsonl"
    _orphan(home, f"{q.name}.rewrite.1.1", 3_600)
    _orphan(home, f"{q.name}.rewrite.1.2", 3_600)
    assert judge_hygiene.purge_orphans(q) == 2
    assert judge_hygiene.purge_orphans(q) == 0
    assert _fo("CHZ-FO-JUDGE-QUEUE-ORPHAN-PURGED") == 1


# ── N2: atomic admission ────────────────────────────────────────────────────

@pytest.fixture
def haiku_world(home, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_HAIKU_FALLBACK", "1")
    monkeypatch.setenv("LLM_ROUTER_JUDGE_MODEL", HAIKU)
    (home / "usage.json").write_text(json.dumps(
        {"session_pct": 10, "weekly_pct": 20, "updated_at": time.time()}))
    return home


def _spent(n, now=None):
    for _ in range(n):
        judge_ledger.record(door="mcp", answering_model="ollama/a", judge_model=HAIKU,
                            judge_kind="subscription", tokens=10, cost_usd=0.0,
                            outcome=judge_ledger.OUT_GRADED, now=now)


def test_concurrent_admission_at_19_of_20_admits_exactly_one(haiku_world):
    _spent(19)
    n = 8
    barrier = threading.Barrier(n)
    out = []

    def go():
        barrier.wait()
        out.append(judge_ledger.reserve_haiku(time.time(), 10))

    threads = [threading.Thread(target=go) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    admitted = [o for o in out if o[0] is None]
    assert len(out) == n and len(admitted) == 1 and admitted[0][1]
    assert sorted(o[0] for o in out if o[0] is not None) == [judge_ledger.OUT_CAP_CALLS] * (n - 1)
    assert not (haiku_world / judge_ledger.LOCK_FILENAME).exists()   # the lock is released


def test_reservation_is_released_after_the_call_and_the_ledger_stays_text_free(haiku_world):
    inv = [LocalModel(name="ollama/only")]
    item = {"routing_decision_id": 1, "prompt": "PROMPT-TEXT", "response": "ANSWER-TEXT",
            "task_type": "query", "answering_model": "ollama/only", "door": "mcp"}
    resp = SimpleNamespace(content='{"correctness": 1, "relevance": 1, "completeness": 1}',
                           input_tokens=100, output_tokens=20, cost_usd=0.002)
    with patch.object(judge_select, "local_inventory", return_value=inv), \
         patch.object(judge, "_store_judge_score", AsyncMock()), \
         patch.object(judge, "call_llm", AsyncMock(return_value=resp)):
        assert asyncio.run(judge._grade_one(item)) == "graded"
    assert judge_ledger._reserved_today(time.time()) == (0, 0)
    assert len(judge_ledger.read_rows()) == 1
    assert "PROMPT-TEXT" not in _all_text(haiku_world)


# ── N4: unknown tokens count as the estimate ────────────────────────────────

def test_unknown_token_use_counts_the_estimate_toward_the_token_cap(haiku_world, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_HAIKU_DAILY_VOLUME", "1500")
    inv = [LocalModel(name="ollama/only")]
    item = {"routing_decision_id": 1, "prompt": "p" * 2000, "response": "r" * 2000,   # estimate: 1000 tokens
            "task_type": "query", "answering_model": "ollama/only", "door": "mcp"}
    no_usage = SimpleNamespace(content='{"correctness": 1, "relevance": 1, "completeness": 1}',
                               input_tokens=None, output_tokens=None, cost_usd=None)
    call = AsyncMock(return_value=no_usage)
    with patch.object(judge_select, "local_inventory", return_value=inv), \
         patch.object(judge, "_store_judge_score", AsyncMock()), patch.object(judge, "call_llm", call):
        assert asyncio.run(judge._grade_one(item)) == "graded"
        assert asyncio.run(judge._grade_one(item)) == "ungraded"
    assert call.await_count == 1
    assert judge_ledger.read_rows()[-1]["outcome"] == judge_ledger.OUT_CAP_TOKENS
    assert judge_ledger.read_rows()[0]["tokens"] is None            # the ledger still says unknown
    assert "haiku tokens 1000/1500" in judge_ledger.status_text()


# ── N3: the day is the local day ────────────────────────────────────────────

@pytest.fixture
def auckland(monkeypatch):
    monkeypatch.setenv("TZ", "Pacific/Auckland")     # UTC+13 in January
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _utc(y, mo, d, h, mi):
    return float(calendar.timegm((y, mo, d, h, mi, 0)))


def test_day_boundary_is_local_midnight_not_utc(home, auckland):
    # Local midnight 15 Jan is 2026-01-14 11:00 UTC.
    _spent(1, now=_utc(2026, 1, 14, 10, 59))     # local 14 Jan 23:59
    now = _utc(2026, 1, 14, 11, 1)               # local 15 Jan 00:01, same UTC day as the row
    assert judge_ledger.usage_today(now)["haiku_calls"] == 0
    _spent(1, now=_utc(2026, 1, 14, 23, 30))     # local 15 Jan 12:30, previous UTC day
    now = _utc(2026, 1, 15, 0, 30)               # local 15 Jan 13:30, next UTC day
    assert judge_ledger.usage_today(now)["haiku_calls"] == 1
    assert "(local day)" in judge_ledger.status_text(now) and "UTC" not in judge_ledger.status_text(now)


# ── N6: no_proxy_env stays in the drain process ─────────────────────────────

def test_drain_and_proxy_env_strip_are_not_referenced_by_the_mcp_server_modules():
    # providers.call_llm takes no base-url override, so the env strip stays; it is safe only
    # while the MCP server never calls the drain. Sources are read from the package directory.
    pkg = pathlib.Path(judge.__file__).parent
    server_files = [pkg / "server.py", pkg / "route_server.py", pkg / "mcp_door.py", pkg / "mcp_roots.py"]
    server_files += sorted(p for p in (pkg / "tools").iterdir() if p.suffix == ".py")
    assert len(server_files) > 10 and all(p.exists() for p in server_files)    # the scan found files
    offenders = [p.name for p in server_files
                 if any(w in p.read_text(encoding="utf-8") for w in ("drain_queue", "no_proxy_env"))]
    assert offenders == []
    assert "DRAIN PROCESS ONLY" in judge_ledger.no_proxy_env.__doc__


# ── N8 and the untested stride path ─────────────────────────────────────────

def test_cap_off_still_purges_expired_rows_at_enqueue(home, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_QUEUE_MAX_ENTRIES", "0")
    _write_queue(home, [_legacy(9, "old", "r", queued_at=time.time() - 9 * DAY)])
    assert _enq(1)
    assert [r["routing_decision_id"] for r in _queue(home)] == [1]     # old row purged, new one kept


def test_stride_crossing_append_runs_the_check_and_enforces_the_cap(home, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_QUEUE_MAX_ENTRIES", "2")
    stride = judge._CHECK_STRIDE_BYTES
    now = time.time()
    target = stride - 100                                  # just under a stride line, over the exact-check size
    rows = [_legacy(i, "p", "r", queued_at=now) for i in (1, 2, 3)]
    base = sum(len((json.dumps({**r, "pad": ""}) + "\n").encode()) for r in rows)
    pad = (target - base) // 3
    rows = [{**r, "pad": "x" * pad} for r in rows]
    q = _write_queue(home, rows)
    assert judge._EXACT_CHECK_BYTES < q.stat().st_size < stride
    with patch.object(judge_hygiene, "purge_expired", wraps=judge_hygiene.purge_expired) as purge:
        assert _enq(4)
    purge.assert_called_once()
    assert [r["routing_decision_id"] for r in _queue(home)] == [3, 4]  # oldest dropped to the cap
