"""W10: the hook-side routing verdict (``llm_router.door_verdict``).

Every test that makes a call talks to a real HTTP server that speaks the Ollama wire format
(``GET /api/ps``, ``POST /v1/systemone``), started on 127.0.0.1 for the test. Nothing here reaches the owner's
Ollama, and nothing stubs ``local_classifier``: the pinned request and the strict parse run for real.

Defects pinned (docs/bugs/CLS-HOOK-1.md, CLS-COLD-1.md, CLS-COOLDOWN-1.md, CLS-TIE-1.md):

* a cold model is WAITED for, not reported as ``cold``;
* one slow call does not stop the next one (the proxy path's 30 s cooldown did);
* an exact tie at the top is a verdict, not a ``parse_error``;
* ``off`` makes zero calls; ``shadow`` never changes a decision; ``on`` acts only on a confident opus verdict;
* a row holds no prompt text.
"""

from __future__ import annotations

import json
import time

import pytest

from llm_router import decision_classifier as dc
from llm_router import door_verdict as dv
from llm_router import paths
from tests._fake_ollama import FakeOllama


@pytest.fixture
def ollama(monkeypatch):
    fake = FakeOllama()
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", fake.url)
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_BACKEND", "systemone")
    monkeypatch.setenv("LLM_ROUTER_DECISION_MODEL", "nimble:9b")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    monkeypatch.delenv("LLM_ROUTER_DOOR_VERDICT_BUDGET_MS", raising=False)
    monkeypatch.delenv("LLM_ROUTER_DOOR_OPUS_MIN_CONF", raising=False)
    yield fake
    fake.close()


def _rows() -> list[dict]:
    return dv.read_rows()


# ---- the model call --------------------------------------------------------------------------------------------


@pytest.mark.timing
def test_a_cold_model_is_waited_for_and_answers(ollama):
    """CLS-COLD-1: the proxy path returned ``cold`` (no verdict) for a model that was not resident."""
    ollama.first_call_delay = 0.8
    t0 = time.monotonic()
    v = dv.classify("fix the typo in README.md", "prompt")
    assert v.ok and v.tier == "sonnet" and v.source == "llm"
    assert v.resident is False                      # it WAS cold when the call began
    assert time.monotonic() - t0 >= 0.8             # and the call waited through the load
    again = dv.classify("fix the typo in README.md again", "prompt")
    assert again.ok and again.resident is True      # now warm


def test_the_hook_budget_is_longer_than_the_proxys_two_seconds(ollama, monkeypatch):
    assert dv.budget_s() == 8.0
    monkeypatch.setenv("LLM_ROUTER_DOOR_VERDICT_BUDGET_MS", "250")
    assert dv.budget_s() == 0.25
    monkeypatch.setenv("LLM_ROUTER_DOOR_VERDICT_BUDGET_MS", "junk")
    assert dv.budget_s() == 8.0


def test_off_makes_zero_calls(ollama, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "off")
    assert dv.start("anything at all", "prompt") is None
    assert dv.classify("anything at all", "prompt").source == "off"
    assert ollama.calls == 0
    monkeypatch.delenv("LLM_ROUTER_LOCAL_CLASSIFIER")
    assert dv.mode() == "off"


def test_no_model_configured_is_off_not_a_failed_request(ollama, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_DECISION_MODEL")
    assert dv.classify("a prompt", "prompt").source == "off"
    assert ollama.calls == 0


def test_a_server_error_is_a_failure_not_a_verdict(ollama):
    ollama.fail = True
    v = dv.classify("a prompt", "prompt")
    assert not v.ok and v.source == "timeout"


# ---- the breaker: consecutive failures only --------------------------------------------------------------------


def test_one_slow_call_does_not_stop_the_next(ollama):
    """CLS-COOLDOWN-1: 129 of 170 7-day ``timeout`` rows were the proxy's 30 s cooldown, not the model."""
    ollama.fail = True
    assert not dv.classify("first", "prompt").ok
    assert not dv.breaker_open()
    ollama.fail = False
    assert dv.classify("second", "prompt").ok           # tried, answered


def test_two_failures_in_a_row_open_the_breaker_and_a_verdict_closes_it(ollama):
    ollama.fail = True
    dv.classify("one", "prompt")
    dv.classify("two", "prompt")
    assert dv.breaker_open()
    calls = ollama.calls
    assert dv.classify("three", "prompt").source == "breaker"
    assert ollama.calls == calls                        # no request while open
    dv.note_result(True)
    assert not dv.breaker_open()


def test_failures_far_apart_do_not_accumulate():
    dv.note_result(False, now=1000.0)
    dv.note_result(False, now=1000.0 + dv.BREAKER_WINDOW_S + 5)
    assert not dv.breaker_open(now=1000.0 + dv.BREAKER_WINDOW_S + 6)


# ---- the tie (CLS-TIE-1) ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("probs,tier", [
    ({"haiku": 0.4825230729172278, "sonnet": 0.4825230729172278, "opus": 0.03495385416554438}, "sonnet"),
    ({"haiku": 0.05, "sonnet": 0.475, "opus": 0.475}, "opus"),
    ({"haiku": 0.3333, "sonnet": 0.3333, "opus": 0.3333}, "opus"),
])
def test_an_exact_tie_at_the_top_is_a_verdict_resolved_to_the_higher_tier(probs, tier):
    top = max(probs, key=probs.get)
    body = {"answers": {"tier": {"type": "choice", "choice": top, "probabilities": probs}}}
    v = dc.parse_answer(body, model="nimble:9b")
    assert v.source == "llm" and v.tier == tier and v.confidence == max(probs.values())


