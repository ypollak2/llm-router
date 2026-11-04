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


def test_a_prompt_is_never_held_longer_than_two_seconds_but_a_spawn_may_wait(ollama, monkeypatch):
    assert dv.budget_s("prompt") == 2.0 and dv.budget_s("agent") == dv.budget_s() == 8.0
    monkeypatch.setenv("LLM_ROUTER_DOOR_VERDICT_PROMPT_BUDGET_MS", "999999")
    assert dv.budget_s("prompt") == 8.0                       # never above the full budget
    monkeypatch.setenv("LLM_ROUTER_DOOR_VERDICT_BUDGET_MS", "1000")
    assert dv.budget_s("prompt") == 1.0 and dv.budget_s("agent") == 1.0


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


def test_unset_means_shadow_only_when_the_decision_model_is_configured(ollama, monkeypatch):
    """The default (W10 follow-up): a deploy produces coverage data without anyone editing .env, but a machine with no
    decision model (the chat backend would pick up a general model) makes no call."""
    monkeypatch.delenv("LLM_ROUTER_LOCAL_CLASSIFIER")
    assert dv.mode() == dv.DEFAULT_MODE == "shadow"
    monkeypatch.delenv("LLM_ROUTER_DECISION_MODEL")
    assert dv.mode() == "off"
    monkeypatch.setenv("LLM_ROUTER_DECISION_MODEL", "nimble:9b")
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_BACKEND", "chat")
    assert dv.mode() == "off"
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_BACKEND", "systemone")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "typo")
    assert dv.mode() == "off"                       # anything but shadow / on, once set, is off
    assert ollama.calls == 0


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


@pytest.fixture
def doors(monkeypatch):
    """No local lane, Codex usable: the state of this machine today with Codex not benched."""
    monkeypatch.setattr(dv, "local_lane_for", lambda kind, task_type=None, text=None: "")
    monkeypatch.setattr(dv, "codex_usable", lambda: True)


@pytest.mark.parametrize("v,kind,door,reason", [
    (_v("opus", 0.7), "prompt", "claude", "opus_confident"),
    (_v("opus", 0.6), "agent", "claude", "opus_confident"),
    (_v("opus", 0.59), "prompt", "mcp", "opus_unsure"),
    (_v("opus", 0.5), "agent", "codex", "opus_unsure"),
    (_v("sonnet", 0.9), "prompt", "codex", "tier_sonnet"),
    (_v("sonnet", 0.5), "prompt", "codex", "tier_sonnet"),
    (_v("sonnet", 0.49), "prompt", "mcp", "tier_sonnet"),       # a tie resolved up, or no preference: the default door
    (_v("sonnet", 0.9), "agent", "codex", "tier_sonnet"),
    (_v("haiku", 0.9), "agent", "codex", "tier_haiku"),         # no local lane: the next door
    (_v("haiku", 0.9), "prompt", "codex", "tier_haiku"),
    (_v("haiku", 0.6), "prompt", "mcp", "tier_haiku"),          # under the haiku threshold
    (_v(None, None, source="timeout"), "prompt", "", "no_verdict"),
    (None, "agent", "", "no_verdict"),
])
def test_pick_door(v, kind, door, reason, doors):
    assert dv.pick_door(v, kind=kind) == (door, reason)


def test_a_prompt_whose_codex_is_unusable_stays_on_the_default_door(monkeypatch):
    monkeypatch.setattr(dv, "local_lane_for", lambda kind, task_type=None, text=None: "")
    monkeypatch.setattr(dv, "codex_usable", lambda: False)
    assert dv.pick_door(_v("sonnet", 0.9), kind="prompt") == ("mcp", "tier_sonnet")
    assert dv.pick_door(_v("sonnet", 0.9, "agent"), kind="agent") == ("codex", "tier_sonnet")   # the NS3 gates decide


