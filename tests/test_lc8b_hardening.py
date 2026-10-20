"""LC-8b: hardening of the LC-8 local-check routing (rv-675 follow-ups).

Settings parity instead of ctx hash equality, a verified digest for routing, no prereg fallback for a
local_check-enabled lane, per-attempt local outcomes for demotion, locked enable/disable.
Tmp homes only; fake digests and Ollama settings; no network, no model. Model names are fixtures.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading

import pytest

from llm_router import eligibility_routing as er
from llm_router import skip_causes, text_job_ledger as L
from llm_router.local_check import routing as lr
from llm_router.local_check import runtimes as rt
from llm_router.resolver import eligibility_store as store
from tests.test_lc8_routing import (  # constants shared with LC-8
    BARE, CHAIN, DIGEST, GEMINI, JOB, LIMIT, LOCAL, NOW, PASS70, PROV, SHA,
)

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_IGNORE_ELIGIBILITY", raising=False)
    er.reset_cache()
    yield tmp_path
    er.reset_cache()


MEASURED = 8192
PARITY = {**PROV, "ctx": MEASURED, "think": False}


def lane(home, prov=None):
    path = home / "local_eligibility.json"
    store.record_local_check(BARE, {JOB: PASS70}, prov or PARITY, path=path)
    return path


def pick(ctx=MEASURED, think=False, input_len=1000, digests=None, models=CHAIN, now=NOW):
    return er.prefer_passed(models, JOB, input_len, now, digests={BARE: DIGEST} if digests is None else digests,
                            ctx_for=lambda m: ctx, think=think)


def causes():
    return [(e["reason"], e.get("detail")) for e in (skip_causes.entries_so_far() or [])]


@pytest.fixture
def ready(home):
    lane(home)
    lr.enable(BARE, JOB, "local_check")
    skip_causes.begin()
    return home


# ------------------------------------------------------------------ 1. settings parity
def test_store_records_measured_ctx_and_think(home):
    path = lane(home)
    rec = json.loads(path.read_text())[BARE]["text_job_local"][JOB]
    assert rec["ctx"] == MEASURED and rec["think"] is False


def test_store_drops_ctx_and_think_of_the_wrong_type(home):
    path = home / "local_eligibility.json"
    store.record_local_check(BARE, {JOB: PASS70}, {**PROV, "ctx": True, "think": "no"}, path=path)
    rec = json.loads(path.read_text())[BARE]["text_job_local"][JOB]
    assert "ctx" not in rec and "think" not in rec
    store.record_local_check(BARE, {JOB: PASS70}, {**PROV, "ctx": 0, "think": None}, path=path)
    rec = json.loads(path.read_text())[BARE]["text_job_local"][JOB]
    assert "ctx" not in rec and "think" not in rec


def test_prefers_when_router_ctx_equals_or_exceeds_measured(ready):
    assert pick(ctx=MEASURED)[0] == LOCAL
    assert pick(ctx=32768)[0] == LOCAL          # the old hash rule refused this silently


def test_not_preferred_when_router_ctx_below_measured_and_says_why(ready):
    assert pick(ctx=4096) == CHAIN
    assert ("not_preferred", "ctx_below_measured") in causes()
    e = [x for x in skip_causes.entries_so_far() if x["reason"] == "not_preferred"][0]
    assert e["model"] == LOCAL and e["lane"] == JOB and e["ctx"] == 4096 and e["measured"] == MEASURED


def test_not_preferred_when_thinking_on_and_says_why(ready):
    assert pick(think=True) == CHAIN and ("not_preferred", "think_on") in causes()


def test_not_preferred_when_router_ctx_unknown_and_says_why(ready):
    assert pick(ctx=None) == CHAIN and ("not_preferred", "ctx_unknown") in causes()


def test_not_preferred_when_lane_was_measured_with_thinking_on(home):
    lane(home, {**PARITY, "think": True})
    lr.enable(BARE, JOB, "local_check")
    skip_causes.begin()
    assert pick() == CHAIN and ("not_preferred", "lane_think") in causes()


def test_input_limit_is_min_of_scope_and_router_window(ready):
    window = rt.max_prompt_chars(2048)
    assert window < LIMIT
    # router window larger than the scope: the scope binds
    assert pick(ctx=32768, input_len=LIMIT)[0] == LOCAL and pick(ctx=32768, input_len=LIMIT + 1) == CHAIN


def test_input_limit_binds_on_router_window_when_scope_is_larger(home):
    big = {**PARITY, "ctx": 2048, "scope": {"max_prompt_chars": 10 ** 6}}
    lane(home, big)
    lr.enable(BARE, JOB, "local_check")
    window = rt.max_prompt_chars(2048)
    assert pick(ctx=2048, input_len=window)[0] == LOCAL and pick(ctx=2048, input_len=window + 1) == CHAIN


def test_temperature_is_not_part_of_the_claim(ready):
    """The router calls with 0.2, the check measured at 0: parity must not depend on it."""
    assert rt.TEMPERATURE != 0.2
    assert pick()[0] == LOCAL


def test_old_lane_without_ctx_keeps_the_hash_rule(home):
    lane(home, PROV)                                    # no ctx / think recorded
    lr.enable(BARE, JOB, "local_check")
    skip_causes.begin()
    assert pick(ctx=MEASURED)[0] == LOCAL               # PROV's hash is for ctx 8192
    assert pick(ctx=32768) == CHAIN and ("not_preferred", "settings_mismatch") in causes()


def test_doctor_line_for_a_passed_lane_withheld_by_settings(ready):
    lines = er.settings_notices(digests={BARE: DIGEST}, ctx_for=lambda m: 4096, think=False, now=NOW)
    assert len(lines) == 1 and BARE in lines[0] and "ctx_below_measured" in lines[0] and "4096" in lines[0]
    assert er.settings_notices(digests={BARE: DIGEST}, ctx_for=lambda m: MEASURED, think=False, now=NOW) == []


def test_doctor_lines_include_settings_notice_through_routing_module(ready, monkeypatch):
    monkeypatch.setattr(er, "_route_time_ctx", lambda m: 4096)
    monkeypatch.setattr(er, "_route_time_think", lambda: False)
    monkeypatch.setattr("llm_router.local_check.display.inventory", lambda: ([BARE], {BARE: DIGEST}))
    monkeypatch.setattr("time.time", lambda: NOW)
    assert any("ctx_below_measured" in ln for ln in lr.doctor_lines())


def test_session_passes_the_ctx_it_ran_with(monkeypatch):
    import inspect
    from llm_router.local_check import session
    src = inspect.getsource(session.run_check)
    assert '"ctx": ctx0' in src and "c.adapter.ctx(c.model)" in src


# ------------------------------------------------------------------ 2. verified digest
def test_missing_or_stale_digest_means_no_preference(ready):
    assert pick(digests={}) == CHAIN                                # no cache / no digest for the model
    assert ("not_preferred", "digest_unverified") in causes()
    assert pick(digests={BARE: "sha256:" + "d" * 16}) == CHAIN      # changed
    assert pick(digests={BARE: DIGEST})[0] == LOCAL


def test_lane_without_a_stored_digest_is_never_routable(home):
    lane(home, {k: v for k, v in PARITY.items() if k != "model_digest"})
    lr.enable(BARE, JOB, "local_check")
    assert pick() == CHAIN


def test_stale_discovery_cache_gives_no_digest(home, monkeypatch):
    from llm_router.local_check import display
    monkeypatch.setattr(display, "_discovery", lambda: ({"ollama/lc8-fx:9b": {"digest": "a" * 16}},
                                                        display.DIGEST_MAX_AGE_S + 1))
    assert display.inventory()[1] == {}


# ------------------------------------------------------------------ 3. no prereg fallback for a local_check entry
def prereg_lane(home):
    path = home / "local_eligibility.json"
    p = {"grade": "passed", "verdict": "eligible", "eligible": True, "k": 60, "n": 70, "source": "prereg",
         "prereg": "PREREG-Q37", "manifest_sha256": SHA}
    store.record_competence(BARE, "text_job", {JOB: p}, path=path)


STALE = {BARE: "sha256:" + "e" * 16}


def test_local_check_entry_does_not_fall_back_to_prereg_when_its_lane_goes_stale(home):
    lane(home)
    prereg_lane(home)
    lr.enable(BARE, JOB, "local_check")
    skip_causes.begin()
    assert pick(digests=STALE) == CHAIN
    assert ("not_preferred", "local_lane_stale") in causes()


@pytest.mark.parametrize("source", ["prereg", "manual"])
def test_prereg_and_manual_entries_may_use_the_prereg_lane(home, source):
    lane(home)
    prereg_lane(home)
    lr.enable(BARE, JOB, source)
    assert pick(digests=STALE)[0] == LOCAL


def test_prereg_lane_still_works_with_no_local_lane(home):
    prereg_lane(home)
    lr.enable(BARE, JOB, "prereg")
    assert pick()[0] == LOCAL


# ------------------------------------------------------------------ 4. demotion on per-attempt local outcomes
def read_attempts(home):
    return [json.loads(x) for x in (home / L.LOCAL_ATTEMPTS_FILENAME).read_text().splitlines()]


def test_a_rescued_local_error_is_recorded_per_attempt(home):
    touched = L.record_local_attempts(job=JOB, served_model=GEMINI, chain_attempts=[LOCAL, GEMINI], accepted=True)
    assert touched == [LOCAL]
    rows = read_attempts(home)
    assert [(r["model"], r["outcome"]) for r in rows] == [(LOCAL, "error")] and "prompt" not in rows[0]


def test_local_rejection_then_acceptance_records_both_attempts(home):
    L.record_local_attempts(job=JOB, served_model=LOCAL, chain_attempts=[LOCAL], accepted=False)
    L.record_local_attempts(job=JOB, served_model=LOCAL, chain_attempts=[LOCAL], accepted=True)
    assert [r["outcome"] for r in read_attempts(home)] == ["rejected", "ok"]


def test_nothing_recorded_for_a_non_local_call_and_markers_are_ignored(home):
    from llm_router.quality_feedback import format_skip_marker
    assert L.record_local_attempts(job=JOB, served_model=GEMINI, chain_attempts=[GEMINI], accepted=True) == []
    assert L.record_local_attempts(job=JOB, served_model=GEMINI,
                                   chain_attempts=[format_skip_marker(LOCAL, 0.1, 5), GEMINI], accepted=True) == []
    assert not (home / L.LOCAL_ATTEMPTS_FILENAME).exists()


def test_dead_ollama_demotes_the_lane(home):
    """20 calls: the local model errors every time and Gemini rescues. The final-row ledger sees only Gemini."""
    lr.enable(BARE, JOB, "local_check")
    for _ in range(20):
        L.record_local_attempts(job=JOB, served_model=GEMINI, chain_attempts=[LOCAL, GEMINI], accepted=True)
    assert lr.demotion_check(LOCAL, JOB) is True
    assert lr.entry(BARE, JOB)["reason"] == lr.DEMOTE_REASON and not lr.is_enabled(BARE, JOB)


def test_rescued_errors_follow_the_7_of_20_rule(home):
    lr.enable(BARE, JOB, "local_check")
    for _ in range(14):
        L.record_local_attempts(job=JOB, served_model=LOCAL, chain_attempts=[LOCAL], accepted=True)
    for _ in range(6):
        L.record_local_attempts(job=JOB, served_model=GEMINI, chain_attempts=[LOCAL, GEMINI], accepted=True)
    assert lr.demotion_check(LOCAL, JOB) is False                       # 6 of 20
    L.record_local_attempts(job=JOB, served_model=GEMINI, chain_attempts=[LOCAL, GEMINI], accepted=True)
    assert lr.demotion_check(LOCAL, JOB) is True                        # 7 of the last 20


def test_llm_text_job_with_a_dead_ollama_demotes_end_to_end(home, monkeypatch):
    from llm_router.tools import text as T
    from llm_router.types import LLMResponse
    lr.enable(BARE, JOB, "local_check")

    async def fake_route(*a, **k):                       # the router tried ollama, it failed, gemini answered
        return LLMResponse(content="feat: touch src/a.py", model=GEMINI, input_tokens=1, output_tokens=1,
                           cost_usd=0.0, latency_ms=1.0, provider="gemini", chain_attempts=[LOCAL, GEMINI])

    async def noop(*a, **k):
        return None

    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", "1")
    monkeypatch.setattr(T, "route_and_call", fake_route)
    monkeypatch.setattr(T, "_announce_routing", noop)
    diff = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a\n+b\n"
    for _ in range(20):
        asyncio.run(T.llm_text_job(JOB, diff, object(), hint="ab"))
    assert not lr.is_enabled(BARE, JOB) and lr.entry(BARE, JOB)["reason"] == lr.DEMOTE_REASON


# ------------------------------------------------------------------ 5. locked read-modify-write, unique temp names
def test_concurrent_enables_lose_nothing(home):
    models = [f"lc8b-m{i}:1b" for i in range(24)]
    errs = []

    def go(m):
        try:
            lr.enable(m, JOB, "local_check")
            lr.on_pass(m, ["pr_description"])
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    ts = [threading.Thread(target=go, args=(m,)) for m in models]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    data = json.loads((home / "local_routing.json").read_text())
    assert sorted(data) == sorted(models)
    assert all(set(v) == {JOB, "pr_description"} for v in data.values())
    assert [p.name for p in home.iterdir() if p.name.endswith(".tmp")] == []


def test_mutations_take_the_lock_and_refuse_to_write_without_it(home, monkeypatch):
    import contextlib
    taken = []

    @contextlib.contextmanager
    def fake(path, *a, **k):
        taken.append(path.name)
        yield False

    monkeypatch.setattr(lr, "exclusive_lock", fake)
    for call in (lambda: lr.enable(BARE, JOB, "manual"), lambda: lr.disable(BARE, JOB),
                 lambda: lr.on_pass(BARE, [JOB])):
        with pytest.raises(OSError):
            call()
    assert taken == ["local_routing.json.lock"] * 3 and not (home / "local_routing.json").exists()


def test_temp_names_are_unique_per_call(home, monkeypatch):
    seen = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda src, dst: (seen.append(os.path.basename(src)), real(src, dst))[1])
    lr.enable("a:1", JOB, "manual")
    lr.enable("b:1", JOB, "manual")
    assert len(seen) == 2 and len(set(seen)) == 2


def test_on_pass_is_one_write(home, monkeypatch):
    writes = []
    real = lr._write
    monkeypatch.setattr(lr, "_write", lambda d, p=None: (writes.append(1), real(d, p))[1])
    lr.on_pass(BARE, ["commit_message", "pr_description", "summarize_output"])
    assert len(writes) == 1
    lr.on_pass(BARE, ["commit_message"])
    assert len(writes) == 1                     # nothing changed: nothing rewritten


def test_untagged_name_is_stored_under_the_tagged_inventory_key(home):
    lr.enable("lc8b-bare", JOB, "manual")
    assert list(json.loads((home / "local_routing.json").read_text())) == ["lc8b-bare:latest"]
    from llm_router.local_check import display
    assert display.routing_state() == {("lc8b-bare:latest", JOB): True}