def test_a_choice_that_is_not_an_argmax_is_still_refused():
    probs = {"haiku": 0.48, "sonnet": 0.48, "opus": 0.04}
    body = {"answers": {"tier": {"type": "choice", "choice": "opus", "probabilities": probs}}}
    assert dc.parse_answer(body, model="m").source == "parse_error"


# ---- the door ---------------------------------------------------------------------------------------------------


def _v(tier, conf, kind="prompt", source="llm"):
    return dv.DoorVerdict(kind, source, tier, conf)


@pytest.mark.parametrize("v,kind,door,reason", [
    (_v("opus", 0.7), "prompt", "claude", "opus_confident"),
    (_v("opus", 0.6), "agent", "claude", "opus_confident"),
    (_v("opus", 0.59), "prompt", "mcp", "opus_unsure"),
    (_v("opus", 0.5), "agent", "codex", "opus_unsure"),
    (_v("sonnet", 0.9), "prompt", "mcp", "tier_sonnet"),
    (_v("haiku", 0.9), "agent", "codex", "tier_haiku"),
    (_v(None, None, source="timeout"), "prompt", "", "no_verdict"),
    (None, "agent", "", "no_verdict"),
])
def test_pick_door(v, kind, door, reason, monkeypatch):
    monkeypatch.setattr(dv, "local_lane_passed", lambda task_type: False)
    assert dv.pick_door(v, kind=kind) == (door, reason)


def test_a_passed_local_lane_makes_the_prompt_door_local(monkeypatch):
    monkeypatch.setattr(dv, "local_lane_passed", lambda task_type: True)
    assert dv.pick_door(_v("haiku", 0.9), kind="prompt") == ("local", "tier_haiku")
    assert dv.pick_door(_v("haiku", 0.9), kind="agent") == ("codex", "tier_haiku")   # sub-agents never go "local"