def test_a_confident_haiku_goes_local_first_where_a_lane_exists(monkeypatch):
    monkeypatch.setattr(dv, "codex_usable", lambda: True)
    c = dv.preferred_door(_v("haiku", 0.8), kind="prompt", local_lane="qa")
    assert (c.door, c.reason, c.lane) == ("local", "haiku_local_lane", "qa")
    # the seam a sub-agent fallback uses: it says it has a lane
    c = dv.preferred_door(_v("haiku", 0.8, "agent"), kind="agent", local_lane=True)
    assert (c.door, c.lane) == ("local", "caller")
    # and says it has none
    assert dv.preferred_door(_v("haiku", 0.8, "agent"), kind="agent", local_lane=False).door == "codex"
    # a lane is not enough for a haiku verdict that is not confident, or for another tier
    assert dv.preferred_door(_v("haiku", 0.69), kind="prompt", local_lane=True).door == "mcp"
    assert dv.preferred_door(_v("sonnet", 0.95), kind="prompt", local_lane=True).door == "codex"
    assert dv.preferred_door(_v("opus", 0.95), kind="prompt", local_lane=True).door == "claude"


def test_the_local_lane_is_looked_up_from_evidence_when_the_caller_does_not_say(monkeypatch):
    seen = []
    monkeypatch.setattr(dv, "local_lane_for", lambda kind, task_type=None, text=None: seen.append((kind, task_type, text)) or "code")
    monkeypatch.setattr(dv, "codex_usable", lambda: True)
    c = dv.preferred_door(_v("haiku", 0.9, "agent"), kind="agent", task_type="code", text="t")
    assert c.door == "local" and seen == [("agent", "code", "t")]


@pytest.mark.parametrize("text,job", [
    ("Write a commit message for the diff I just made", "commit_message"),
    ("please draft a good commit message for this change", "commit_message"),
    ("Write the PR description for this branch", "pr_description"),
    ("generate a pull request summary", "pr_description"),
    ("what is a commit message convention?", None),
    ("commit this", None),
    ("explain the PR", None),
    ("", None),
    (None, None),
])
def test_text_job_of(text, job):
    assert dv.text_job_of(text) == job


