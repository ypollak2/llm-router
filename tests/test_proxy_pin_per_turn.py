"""PR-C: ``LLM_ROUTER_PIN_SCOPE=turn`` -- a failed local step pins the rest of ONE human turn, not the conversation.

Turn = ``cls_decide.turn_idx`` (the row's ``cls_turn_idx``, the human-turn index #915 keys its verdicts on).
After ``LLM_ROUTER_PIN_TURN_MAX_FALLBACKS`` fallbacks the pin is sticky under its own reason ``pinned_sticky``, which
the classifier authority (``LLM_ROUTER_CLASSIFIER_AUTHORITY=positive``) never overrides. Flags off: unchanged.

No Ollama, no network: the classifier is injected, Anthropic is a mocked upstream, the serving model a fake backend.
"""
# ruff: noqa: F811  (fixtures are imported and used as arguments)

from __future__ import annotations

import pytest

from llm_router.proxy import ledger, local_mode
from tests.test_proxy_cls_authority import flags, pinned_env  # noqa: F401
from tests.test_proxy_cls_decide import Env, FakeClassifier, turn_body  # noqa: F401
from tests.test_proxy_local_mode import Backend, _ollama, _overrides_readable  # noqa: F401

FIELDS = ("pin_scope", "pin_fallbacks", "pin_cleared")


def failing():
    return Backend(_ollama("", [{"function": {"name": "Nope", "arguments": {}}}]))  # a tool the model may not call


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("LLM_ROUTER_PIN_SCOPE", "LLM_ROUTER_PIN_TURN_MAX_FALLBACKS", "LLM_ROUTER_CLASSIFIER_AUTHORITY",
              "LLM_ROUTER_CLASSIFIER_ELIGIBLE_SHADOW", "LLM_ROUTER_CLASSIFIER_SINGLE_MODEL", "LLM_ROUTER_CLAUDE_MARKER"):
        monkeypatch.delenv(k, raising=False)


def turn_env(tmp_path, monkeypatch, *, max_fb=None):
    monkeypatch.setenv("LLM_ROUTER_PIN_SCOPE", "turn")
    if max_fb is not None:
        monkeypatch.setenv("LLM_ROUTER_PIN_TURN_MAX_FALLBACKS", str(max_fb))
    e = Env(tmp_path)
    e.app = e.build(failing())
    return e


P1, P2, P3, P4 = "rename the helper", "and the other", "third", "fourth"


# -- flag off -----------------------------------------------------------------------


def test_flag_off_a_fallback_pin_is_sticky_and_adds_no_field(tmp_path):
    e = Env(tmp_path)
    e.app = e.build(failing())
    e.post(turn_body([P1]))
    e.post(turn_body([P1, P2]))                                       # a NEW human turn
    fb, nxt = e.rows()
    assert fb["decision"] == ledger.DECISION_FALLBACK and fb["local_mode"]["pin_change"] == "local->claude"
    assert nxt["local_mode"]["reason"] == "pinned_claude" and len(e.backend.calls) == 1
    assert not any(k in r["local_mode"] for r in (fb, nxt) for k in FIELDS)


@pytest.mark.parametrize("val", ["", "sticky", "TURN ", "off"])
def test_only_turn_turns_it_on(tmp_path, monkeypatch, val):
    monkeypatch.setenv("LLM_ROUTER_PIN_SCOPE", val)
    e = Env(tmp_path)
    e.app = e.build(failing())
    e.post(turn_body([P1]))
    e.post(turn_body([P1, P2]))
    expect = "local" if val.strip().lower() == "turn" else "pinned_claude"
    assert e.rows()[-1]["local_mode"]["reason"] == expect


# -- flag on ------------------------------------------------------------------------


def test_pin_holds_within_the_turn_and_is_cleared_at_the_next_human_turn(tmp_path, monkeypatch):
    e = turn_env(tmp_path, monkeypatch)
    e.post(turn_body([P1]))                                           # local fails -> turn pin
    e.post(turn_body([P1], loops=1))                                  # same human turn
    e.post(turn_body([P1], loops=2))                                  # still the same turn
    e.post(turn_body([P1, P2]))                                       # next human turn: local is tried again
    fb, c1, c2, nxt = e.rows()
    assert (fb["local_mode"]["pin_scope"], fb["local_mode"]["pin_fallbacks"]) == ("turn", 1)
    assert c1["local_mode"]["reason"] == c2["local_mode"]["reason"] == "pinned_turn"
    assert len(e.backend.calls) == 2                                  # first call + the new turn; the continuations made none
    assert nxt["local_mode"]["pin_cleared"] == "new_turn" and nxt["decision"] == ledger.DECISION_FALLBACK
    assert nxt["local_mode"]["pin_scope"] == "turn" and nxt["local_mode"]["pin_fallbacks"] == 2