def test_local_lane_passed_reads_fresh_quality_batches_and_nothing_else(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    assert dv.local_lane_passed("query") is False        # no file: no lane
    now = time.time()
    (tmp_path / "lane_quality.jsonl").write_text(json.dumps(
        {"ts": now, "door": "mcp", "served_class": "local", "task_group": "qa", "kind": "audit",
         "n": 30, "acceptable": 27, "source": "t"}) + "\n")
    assert dv.local_lane_passed("query") is True          # Q&A lane passed
    assert dv.local_lane_passed("code") is False          # the code lane did not


def test_the_opus_threshold_is_tunable(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_DOOR_OPUS_MIN_CONF", "0.9")
    assert dv.pick_door(_v("opus", 0.8), kind="prompt")[0] != "claude"
    monkeypatch.setenv("LLM_ROUTER_DOOR_OPUS_MIN_CONF", "7")      # out of range: the default
    assert dv.opus_min_conf() == dv.DEFAULT_OPUS_MIN_CONF


# ---- the handle and the row -------------------------------------------------------------------------------------


def test_shadow_logs_and_never_applies(ollama):
    ollama.probs = {"haiku": 0.05, "sonnet": 0.15, "opus": 0.8}
    p = dv.start("redesign the whole auth flow", "prompt", session_id="s1")
    dec = p.decide(task_type="analyze")
    assert dec.door == "claude" and dec.applied is False
    row = _rows()[-1]
    assert (row["mode"], row["door"], row["applied"], row["outcome"]) == ("shadow", "claude", False, "no_change")
    assert row["tier"] == "opus" and row["session_id"] == "s1" and row["hook_kind"] == "prompt"


def test_on_applies_a_confident_opus_verdict_and_logs_the_outcome(ollama, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    ollama.probs = {"haiku": 0.05, "sonnet": 0.15, "opus": 0.8}
    dec = dv.start("redesign the whole auth flow", "agent", tool_use_id="toolu_1").decide(task_type="code")
    assert dec.applied and dec.door == "claude"
    row = _rows()[-1]
    assert (row["applied"], row["outcome"], row["tool_use_id"]) == (True, "codex_skipped", "toolu_1")


def test_on_does_not_apply_a_verdict_that_keeps_the_work_on_a_non_claude_door(ollama, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    dec = dv.start("what does os.path.join do", "prompt").decide(task_type="query")
    assert dec.door == "mcp" and dec.applied is False


def test_no_verdict_keeps_todays_behaviour(ollama, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    ollama.fail = True
    dec = dv.start("a prompt", "prompt").decide()
    assert (dec.door, dec.applied) == ("", False)


@pytest.mark.timing
def test_a_late_model_reads_as_a_timeout_not_a_hang(ollama, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_DOOR_VERDICT_BUDGET_MS", "300")
    ollama.first_call_delay = 2.0
    t0 = time.monotonic()
    dec = dv.start("a prompt", "prompt").decide()
    assert time.monotonic() - t0 < 1.5
    assert dec.verdict.source == "timeout" and dec.door == ""


def test_a_row_holds_no_prompt_text(ollama):
    secret = "the launch code is swordfish-7731"
    dv.start(secret, "prompt", session_id="s2").decide()
    raw = paths.state_path(dv.LEDGER).read_text()
    assert "swordfish" not in raw and "launch" not in raw
    row = json.loads(raw.splitlines()[-1])
    assert row["text_sha"] == dv.text_sha(secret) and row["chars"] == len(secret)


def test_a_hook_that_exits_early_still_leaves_its_row(ollama):
    p = dv.start("a prompt", "prompt")
    p._at_exit()                       # what atexit runs when the hook calls sys.exit before its decision point
    assert _rows()[-1]["outcome"] == "exit_before_decision"
    p._at_exit()                       # and only once
    assert len(_rows()) == 1


def test_blank_text_and_unknown_kind_start_nothing(ollama):
    assert dv.start("   ", "prompt") is None
    assert dv.start("text", "other") is None
    assert ollama.calls == 0


# ---- the warm-up ------------------------------------------------------------------------------------------------


def test_warm_loads_the_decision_model_with_the_verdict_keep_alive(ollama, monkeypatch):
    assert dv.warm() == "nimble:9b"
    assert ollama.resident is True and ollama.calls == 1


def test_warm_is_a_noop_when_off_or_opted_out(ollama, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_PRELOAD", "off")
    assert dv.warm() is None
    monkeypatch.delenv("LLM_ROUTER_CLASSIFIER_PRELOAD")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "off")
    assert dv.warm() is None
    assert ollama.calls == 0


def test_warm_reports_a_model_that_did_not_load(ollama):
    ollama.fail = True
    assert dv.warm() is None


# ---- the report -------------------------------------------------------------------------------------------------


def test_summarize_counts_coverage_latency_cold_and_doors():
    rows = [
        {"kind": "door_verdict", "source": "llm", "tier": "sonnet", "ms": 400.0, "resident": True, "door": "mcp",
         "outcome": "no_change", "applied": False, "hook_kind": "prompt"},
        {"kind": "door_verdict", "source": "llm", "tier": "opus", "ms": 2100.0, "resident": False, "door": "claude",
         "outcome": "hint_quieted", "applied": True, "hook_kind": "prompt"},
        {"kind": "door_verdict", "source": "timeout", "tier": None, "ms": 5000.0, "resident": None, "door": "",
         "outcome": "no_change", "applied": False, "hook_kind": "agent"},
        {"kind": "other"},
    ]
    s = dv.summarize(rows)
    assert (s["n"], s["verdicts"], s["coverage"]) == (3, 2, round(2 / 3, 4))
    assert s["cold_n"] == 1 and s["residency_known_n"] == 2 and s["cold_share"] == 0.5
    assert s["doors"] == {"mcp": 1, "claude": 1, "none": 1} and s["applied"] == 1
    assert s["by_kind"] == {"prompt": 2, "agent": 1}
    assert dv.summarize([])["coverage"] is None