def test_local_lane_for_needs_evidence_and_matches_the_hook_kind(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    assert dv.local_lane_for("prompt", "query") == ""                 # no file: no lane
    now = time.time()
    (tmp_path / "lane_quality.jsonl").write_text(json.dumps(
        {"ts": now, "door": "mcp", "served_class": "local", "task_group": "qa", "kind": "audit",
         "n": 30, "acceptable": 27, "source": "t"}) + "\n")
    assert dv.local_lane_for("prompt", "query") == "qa"               # a passed Q&A lane on the MCP door
    assert dv.local_lane_for("prompt", "code") == ""                  # the code lane did not pass
    assert dv.local_lane_for("agent", "query") == ""                  # a sub-agent needs a sub-agent lane
    (tmp_path / "lane_quality.jsonl").write_text(json.dumps(
        {"ts": now, "door": "subagent", "served_class": "local", "task_group": "code", "kind": "audit",
         "n": 30, "acceptable": 27, "source": "t"}) + "\n")
    assert dv.local_lane_for("agent", "code") == "code"


def test_a_failed_batch_is_no_lane(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    (tmp_path / "lane_quality.jsonl").write_text(json.dumps(
        {"ts": time.time(), "door": "mcp", "served_class": "local", "task_group": "qa", "kind": "audit",
         "n": 30, "acceptable": 13, "source": "t"}) + "\n")
    assert dv.local_lane_for("prompt", "query") == ""


def test_a_commit_message_prompt_has_a_local_lane_only_when_the_text_job_switch_is_on(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    from llm_router.local_check import routing

    text = "Write a commit message for the staged diff"
    assert dv.local_lane_for("prompt", "generate", text) == ""
    routing.enable("qwen3.6:35b-a3b-coding", "commit_message", "manual")
    assert dv.local_lane_for("prompt", "generate", text) == "text_job:commit_message"
    assert dv.local_lane_for("prompt", "generate", "Write the PR description") == ""      # a different job, not enabled
    assert dv.local_lane_for("agent", "generate", text) == ""                              # sub-agents take no text-job lane


def test_hint_notes_name_the_door_and_its_prefix():
    assert 'llm_text_job(job="commit_message"' in dv.hint_note("local", "text_job:commit_message")
    assert 'prompt="local: ' in dv.hint_note("local", "qa", "haiku")
    assert 'prompt="codex: ' in dv.hint_note("codex", "", "sonnet") and "sonnet-level" in dv.hint_note("codex", "", "sonnet")
    assert dv.hint_note("claude") == dv.hint_note("mcp") == dv.hint_note("") == ""


def test_the_haiku_and_sonnet_thresholds_are_tunable(monkeypatch):
    assert (dv.haiku_min_conf(), dv.sonnet_min_conf()) == (0.7, 0.5)
    monkeypatch.setenv("LLM_ROUTER_DOOR_HAIKU_MIN_CONF", "0.9")
    monkeypatch.setenv("LLM_ROUTER_DOOR_SONNET_MIN_CONF", "0.8")
    assert (dv.haiku_min_conf(), dv.sonnet_min_conf()) == (0.9, 0.8)
    monkeypatch.setenv("LLM_ROUTER_DOOR_HAIKU_MIN_CONF", "x")
    assert dv.haiku_min_conf() == dv.DEFAULT_HAIKU_MIN_CONF


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


# ---- never stall a prompt (CLS-STALL-1) ----------------------------------------------------------------------


@pytest.fixture
def detached(monkeypatch):
    """Allow the hook's detached worker (the suite forbids detached children everywhere else, F44)."""
    monkeypatch.delenv("LLM_ROUTER_NO_DETACHED_SPAWN")


def _wait_rows(pred, timeout: float = 20.0) -> list[dict]:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        rows = [r for r in dv.read_rows() if pred(r)]
        if rows:
            return rows
        time.sleep(0.1)
    return []


OPUS = {"haiku": 0.05, "sonnet": 0.15, "opus": 0.8}


@pytest.mark.timing
def test_shadow_never_waits_for_the_model(ollama, detached):
    ollama.first_call_delay = 1.5
    t0 = time.monotonic()
    h = dv.start("a prompt", "prompt", session_id="s1")
    dec = h.decide()
    assert time.monotonic() - t0 < 1.0
    assert isinstance(h, dv.Deferred) and (dec.door, dec.applied, dec.verdict.source) == ("", False, "deferred")
    (bg,) = _wait_rows(lambda r: r.get("outcome") == "background_result")
    assert (bg["source"], bg["tier"], bg["text_sha"], bg["session_id"], bg["applied"]) == ("llm", "sonnet", h.sha, "s1", False)
    assert [r["outcome"] for r in dv.read_rows() if r["outcome"] != "background_result"] == ["background_shadow"]


def test_the_worker_row_holds_no_prompt_text(ollama, detached):
    dv.start("the launch code is swordfish-7731", "prompt").decide()
    _wait_rows(lambda r: r.get("outcome") == "background_result")
    raw = paths.state_path(dv.LEDGER).read_text()
    assert "swordfish" not in raw and "launch" not in raw


@pytest.mark.timing
def test_on_defers_a_prompt_that_finds_the_model_cold_and_the_next_one_decides(ollama, detached, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    ollama.probs = OPUS
    ollama.first_call_delay = 1.2
    t0 = time.monotonic()
    first = dv.start("first prompt after an eviction", "prompt")
    d1 = first.decide()
    assert time.monotonic() - t0 < 1.0                      # the hook did not wait for the load
    assert isinstance(first, dv.Deferred) and (d1.door, d1.applied) == ("", False)
    assert _wait_rows(lambda r: r.get("outcome") == "background_result")      # the worker loaded the model and answered
    second = dv.start("second prompt, model now loaded", "prompt")
    assert isinstance(second, dv.Pending)
    d2 = second.decide()
    assert (d2.door, d2.applied) == ("claude", True)


def test_on_decides_inline_when_the_model_is_warm(ollama, detached, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    ollama.resident = True
    assert isinstance(dv.start("a prompt", "prompt"), dv.Pending)


def test_an_agent_spawn_waits_even_when_the_model_is_cold(ollama, detached, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    ollama.probs = OPUS
    h = dv.start("a spawn brief", "agent")
    assert isinstance(h, dv.Pending)
    assert h.decide().applied is True


def test_shadow_with_the_breaker_open_makes_no_worker(ollama, detached):
    dv.note_result(False)
    dv.note_result(False)
    assert dv.breaker_open()
    assert isinstance(dv.start("a prompt", "prompt"), dv.Pending)     # returns a "breaker" row at once, spawns nothing


def test_without_a_detached_child_shadow_makes_the_verdict_inline(ollama):
    h = dv.start("a prompt", "prompt")                # the suite's default: detached children are forbidden
    assert isinstance(h, dv.Pending) and h.decide().verdict.ok


def test_summarize_covers_a_deferred_decision_by_its_background_row():
    rows = [
        {"kind": "door_verdict", "source": "deferred", "outcome": "background_cold", "door": "", "hook_kind": "prompt"},
        {"kind": "door_verdict", "source": "llm", "tier": "sonnet", "ms": 5800.0, "resident": False,
         "outcome": "background_result", "door": "mcp", "hook_kind": "prompt"},
        {"kind": "door_verdict", "source": "llm", "tier": "opus", "ms": 350.0, "resident": True,
         "outcome": "hint_quieted", "door": "claude", "applied": True, "hook_kind": "prompt"},
        {"kind": "door_verdict", "source": "deferred", "outcome": "background_shadow", "door": "", "hook_kind": "agent"},
    ]
    sm = dv.summarize(rows)
    assert (sm["n"], sm["inline_verdicts"], sm["deferred"], sm["background_verdicts"]) == (3, 1, 2, 1)
    assert sm["verdicts"] == 2 and sm["coverage"] == round(2 / 3, 4) and sm["inline_coverage"] == round(1 / 3, 4)
    assert sm["cold_n"] == 1 and sm["cold_share"] == 0.5 and sm["latency_ms_p50"] == 3075.0
    assert sm["outcomes"] == {"background_cold": 1, "hint_quieted": 1, "background_shadow": 1}


def test_the_row_says_what_the_verdict_asked_the_hook_to_do(ollama, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    monkeypatch.setattr(dv, "codex_usable", lambda: True)
    monkeypatch.setattr(dv, "local_lane_for", lambda kind, task_type=None, text=None: "")
    dv.start("what does os.path.join do", "prompt").decide(task_type="query")
    assert _rows()[-1]["outcome"] == "hint_prefers_codex" and _rows()[-1]["door"] == "codex"
    # the Agent hook acts only on the claude door: a codex door there is "no_change" (the NS3 gates already go first)
    dv.start("a spawn brief", "agent").decide(task_type="analyze")
    assert (_rows()[-1]["door"], _rows()[-1]["applied"], _rows()[-1]["outcome"]) == ("codex", False, "no_change")


def test_defer_log_lets_the_hook_record_what_it_did(ollama, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    monkeypatch.setattr(dv, "codex_usable", lambda: True)
    h = dv.start("what does os.path.join do", "prompt")
    dec = h.decide(task_type="query", defer_log=True)
    assert dec.door == "codex" and _rows() == []              # nothing written yet
    h.record("no_hint_shown")
    h.record("again")                                        # once
    assert [r["outcome"] for r in _rows()] == ["no_hint_shown"]


def test_a_decision_not_recorded_before_exit_is_still_written(ollama):
    h = dv.start("a prompt", "prompt")
    h.decide(defer_log=True)
    h._at_exit()
    assert _rows()[-1]["outcome"] == "exit_before_record"


# ---- the warm-up ------------------------------------------------------------------------------------------------


def test_warm_loads_the_decision_model_only_in_on(ollama, monkeypatch):
    # shadow (the fixture's mode): log-only, so it must not load a model over whatever the other lanes use
    assert dv.warm() is None and ollama.calls == 0 and not dv.preload_enabled()
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    assert dv.preload_enabled()
    assert dv.warm() == "nimble:9b"
    assert ollama.resident is True and ollama.calls == 1


def test_warm_is_a_noop_when_off_or_opted_out(ollama, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_PRELOAD", "off")
    assert dv.warm() is None
    monkeypatch.delenv("LLM_ROUTER_CLASSIFIER_PRELOAD")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "off")
    assert dv.warm() is None
    assert ollama.calls == 0


def test_warm_reports_a_model_that_did_not_load(ollama, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    ollama.fail = True
    assert dv.warm() is None


# ---- shadow never evicts another lane's model (CLS-EVICT-1) -----------------------------------------------------


def test_shadow_skips_when_another_model_is_resident_and_never_loads_over_it(ollama):
    ollama.others = ["qwen3.6:35b-a3b-coding"]          # the local text-job lane's model
    v = dv.classify("write a commit message", "prompt")
    assert (v.source, v.resident, v.ok) == ("skipped_slot_busy", False, False)
    assert ollama.calls == 0 and ollama.others == ["qwen3.6:35b-a3b-coding"]      # no request, nothing evicted
    assert not dv.breaker_open()                        # a skip is not a failure


def test_shadow_classifies_when_the_slot_is_free_or_nimble_is_already_resident(ollama):
    assert dv.classify("first, nothing resident", "prompt").ok and ollama.calls == 1
    ollama.others = ["gemma3:12b"]                      # nimble resident beside a small model: allowed
    assert dv.classify("second", "prompt").ok and ollama.calls == 2


def test_shadow_does_not_guess_when_residency_is_unknown(ollama):
    ollama.ps_fail = True
    v = dv.classify("a prompt", "prompt")
    assert v.source == "skipped_slot_unknown" and ollama.calls == 0


def test_on_loads_the_decision_model_even_beside_another_resident_model(ollama, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    ollama.others = ["qwen3.6:35b-a3b-coding"]
    assert dv.classify("a prompt", "prompt").ok and ollama.calls == 1


def test_a_skipped_shadow_decision_leaves_a_row_that_says_why(ollama, detached):
    ollama.others = ["qwen3.6:35b-a3b-coding"]
    dv.start("a prompt", "prompt").decide()
    (bg,) = _wait_rows(lambda r: r.get("outcome") == "background_result")
    assert bg["source"] == "skipped_slot_busy" and ollama.calls == 0
    assert dv.summarize(dv.read_rows())["background_by_source"] == {"skipped_slot_busy": 1}


# ---- privacy: loopback only, and the secret door first ---------------------------------------------------------


@pytest.mark.timing
def test_a_non_loopback_ollama_is_refused_before_any_request(ollama, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", "http://192.0.2.7:11434")       # TEST-NET: nothing is sent anywhere
    t0 = time.monotonic()
    v = dv.classify("a prompt", "prompt")
    assert v.source == "refused_remote" and not v.ok and time.monotonic() - t0 < 1.0
    from llm_router import local_classifier as lc

    assert lc.classify_local("a prompt").source == "refused_remote"             # the shared client refuses too
    assert dv.warm() is None


def test_the_remote_opt_in_is_explicit_and_only_that_env(monkeypatch):
    from llm_router import local_privacy as lp

    assert lp.classifier_base_allowed("http://127.0.0.1:11434") and lp.classifier_base_allowed("http://localhost:11434")
    assert not lp.classifier_base_allowed("http://192.0.2.7:11434")
    assert not lp.classifier_base_allowed(None)
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_ALLOW_REMOTE", "1")
    assert lp.classifier_base_allowed("http://192.0.2.7:11434")


def test_with_the_opt_in_a_remote_base_is_used(ollama, monkeypatch):
    from llm_router import local_classifier as lc

    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", "http://192.0.2.7:11434")
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_ALLOW_REMOTE", "1")
    seen = []
    monkeypatch.setattr(dv, "_resident_models", lambda base, timeout=0.5: [])
    monkeypatch.setattr(lc, "classify_local", lambda *a, **k: seen.append(k) or lc.Verdict(
        None, "sonnet", None, None, None, None, None, "direct", "llm", "nimble:9b", "sys1", 1.0, 0.7))
    assert dv.classify("a prompt", "prompt").ok and len(seen) == 1


SECRET = "my token is ghp_Zk3Qx9LmW2vTb7Yc4Rn8Hd5Pf1Js6GaE0uXo please use it"


@pytest.mark.parametrize("kind", ["prompt", "agent"])
def test_a_prompt_holding_a_secret_is_classified_by_no_model(ollama, kind):
    h = dv.start(SECRET, kind, session_id="s3")
    assert isinstance(h, dv.Withheld)
    dec = h.decide()
    assert (dec.door, dec.verdict.source, dec.applied) == ("", "withheld_secret", False)
    assert ollama.calls == 0
    raw = paths.state_path(dv.LEDGER).read_text()
    assert "ghp_" not in raw and _rows()[-1]["source"] == "withheld_secret"


def test_a_secret_prompt_is_withheld_even_where_a_worker_would_have_run(ollama, detached):
    assert isinstance(dv.start(SECRET, "prompt"), dv.Withheld) and ollama.calls == 0


# ---- the 2 s bound is the whole thing, and an early exit never waits ---------------------------------------------


@pytest.mark.timing
def test_a_prompt_decided_in_process_is_bounded_to_two_seconds_in_all(ollama, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    ollama.resident = True
    ollama.post_delay = 6.0
    t0 = time.monotonic()
    h = dv.start("a prompt", "prompt")
    dec = h.decide()
    assert time.monotonic() - t0 < 2.4                      # the probe is inside the budget, not before it
    assert dec.verdict.source == "timeout" and dec.door == ""


@pytest.mark.timing
def test_an_early_exit_never_waits_for_the_model(ollama, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    ollama.resident = True
    ollama.post_delay = 6.0
    h = dv.start("a prompt", "prompt")
    t0 = time.monotonic()
    h._at_exit()                                           # what atexit runs when the hook calls sys.exit early
    assert time.monotonic() - t0 < 0.5
    r = _rows()[-1]
    assert (r["source"], r["outcome"]) == ("abandoned", "exit_before_decision")


# ---- the Codex hint respects every gate the call will meet -------------------------------------------------------


@pytest.fixture
def codex_gates(monkeypatch):
    from llm_router import codex_agent, codex_meter, codex_window, provider_reset

    class V:
        allowed = True

    state = {"meter": True, "window": True, "bench": None, "binary": True}
    monkeypatch.setattr(codex_agent, "is_codex_available", lambda: state["binary"])
    monkeypatch.setattr(provider_reset, "get_provider_reset_until", lambda k, now=None: state["bench"])
    monkeypatch.setattr(codex_meter, "check", lambda caller, now=None: type("V", (), {"allowed": state["meter"]})())
    monkeypatch.setattr(codex_window, "admit", lambda **kw: type("A", (), {"allowed": state["window"]})())
    return state


def test_codex_usable_when_every_gate_allows(codex_gates):
    assert dv.codex_usable() is True


@pytest.mark.parametrize("gate,value", [("meter", False), ("window", False), ("bench", 9e9), ("binary", False)])
def test_codex_unusable_when_any_gate_refuses(codex_gates, gate, value):
    codex_gates[gate] = value
    assert dv.codex_usable() is False


def test_the_meter_is_asked_as_the_router_chain(monkeypatch, codex_gates):
    from llm_router import codex_meter

    seen = []
    monkeypatch.setattr(codex_meter, "check", lambda caller, now=None: seen.append(caller) or type("V", (), {"allowed": True})())
    assert dv.codex_usable() and seen == ["router_chain"]       # the name the llm() door's Codex call is checked under


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
