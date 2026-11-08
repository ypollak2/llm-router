"""NS-A2: the per-turn classifier verdict is ENFORCED as a veto, and only for its turn.

Plan section 2 table, row by row; the veto never sets the conversation pin; a vetoed turn's
continuations stay on Claude with no second classifier call; the next human turn is classified
again; shadow mode and an unset decision model veto nothing; a failed local step still pins the
conversation (the stated residual); the classifier latency is measured with a slow fake.

No Ollama, no network: the classifier is injected, Anthropic is a mocked upstream and the serving
model a fake backend (see tests/test_proxy_cls_decide.py, whose helpers are reused).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from llm_router import lane_quality
from llm_router import local_classifier as lc
from llm_router.proxy import cls_decide, ledger, local_mode
from llm_router.proxy import server as ps
from tests.test_proxy_cls_decide import (  # noqa: F401  (fixtures and helpers)
    ADDITIVE, SID, Env, FakeClassifier, _base_rows, _comparable, _env, _script, decision_model_on, env,
    passing_proxy_lane, turn_body, verdict,
)
from tests.test_proxy_local_mode import Backend, Upstream, _ollama, _overrides_readable  # noqa: F401


def lane_rows(monkeypatch, rows):
    calls = []

    def read(path=None):
        calls.append(1)
        return rows

    monkeypatch.setattr(lane_quality, "read_rows", read)
    return calls


def audit(n=30, acceptable=28, door="proxy", served="local"):
    return {"ts": time.time() - 3600, "door": door, "served_class": served, "task_group": "qa",
            "kind": "audit", "n": n, "acceptable": acceptable, "source": "test"}


def served_local(env_, row):
    return row["decision"] == ledger.DECISION_SERVED


# -- section 2 table, row by row -------------------------------------------------------


@pytest.mark.parametrize("tier", ["opus", "sonnet"])
@pytest.mark.parametrize("source", ["llm", "cache"])
def test_tier_opus_or_sonnet_vetoes_the_turn(env, monkeypatch, tier, source):
    decision_model_on(monkeypatch)
    FakeClassifier(monkeypatch, source=source, tier=tier)
    env.post(turn_body(["design a multi-file change"]))
    (row,) = env.rows()
    assert row["cls_turn_reason"] == "veto_tier" and row["cls_turn_decided"] is True
    assert row["cls_applied"] is True
    assert row["decision"] == ledger.DECISION_FORWARDED and row["reason"] == "classifier_veto"
    assert row["local_mode"]["reason"] == "classifier_veto" and row["local_mode"]["detail"] == "veto_tier"
    assert row["egress"] is True and len(env.up.requests) == 1 and env.backend.calls == []


def test_tier_haiku_with_a_passing_lane_serves_local(env, monkeypatch):
    decision_model_on(monkeypatch)
    lane_rows(monkeypatch, [audit()])
    FakeClassifier(monkeypatch, tier="haiku")
    env.post(turn_body(["rename the helper"]))
    (row,) = env.rows()
    assert row["cls_turn_reason"] == "allow" and row["cls_applied"] is False
    assert row["decision"] == ledger.DECISION_SERVED and env.up.requests == [] and len(env.backend.calls) == 1


@pytest.mark.parametrize("rows", [
    [audit(n=30, acceptable=20)],                       # latest (proxy, local) batch failed
    [audit(n=5, acceptable=5)],                         # a (proxy, local) lane exists, too few to pass
    [audit(n=30, acceptable=28), {**audit(n=30, acceptable=10), "ts": time.time() - 60}],  # newer failing batch wins
], ids=["failing_batch", "too_few", "newer_failing_batch"])
def test_tier_haiku_with_a_failing_proxy_local_lane_vetoes(env, monkeypatch, rows):
    decision_model_on(monkeypatch)
    lane_rows(monkeypatch, rows)
    FakeClassifier(monkeypatch, tier="haiku")
    env.post(turn_body(["rename the helper"]))
    (row,) = env.rows()
    assert row["cls_turn_reason"] == "veto_lane" and row["cls_turn_decided"] is True
    assert row["cls_turn_tier"] == "haiku" and row["cls_applied"] is True
    assert row["decision"] == ledger.DECISION_FORWARDED and row["local_mode"]["detail"] == "veto_lane"
    assert env.backend.calls == [] and len(env.up.requests) == 1


@pytest.mark.parametrize("rows", [
    [],
    [audit(door="mcp", acceptable=1)],                  # another door's failing evidence is not the proxy's
    [audit(served="haiku", acceptable=1)],              # lane A's own (proxy, haiku) rows: no local evidence
    [audit(served="haiku")],                            # ... passing or failing
    [audit(n=30, acceptable=10, served="haiku")],
    [{"ts": time.time(), "door": "proxy", "served_class": "local", "task_group": "qa", "kind": "audit"}],  # malformed
], ids=["no_lane", "other_door", "lane_a_haiku_failing", "lane_a_haiku_passing", "lane_a_haiku_n30", "malformed"])
def test_tier_haiku_without_proxy_local_evidence_is_allowed(env, monkeypatch, rows):
    decision_model_on(monkeypatch)
    lane_rows(monkeypatch, rows)
    FakeClassifier(monkeypatch, tier="haiku")
    env.post(turn_body(["rename the helper"]))
    (row,) = env.rows()
    assert row["cls_turn_reason"] == "allow" and row["cls_applied"] is False
    assert row["decision"] == ledger.DECISION_SERVED and env.up.requests == [] and len(env.backend.calls) == 1


def test_a_passing_lane_alongside_lane_a_haiku_rows_is_allowed(env, monkeypatch):
    decision_model_on(monkeypatch)
    lane_rows(monkeypatch, [audit(), audit(served="haiku", acceptable=1)])
    FakeClassifier(monkeypatch, tier="haiku")
    env.post(turn_body(["rename the helper"]))
    (row,) = env.rows()
    assert row["cls_turn_reason"] == "allow" and row["decision"] == ledger.DECISION_SERVED


def test_an_unreadable_lane_file_on_disk_is_a_veto_not_an_error(env, monkeypatch, tmp_path):
    from llm_router import paths

    decision_model_on(monkeypatch)
    lane_rows(monkeypatch, [])                                           # read_rows maps unreadable to []
    monkeypatch.setattr(paths, "state_path", lambda name: tmp_path)      # a directory: exists, read raises OSError
    FakeClassifier(monkeypatch, tier="haiku")
    env.post(turn_body(["rename the helper"]))
    (row,) = env.rows()
    assert row["cls_turn_reason"] == "veto_lane" and row["decision"] == ledger.DECISION_FORWARDED


def test_an_unreadable_lane_file_is_a_veto_not_an_error(env, monkeypatch):
    decision_model_on(monkeypatch)

    def boom(path=None):
        raise RuntimeError("lane file gone")

    monkeypatch.setattr(lane_quality, "read_rows", boom)
    FakeClassifier(monkeypatch, tier="haiku")
    env.post(turn_body(["rename the helper"]))
    (row,) = env.rows()
    assert row["cls_turn_reason"] == "veto_lane" and row["decision"] == ledger.DECISION_FORWARDED


@pytest.mark.parametrize("source", ["timeout", "parse_error", "cold", "off", "abstain", "refused_remote"])
def test_each_failopen_source_serves_local_exactly_as_today_and_applies_nothing(env, monkeypatch, source):
    decision_model_on(monkeypatch)
    FakeClassifier(monkeypatch, source=source)
    env.post(turn_body(["fix the bug"]))
    (row,) = env.rows()
    assert row["cls_turn_reason"] == f"failopen:{source}" and row["cls_turn_decided"] is False
    assert row["cls_applied"] is False and row["decision"] == ledger.DECISION_SERVED
    assert env.up.requests == [] and len(env.backend.calls) == 1


def test_a_secret_holding_prompt_is_not_vetoed_and_is_not_sent_to_the_classifier(env, monkeypatch):
    decision_model_on(monkeypatch)
    fake = FakeClassifier(monkeypatch, tier="opus")
    env.post(turn_body(["use this key sk-ant-api03-" + "q8Zx3LmP9vRtY2wKcN5bH7dF1gJ4sAe6UoI0xW"]))
    (row,) = env.rows()
    assert fake.calls == [] and row["cls_applied"] is False and row["decision"] == ledger.DECISION_SERVED


def test_override_rows_are_never_vetoed(env, monkeypatch):
    decision_model_on(monkeypatch)
    fake = FakeClassifier(monkeypatch, tier="opus")
    env.post(turn_body(["local: fix the bug"]))
    (row,) = env.rows()
    assert fake.calls == [] and row["cls_applied"] is False and row["decision"] == ledger.DECISION_SERVED


# -- active only when ---------------------------------------------------------------------


def test_shadow_mode_is_no_veto(env, monkeypatch):
    decision_model_on(monkeypatch)
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    fake = FakeClassifier(monkeypatch, tier="opus")
    env.post(turn_body(["design a multi-file change"]))
    (row,) = env.rows()
    assert fake.calls == [] and row["cls_applied"] is False and row["decision"] == ledger.DECISION_SERVED


def test_decision_model_unset_is_no_veto_and_rows_equal_base_apart_from_the_additive_keys(tmp_path, monkeypatch):
    (tmp_path / "base").mkdir()
    (tmp_path / "new").mkdir()
    base = _base_rows(tmp_path, monkeypatch)

    async def _boom(*a, **k):
        raise AssertionError("no policy")

    monkeypatch.setattr(ps, "choose_model", _boom)

    async def forbidden(*a, **k):
        raise AssertionError("the classifier must not run with the decision model unset")

    monkeypatch.setattr(lc, "classify_async", forbidden)
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")        # mode on, but no decision model configured
    new = _script(Env(tmp_path / "new"))
    assert len(base) == len(new) == 5
    for b, n in zip(base, new):
        assert _comparable(n, drop_additive=True) == _comparable(b, drop_additive=False)
        assert n["decision"] == b["decision"] and n.get("reason") == b.get("reason")
        assert n["cls_applied"] is False and ADDITIVE <= set(n)


def test_a_stored_veto_is_lifted_when_the_mode_is_switched_to_shadow(env, monkeypatch):
    """Rollback takes effect on the next request, stored vetoes included."""
    decision_model_on(monkeypatch)
    FakeClassifier(monkeypatch, tier="opus")
    env.post(turn_body(["design a multi-file change"], loops=0))
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    env.post(turn_body(["design a multi-file change"], loops=1))
    first, second = env.rows()
    assert first["cls_applied"] is True and first["decision"] == ledger.DECISION_FORWARDED
    assert second["cls_applied"] is False                       # the verdict is no longer taken


# -- per turn, not per conversation ---------------------------------------------------------


def test_a_vetoed_turns_continuations_stay_on_claude_with_no_extra_classifier_call(env, monkeypatch):
    decision_model_on(monkeypatch)
    fake = FakeClassifier(monkeypatch, tier="opus")
    for loops in (0, 1, 2, 3):
        env.post(turn_body(["design a multi-file change"], loops=loops))
    rows = env.rows()
    assert len(fake.calls) == 1 and len(env.up.requests) == 4 and env.backend.calls == []
    assert [r["cls_turn_source"] for r in rows] == ["llm", "inherit", "inherit", "inherit"]
    assert all(r["cls_applied"] is True and r["decision"] == ledger.DECISION_FORWARDED for r in rows)
    assert len({r["cls_decision_id"] for r in rows}) == 1
    assert all(r["local_mode"]["reason"] == "classifier_veto" for r in rows)
    assert not any("pin_change" in r["local_mode"] for r in rows)       # nothing was pinned


def test_the_next_human_turn_is_classified_again_and_can_go_local(env, monkeypatch):
    decision_model_on(monkeypatch)
    lane_rows(monkeypatch, [audit()])
    fake = FakeClassifier(monkeypatch, tier="opus")
    env.post(turn_body(["design a multi-file change"], loops=0))              # turn 1: vetoed
    fake.tier = "haiku"
    env.post(turn_body(["design a multi-file change", "now rename the helper"], loops=0))   # turn 2
    env.post(turn_body(["design a multi-file change", "now rename the helper"], loops=2))   # its continuation
    t1, t2, t2c = env.rows()
    assert len(fake.calls) == 2                                              # one call per human turn
    assert (t1["cls_turn_reason"], t1["decision"]) == ("veto_tier", ledger.DECISION_FORWARDED)
    assert (t2["cls_turn_reason"], t2["cls_applied"]) == ("allow", False)
    assert t2["decision"] == ledger.DECISION_SERVED and t2c["decision"] == ledger.DECISION_SERVED
    assert t2["cls_decision_id"] != t1["cls_decision_id"] and t2c["cls_decision_id"] == t2["cls_decision_id"]
    assert t2["local_mode"]["reason"] == "local"


def test_a_veto_on_turn_1_does_not_make_turn_2_started_elsewhere_or_pin_the_conversation(env, monkeypatch):
    decision_model_on(monkeypatch)
    fake = FakeClassifier(monkeypatch, tier="opus")
    env.post(turn_body(["design a multi-file change"], loops=0))
    env.post(turn_body(["design a multi-file change", "and then the docs"], loops=0))       # vetoed again
    t1, t2 = env.rows()
    assert t2["local_mode"]["reason"] == "classifier_veto"                   # not conversation_started_on_claude
    assert t2["cls_turn_reason"] == "veto_tier" and t2["cls_turn_source"] == "llm"
    assert t1["local_mode"]["pin"] is None and t2["local_mode"]["pin"] is None
    fake.tier = "haiku"
    lane_rows(monkeypatch, [audit()])
    env.post(turn_body(["design a multi-file change", "and then the docs", "rename it"], loops=0))
    assert env.rows()[2]["decision"] == ledger.DECISION_SERVED and len(env.backend.calls) == 1


def test_a_conversation_never_seen_by_this_process_is_still_started_elsewhere(env, monkeypatch):
    """The restart rule is intact: `seen` is memory, and a continuation of an unseen conversation pins Claude."""
    decision_model_on(monkeypatch)
    FakeClassifier(monkeypatch, tier="opus")
    env.post(turn_body(["design a multi-file change"], loops=0))
    env.app = env.build()
    env.post(turn_body(["design a multi-file change"], loops=2))
    after = env.rows()[1]
    assert after["cls_turn_reason"] == "started_elsewhere"
    assert after["local_mode"]["reason"] == "conversation_started_on_claude" and after["cls_applied"] is False


def test_non_veto_first_call_refusals_still_pin_as_before(tmp_path, monkeypatch):
    """Only a veto skips the pin: a first call refused for another reason (kill switch) still does."""
    decision_model_on(monkeypatch)
    lane_rows(monkeypatch, [audit()])
    FakeClassifier(monkeypatch, tier="haiku")
    e = Env(tmp_path)
    e.kill.write_text("x")
    e.post(turn_body(["rename the helper"]))
    e.kill.unlink()
    e.post(turn_body(["rename the helper"], loops=1))
    first, second = e.rows()
    assert first["local_mode"]["reason"] == "kill_switch" and first["local_mode"]["pin_change"] == "new->claude"
    assert second["local_mode"]["reason"] == "pinned_claude" and second["cls_applied"] is False


# -- 705: a failed local step (the stated residual) ----------------------------------------


def test_a_failed_local_step_still_pins_the_conversation_to_claude_residual(tmp_path, monkeypatch):
    decision_model_on(monkeypatch)
    lane_rows(monkeypatch, [audit()])
    fake = FakeClassifier(monkeypatch, tier="haiku")
    e = Env(tmp_path)
    e.backend = Backend(_ollama("", [{"function": {"name": "Nope", "arguments": {}}}]))
    e.app = e.build(e.backend)
    e.post(turn_body(["rename the helper"], loops=0))                        # allowed, but the local step fails
    e.post(turn_body(["rename the helper", "and the other"], loops=0))       # next human turn: classified, allowed
    fail, nxt = e.rows()
    assert fail["decision"] == ledger.DECISION_FALLBACK and fail["local_mode"]["pin_change"] == "local->claude"
    assert len(fake.calls) == 2 and nxt["cls_turn_reason"] == "allow"
    assert nxt["reason"] == "pinned_claude" and nxt["cls_applied"] is False   # served by Claude, not a veto
    assert len(e.backend.calls) == 1                                          # not retried locally


# -- ledger, KPI, ns-check ------------------------------------------------------------------


def test_cls_applied_is_true_exactly_on_veto_rows(env, monkeypatch):
    decision_model_on(monkeypatch)
    lane_rows(monkeypatch, [audit()])
    fake = FakeClassifier(monkeypatch, tier="opus")
    env.post(turn_body(["a"], loops=0))
    fake.tier = "haiku"
    env.post(turn_body(["a", "b"], loops=0))
    env.post(turn_body(["a", "b", "c"], loops=0))
    side = turn_body(["title this"])
    side.pop("tools")
    env.post(side)
    for r in env.rows():
        assert r["cls_applied"] is (r["decision"] == ledger.DECISION_FORWARDED and r["cls_turn_reason"] == "veto_tier")


def test_four_turn_script_keeps_idx_and_sha_per_turn_and_continuations_equal_their_turn(env, monkeypatch):
    """rv17c S4: per session, T1 rows share idx a, T2 a+1, T3 a+2, T4 a+3; four distinct shas."""
    decision_model_on(monkeypatch)
    lane_rows(monkeypatch, [audit()])
    fake = FakeClassifier(monkeypatch, tier="haiku")
    prompts = ["first prompt", "second prompt", "third prompt", "fourth prompt"]
    for t in range(1, 5):
        fake.tier = "opus" if t == 3 else "haiku"
        for loops in (0, 2):
            env.post(turn_body(prompts[:t], loops=loops))
    rows = env.rows()
    assert len(rows) == 8 and len(fake.calls) == 4
    by_turn = {}
    for r in rows:
        by_turn.setdefault(r["cls_turn_idx"], []).append(r)
    assert sorted(by_turn) == [1, 2, 3, 4] and all(len(v) == 2 for v in by_turn.values())
    assert len({v[0]["cls_turn_sha"] for v in by_turn.values()}) == 4
    assert all(len({x["cls_turn_sha"] for x in v}) == 1 and len({x["cls_decision_id"] for x in v}) == 1
               for v in by_turn.values())
    assert [v[0]["cls_turn_reason"] for _, v in sorted(by_turn.items())] == ["allow", "allow", "veto_tier", "allow"]
    assert all(x["decision"] == ledger.DECISION_FORWARDED for x in by_turn[3])      # T3 on Claude
    assert all(x["decision"] == ledger.DECISION_SERVED for t in (1, 2, 4) for x in by_turn[t])   # the rest local


def test_turn_key_is_computed_once_per_call(env, monkeypatch):
    decision_model_on(monkeypatch)
    FakeClassifier(monkeypatch, tier="opus")
    real, seen = cls_decide.turn_key, []
    monkeypatch.setattr(cls_decide, "turn_key", lambda body, sid: seen.append(1) or real(body, sid))
    env.post(turn_body(["design a multi-file change"]))
    assert len(seen) == 1
    env.post(turn_body(["design a multi-file change"], loops=1))              # a continuation: also once
    assert len(seen) == 2


def test_lane_evidence_is_read_once_per_human_turn_not_per_call(env, monkeypatch):
    decision_model_on(monkeypatch)
    reads = lane_rows(monkeypatch, [audit()])
    FakeClassifier(monkeypatch, tier="haiku")
    for loops in (0, 1, 2):
        env.post(turn_body(["rename the helper"], loops=loops))
    assert len(reads) == 1


# -- latency (rv25): measured with a slow fake classifier -------------------------------------


def test_classifier_latency_is_paid_once_per_human_turn_and_continuations_do_not_wait(env, monkeypatch):
    decision_model_on(monkeypatch)
    delay = 0.30

    class Slow(FakeClassifier):
        async def __call__(self, assembled, *, session_id, text_sha, **kw):
            await asyncio.sleep(delay)
            return await super().__call__(assembled, session_id=session_id, text_sha=text_sha, **kw)

    fake = Slow(monkeypatch, tier="opus")
    t0 = time.perf_counter()
    env.post(turn_body(["design a multi-file change"], loops=0))
    human_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    env.post(turn_body(["design a multi-file change"], loops=1))
    cont_s = time.perf_counter() - t0
    human, cont = env.rows()
    print(f"\nLATENCY slow-fake {delay * 1000:.0f} ms: human-turn call {human_s * 1000:.0f} ms "
          f"(cls_turn_ms {human['cls_turn_ms']}), continuation call {cont_s * 1000:.0f} ms (cls_turn_ms {cont['cls_turn_ms']})")
    assert len(fake.calls) == 1
    assert human["cls_turn_ms"] >= delay * 1000 and human_s >= delay
    assert human["cls_turn_ms"] < delay * 1000 + 250                       # overhead beside the model: assembly + lane read
    assert cont["cls_turn_ms"] is None and cont_s < delay                  # the store read costs no model time


# -- rv30 S4: a hung classifier through the request handler ------------------------------------


def test_a_hung_classifier_is_bounded_then_failopen_and_serves_local(env, monkeypatch):
    """Not the injected fake: the real classify_async runs; the residence probe hangs. The bound is
    the classifier's own asyncio.timeout(budget); the turn then serves local as a failopen."""
    decision_model_on(monkeypatch)
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER_TIMEOUT_MS", "300")
    assert lc.DEFAULT_TIMEOUT_MS == 2000                                  # the plan's production bound
    monkeypatch.setattr(lc, "_cool_until", 0.0)
    monkeypatch.setattr(lc, "_resident_until", 0.0)
    monkeypatch.setattr(lc, "_inflight", {})

    async def hang(*a, **k):
        await asyncio.sleep(60)

    monkeypatch.setattr(lc, "_is_loaded", hang)
    t0 = time.perf_counter()
    env.post(turn_body(["rename the helper"]))
    wall = time.perf_counter() - t0
    (row,) = env.rows()
    assert row["cls_turn_source"] == "timeout" and row["cls_turn_reason"] == "failopen:timeout"
    assert row["cls_turn_decided"] is False and row["cls_applied"] is False
    assert row["decision"] == ledger.DECISION_SERVED and env.up.requests == []
    assert 0.25 <= wall < 1.0, wall


def test_a_hung_classifier_at_the_real_plan_bound_is_failopen_within_2_2_s(env, monkeypatch):
    """No timeout override: the production 2.0 s budget (LLM_ROUTER_LOCAL_CLASSIFIER_TIMEOUT_MS unset)."""
    decision_model_on(monkeypatch)
    monkeypatch.delenv("LLM_ROUTER_LOCAL_CLASSIFIER_TIMEOUT_MS", raising=False)
    monkeypatch.setattr(lc, "_cool_until", 0.0)
    monkeypatch.setattr(lc, "_resident_until", 0.0)
    monkeypatch.setattr(lc, "_inflight", {})

    async def hang(*a, **k):
        await asyncio.sleep(60)

    monkeypatch.setattr(lc, "_is_loaded", hang)
    t0 = time.perf_counter()
    env.post(turn_body(["rename the helper"]))
    wall = time.perf_counter() - t0
    (row,) = env.rows()
    assert row["cls_turn_source"] == "timeout" and row["decision"] == ledger.DECISION_SERVED
    assert 1.9 <= wall <= 2.2, wall