def test_a_good_local_step_after_the_clear_pins_local(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PIN_SCOPE", "turn")
    e = Env(tmp_path)
    e.app = e.build(failing())
    e.post(turn_body([P1]))
    e.backend.replies = [_ollama("done")]                             # the backend recovers
    e.post(turn_body([P1, P2]))
    row = e.rows()[-1]
    assert row["decision"] == ledger.DECISION_SERVED and row["local_mode"]["pin"] == "local"
    assert row["local_mode"]["pin_cleared"] == "new_turn"


def test_sticky_after_n_fallbacks_under_its_own_reason(tmp_path, monkeypatch):
    e = turn_env(tmp_path, monkeypatch, max_fb=3)
    for prompts in ([P1], [P1, P2], [P1, P2, P3]):                    # three failed turns
        e.post(turn_body(prompts))
    scopes = [(r["local_mode"]["pin_scope"], r["local_mode"]["pin_fallbacks"]) for r in e.rows()]
    assert scopes == [("turn", 1), ("turn", 2), ("sticky", 3)]
    e.post(turn_body([P1, P2, P3, P4]))                               # a new turn: still pinned
    e.post(turn_body([P1, P2, P3, P4], loops=1))
    after = e.rows()[3:]
    assert [r["local_mode"]["reason"] for r in after] == ["pinned_sticky", "pinned_sticky"]
    assert len(e.backend.calls) == 3 and all("pin_cleared" not in r["local_mode"] for r in after)


def test_invalid_max_fallbacks_means_three(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PIN_TURN_MAX_FALLBACKS", "lots")
    assert local_mode.pin_turn_max_fallbacks() == 3
    monkeypatch.setenv("LLM_ROUTER_PIN_TURN_MAX_FALLBACKS", "0")
    assert local_mode.pin_turn_max_fallbacks() == 1
    monkeypatch.delenv("LLM_ROUTER_PIN_TURN_MAX_FALLBACKS")
    assert local_mode.pin_turn_max_fallbacks() == 3


def test_other_pins_are_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PIN_SCOPE", "turn")
    e = Env(tmp_path)
    e.kill.write_text("x")
    e.post(turn_body([P1]))                                           # kill switch: a plain pin, not a fallback pin
    e.kill.unlink()
    e.post(turn_body([P1, P2]))
    e.post(turn_body([P1, P2, P3]))
    rows = e.rows()
    assert [r["local_mode"]["reason"] for r in rows] == ["kill_switch", "pinned_claude", "pinned_claude"]
    assert e.backend.calls == [] and not any(k in r["local_mode"] for r in rows for k in FIELDS)


def test_a_structural_pin_is_untouched(tmp_path, monkeypatch):
    from tests.test_proxy_local_mode import _with_image
    monkeypatch.setenv("LLM_ROUTER_PIN_SCOPE", "turn")
    e = Env(tmp_path)
    e.post(_with_image(turn_body([P1])))
    e.post(turn_body([P1, P2]))
    rows = e.rows()
    assert rows[0]["local_mode"]["reason"] == "media_present"
    assert rows[1]["local_mode"]["reason"] in ("pinned_claude", "conversation_started_on_claude")  # today's, not PR-C's
    assert e.backend.calls == [] and not any(k in r["local_mode"] for r in rows for k in FIELDS)


# -- with the classifier authority (#915) ------------------------------------------------


def test_sticky_is_never_overridden_by_the_classifier_authority(tmp_path, monkeypatch):
    flags(monkeypatch, authority=True)
    monkeypatch.setenv("LLM_ROUTER_PIN_SCOPE", "turn")
    monkeypatch.setenv("LLM_ROUTER_PIN_TURN_MAX_FALLBACKS", "1")      # the first fallback is already sticky
    fake = FakeClassifier(monkeypatch, tier="local")
    e = Env(tmp_path)
    e.app = e.build(failing())
    e.post(turn_body([P1]))
    assert e.rows()[-1]["local_mode"]["pin_scope"] == "sticky"
    calls_before = len(fake.calls)
    for prompts in ([P1, P2], [P1, P2, P3]):                          # new human turns, a positive verdict on offer
        e.post(turn_body(prompts))
        row = e.rows()[-1]
        assert row["local_mode"]["reason"] == "pinned_sticky" and row["cls_applied"] is False
        assert "cls_authority" not in row["local_mode"]
    assert len(fake.calls) == calls_before and len(e.backend.calls) == 1   # no verdict asked, local never retried


def test_sticky_after_n_wins_over_a_positive_verdict(tmp_path, monkeypatch):
    flags(monkeypatch, authority=True)
    monkeypatch.setenv("LLM_ROUTER_PIN_SCOPE", "turn")
    monkeypatch.setenv("LLM_ROUTER_PIN_TURN_MAX_FALLBACKS", "2")
    FakeClassifier(monkeypatch, tier="local")
    e = pinned_env(tmp_path, backend=failing())                       # turn 1: kill switch, a plain (soft) pin
    e.post(turn_body([P1, P2]))                                       # soft pin waived, flips, fails -> turn pin (1)
    e.post(turn_body([P1, P2, P3]))                                   # turn pin lifted, local fails -> sticky (2)
    rows = e.rows()
    assert [r["local_mode"].get("pin_scope") for r in rows] == [None, "turn", "sticky"]
    e.post(turn_body([P1, P2, P3, P4]))
    assert e.rows()[-1]["local_mode"]["reason"] == "pinned_sticky" and len(e.backend.calls) == 2


def test_turn_pin_and_failed_turn_mark_agree_inside_the_turn(tmp_path, monkeypatch):
    flags(monkeypatch, authority=True)
    monkeypatch.setenv("LLM_ROUTER_PIN_SCOPE", "turn")
    fake = FakeClassifier(monkeypatch, tier="local")
    e = pinned_env(tmp_path, backend=failing())
    e.post(turn_body([P1, P2]))                                       # flips, fails: mark_failed + turn pin
    e.post(turn_body([P1, P2], loops=1))                              # same turn
    flip, cont = e.rows()[-2:]
    assert flip["cls_applied"] is True and flip["local_mode"]["pin_scope"] == "turn"
    assert cont["local_mode"]["reason"] == "pinned_turn" and cont["cls_applied"] is False
    assert len(e.backend.calls) == 1 and len(fake.calls) == 1         # no second flip, no second verdict
    e.post(turn_body([P1, P2, P3]))                                   # next turn: pin lifted, local is tried
    assert e.rows()[-1]["local_mode"]["pin_cleared"] == "new_turn" and len(e.backend.calls) == 2


def test_a_plain_turn_pin_is_not_overridden_inside_its_turn_even_without_a_failed_mark(tmp_path, monkeypatch):
    """A step that was local from the start (no flip) fails: there is no failed-turn mark, yet the verdict that is
    cached for the turn must not retry local inside it."""
    flags(monkeypatch, authority=True)
    monkeypatch.setenv("LLM_ROUTER_PIN_SCOPE", "turn")
    FakeClassifier(monkeypatch, tier="local")
    e = Env(tmp_path)
    e.app = e.build(failing())
    e.post(turn_body([P1]))                                           # no pin yet: local, fails
    e.post(turn_body([P1], loops=1))
    cont = e.rows()[-1]
    assert cont["local_mode"]["reason"] == "pinned_turn" and cont["cls_applied"] is False and len(e.backend.calls) == 1


# -- order of the checks ------------------------------------------------------------------


@pytest.mark.parametrize("pin_reason", [local_mode.REASON_PINNED_TURN, local_mode.REASON_PINNED_STICKY])
def test_order_kill_marker_exempt_then_pin(monkeypatch, pin_reason):
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_MARKER", "on")
    marked = turn_body([f"hi {local_mode.CLAUDE_MARKER}"])
    plain = turn_body([P1])

    def decide(body, **kw):
        s = local_mode.LocalSession(pinned=local_mode.PIN_CLAUDE, pin_reason=pin_reason, cls_local=True, **kw)
        return local_mode.decide_local(body, s).reason

    assert decide(marked, kill_switch=True) == "kill_switch"
    assert decide(marked) == "claude_marker"                          # the marker beats the pin
    assert decide(plain, exempt=local_mode.EXEMPT_COORDINATOR) == "exempt_session"
    assert decide(plain) == pin_reason                                # cls_local does not waive it
    soft = local_mode.LocalSession(pinned=local_mode.PIN_CLAUDE, pin_reason=None, cls_local=True)
    assert local_mode.decide_local(plain, soft).local is True         # the plain pin is still the one soft rule
