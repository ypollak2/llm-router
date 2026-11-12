"""PR-B: the local classifier may decide local, behind three flags that default OFF.

SINGLE_MODEL lets the one configured local model be the decision model; ELIGIBLE_SHADOW records a verdict on every
call the input rules allow (or refuse only for a pin / exempt session); AUTHORITY=positive lets a decided ``local``
verdict waive the one SOFT rule (a plain conversation pin). Hard limits always win; flag-off rows are unchanged.

No Ollama, no network: the classifier is injected, Anthropic is a mocked upstream, the serving model a fake backend.
"""
# ruff: noqa: F811  (fixtures are imported and used as arguments)

from __future__ import annotations

import asyncio

import pytest

from llm_router import local_classifier as lc
from llm_router.proxy import cls_decide, ledger, local_mode
from tests.test_proxy_cls_decide import (  # noqa: F401  (fixtures and helpers)
    Env, FakeClassifier, _env, env, turn_body, verdict,
)
from tests.test_proxy_local_mode import Breaker, _overrides_readable, _with_image  # noqa: F401

ELIG = ("cls_elig_verdict", "cls_elig_ms", "cls_elig_ok", "cls_elig_source")


def flags(monkeypatch, *, shadow=False, authority=False, budget=None):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    if shadow:
        monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_ELIGIBLE_SHADOW", "1")
    if authority:
        monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_AUTHORITY", "positive")
    if budget is not None:
        monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_BUDGET_S", str(budget))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("LLM_ROUTER_CLASSIFIER_SINGLE_MODEL", "LLM_ROUTER_CLASSIFIER_ELIGIBLE_SHADOW",
              "LLM_ROUTER_CLASSIFIER_AUTHORITY", "LLM_ROUTER_CLASSIFIER_BUDGET_S"):
        monkeypatch.delenv(k, raising=False)


def pinned_env(tmp_path, first=None, backend=None):
    """Turn 1 refused by the kill switch (a plain, non-structural pin to Claude); the caller posts turn 2."""
    e = Env(tmp_path)
    if backend is not None:
        e.app = e.build(backend)
    e.kill.write_text("x")
    e.post(first or turn_body(["rename the helper"]))
    e.kill.unlink()
    return e


def second(e, body=None):
    e.post(body or turn_body(["rename the helper", "and the other"]))
    return e.rows()[-1]


# -- flags off ----------------------------------------------------------------------


def test_flags_off_no_fields_no_call_pin_holds(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    fake = FakeClassifier(monkeypatch, tier="local")
    row = second(pinned_env(tmp_path))
    assert row["local_mode"]["reason"] == "pinned_claude" and row["cls_applied"] is False
    assert row["cls_source"] == "rules" and not any(k in row for k in ELIG)
    assert "cls_authority" not in row["local_mode"] and fake.calls == []


def test_authority_value_other_than_positive_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_AUTHORITY", "negative")
    fake = FakeClassifier(monkeypatch, tier="local")
    assert second(pinned_env(tmp_path))["local_mode"]["reason"] == "pinned_claude" and fake.calls == []


# -- ELIGIBLE_SHADOW alone: records, never changes serving ----------------------------


def test_shadow_records_on_a_call_the_rules_allow_and_serves_as_before(env, monkeypatch):
    flags(monkeypatch, shadow=True)
    FakeClassifier(monkeypatch, tier="local")
    env.post(turn_body(["rename the helper"]))
    (row,) = env.rows()
    assert row["decision"] == ledger.DECISION_SERVED and row["cls_applied"] is False and row["cls_source"] == "rules"
    assert (row["cls_elig_verdict"], row["cls_elig_ok"], row["cls_elig_source"]) == ("local", True, "llm")
    assert isinstance(row["cls_elig_ms"], float)


def test_shadow_continuations_inherit_with_no_second_call(env, monkeypatch):
    flags(monkeypatch, shadow=True)
    fake = FakeClassifier(monkeypatch, tier="local")
    env.post(turn_body(["rename the helper"]))
    env.post(turn_body(["rename the helper"], loops=1))
    first, cont = env.rows()
    assert len(fake.calls) == 1 and cont["cls_elig_source"] == "inherit" and cont["cls_elig_verdict"] == "local"
    assert first["cls_elig_source"] == "llm"


def test_shadow_on_a_pinned_call_records_but_the_pin_holds(tmp_path, monkeypatch):
    flags(monkeypatch, shadow=True)
    FakeClassifier(monkeypatch, tier="local")
    row = second(pinned_env(tmp_path))
    assert row["local_mode"]["reason"] == "pinned_claude" and row["cls_applied"] is False
    assert row["cls_elig_verdict"] == "local" and row["decision"] == ledger.DECISION_FORWARDED


def test_shadow_skips_a_call_a_hard_rule_refuses(tmp_path, monkeypatch):
    flags(monkeypatch, shadow=True)
    fake = FakeClassifier(monkeypatch, tier="local")
    e = Env(tmp_path)
    e.post(_with_image(turn_body(["what is in this image?"])))
    (row,) = e.rows()
    assert row["local_mode"]["reason"] == "media_present" and not any(k in row for k in ELIG) and fake.calls == []


def test_shadow_timeout_records_it_and_proceeds_with_the_rules(env, monkeypatch):
    flags(monkeypatch, shadow=True, budget=0.2)

    async def hang(*a, **k):
        await asyncio.sleep(60)

    monkeypatch.setattr(lc, "classify_async", hang)
    env.post(turn_body(["rename the helper"]))
    (row,) = env.rows()
    assert (row["cls_elig_source"], row["cls_elig_ok"], row["cls_elig_verdict"]) == ("timeout", False, None)
    assert row["decision"] == ledger.DECISION_SERVED and row["cls_applied"] is False


def test_shadow_failopen_source_is_recorded_not_ok(env, monkeypatch):
    flags(monkeypatch, shadow=True)
    FakeClassifier(monkeypatch, source="parse_error")
    env.post(turn_body(["rename the helper"]))
    (row,) = env.rows()
    assert (row["cls_elig_source"], row["cls_elig_ok"], row["cls_elig_verdict"]) == ("parse_error", False, None)


def test_budget_default_is_the_classifiers_own_and_env_overrides(monkeypatch):
    assert cls_decide.budget_s() == lc._timeout_s() == 2.0
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_BUDGET_S", "0.5")
    assert cls_decide.budget_s() == 0.5
    for bad in ("x", "0", "-1"):
        monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_BUDGET_S", bad)
        assert cls_decide.budget_s() == 2.0


# -- AUTHORITY=positive ------------------------------------------------------------------


def test_positive_local_verdict_waives_a_plain_pin_and_serves_local(tmp_path, monkeypatch):
    flags(monkeypatch, authority=True)
    FakeClassifier(monkeypatch, tier="local")
    e = pinned_env(tmp_path)
    row = second(e)
    assert row["decision"] == ledger.DECISION_SERVED
    assert row["cls_applied"] is True and row["cls_source"] == "llm"
    assert row["local_mode"]["local"] is True and row["local_mode"]["cls_authority"] == "pin_waived"
    assert not any(k in row for k in ELIG)                  # shadow flag off: no shadow fields
    assert len(e.up.requests) == 1 and len(e.backend.calls) == 1


@pytest.mark.parametrize("tier", ["haiku", "sonnet", "opus"])
def test_positive_non_local_verdict_leaves_the_pin(tmp_path, monkeypatch, tier):
    flags(monkeypatch, authority=True)
    FakeClassifier(monkeypatch, tier=tier)
    row = second(pinned_env(tmp_path))
    assert row["local_mode"]["reason"] == "pinned_claude" and row["cls_applied"] is False


@pytest.mark.parametrize("source", ["timeout", "parse_error", "abstain", "off"])
def test_positive_undecided_verdict_proceeds_with_the_rules(tmp_path, monkeypatch, source):
    flags(monkeypatch, authority=True)
    FakeClassifier(monkeypatch, source=source)
    row = second(pinned_env(tmp_path))
    assert row["local_mode"]["reason"] == "pinned_claude" and row["cls_applied"] is False


def test_positive_a_failing_proxy_local_lane_blocks_the_flip(tmp_path, monkeypatch):
    flags(monkeypatch, authority=True)
    FakeClassifier(monkeypatch, tier="local")
    monkeypatch.setattr(cls_decide, "proxy_lane_fails", lambda: True)
    row = second(pinned_env(tmp_path))
    assert row["local_mode"]["reason"] == "pinned_claude" and row["cls_applied"] is False


def test_positive_with_the_shadow_flag_records_both(tmp_path, monkeypatch):
    flags(monkeypatch, shadow=True, authority=True)
    FakeClassifier(monkeypatch, tier="local")
    row = second(pinned_env(tmp_path))
    assert row["cls_applied"] is True and row["cls_elig_verdict"] == "local" and row["cls_elig_ok"] is True


# -- hard limits always win ---------------------------------------------------------------


def _kill_then(tmp_path, monkeypatch, mutate):
    """Turn 1 pins (kill switch); `mutate(e)` sets a hard limit; turn 2 carries a ``local`` verdict."""
    flags(monkeypatch, shadow=True, authority=True)
    fake = FakeClassifier(monkeypatch, tier="local")
    e = pinned_env(tmp_path)
    mutate(e)
    return e, second(e), fake


def test_hard_kill_switch_wins(tmp_path, monkeypatch):
    e, row, fake = _kill_then(tmp_path, monkeypatch, lambda e: e.kill.write_text("x"))
    assert row["local_mode"]["reason"] == "kill_switch" and row["cls_applied"] is False and fake.calls == []
    assert e.backend.calls == []


def test_hard_exempt_session_wins_and_asks_for_no_verdict(tmp_path, monkeypatch):
    flags(monkeypatch, shadow=True, authority=True)
    fake = FakeClassifier(monkeypatch, tier="local")
    e = pinned_env(tmp_path)
    monkeypatch.setattr(local_mode, "exempt_reason", lambda *a, **k: (local_mode.EXEMPT_COORDINATOR, None))
    row = second(e)
    assert row["local_mode"]["reason"] == "exempt_session" and row["cls_applied"] is False
    assert fake.calls == [] and "cls_elig_verdict" not in row and e.backend.calls == []   # a verdict could not be used


def test_hard_media_wins(tmp_path, monkeypatch):
    flags(monkeypatch, shadow=True, authority=True)
    fake = FakeClassifier(monkeypatch, tier="local")
    e = pinned_env(tmp_path, _with_image(turn_body(["what is in this image?"])))   # a plain pin (kill switch)
    row = second(e, _with_image(turn_body(["what is in this image?", "and now"])))
    assert row["local_mode"]["reason"] == "pinned_claude" and row["cls_applied"] is False
    assert fake.calls == [] and e.backend.calls == []       # media is a hard rule: not even eligible


def test_hard_prompt_over_cap_wins(tmp_path, monkeypatch):
    flags(monkeypatch, shadow=True, authority=True)
    fake = FakeClassifier(monkeypatch, tier="local")
    e = pinned_env(tmp_path)
    monkeypatch.setattr(local_mode, "PROMPT_CAP_TOKENS", 5)
    row = second(e)
    assert row["local_mode"]["reason"] == "pinned_claude" and row["cls_applied"] is False
    assert fake.calls == [] and e.backend.calls == []


def test_hard_breaker_open_wins(tmp_path, monkeypatch):
    flags(monkeypatch, shadow=True, authority=True)
    FakeClassifier(monkeypatch, tier="local")
    e = pinned_env(tmp_path)
    import httpx
    from llm_router.proxy import server as ps
    from tests.test_proxy_local_mode import MODEL
    cfg = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=e.ledger, kill_switch=e.kill, model=MODEL,
                         trim="none", serve="local-agent", hedge_s=None, warm_up=False)
    app = ps.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(e.up)),
                       backend_factory=lambda m: e.backend, breaker_fn=Breaker(allowed=False))
    # a fresh app has no pins, so this call reaches the breaker rule itself with a ``local`` verdict in hand
    from tests.test_proxy_local_mode import _post
    asyncio.run(_post(app, turn_body(["rename the helper", "and the other"])))
    row = e.rows()[-1]
    assert row["cls_applied"] is False and row["decision"] != ledger.DECISION_SERVED and e.backend.calls == []


def test_a_claude_verdict_still_vetoes_as_today(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_BACKEND", "systemone")
    monkeypatch.setenv("LLM_ROUTER_DECISION_MODEL", "nimble:9b")
    flags(monkeypatch, authority=True)
    FakeClassifier(monkeypatch, tier="opus")
    env.post(turn_body(["design a multi-file change"]))
    (row,) = env.rows()
    assert row["reason"] == "classifier_veto" and row["cls_applied"] is True and row["cls_source"] == "llm"


def test_structural_pin_is_not_waived(tmp_path, monkeypatch):
    flags(monkeypatch, authority=True)
    FakeClassifier(monkeypatch, tier="local")
    e = Env(tmp_path)
    e.post(_with_image(turn_body(["what is in this image?"])))
    row = second(e, _with_image(turn_body(["what is in this image?", "and now"])))
    assert row["local_mode"]["reason"] == "pinned_claude" and row["cls_applied"] is False and e.backend.calls == []


def test_decide_local_cls_local_waives_only_the_pin():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    S = local_mode.LocalSession
    assert local_mode.decide_local(body, S(pinned="claude")).reason == "pinned_claude"
    assert local_mode.decide_local(body, S(pinned="claude", cls_local=True)).local is True
    for kw, reason in (({"kill_switch": True}, "kill_switch"), ({"exempt": "x"}, "exempt_session"),
                       ({"backend_healthy": False}, "backend_unhealthy"), ({"breaker_closed": False}, "breaker_open"),
                       ({"verdict": "veto_tier"}, "classifier_veto")):
        assert local_mode.decide_local(body, S(pinned="claude", cls_local=True, **kw)).reason == reason


# -- SINGLE_MODEL ---------------------------------------------------------------------


def test_single_model_flag_makes_the_configured_local_model_the_decision_model(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", "qwen3-coder:30b")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    assert cls_decide.active() is False                      # today: LOCAL_MODEL set means no decision model
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_SINGLE_MODEL", "1")
    assert cls_decide.active() is True
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    assert cls_decide.active() is False                      # explicit `on` only
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    monkeypatch.delenv("LLM_ROUTER_LOCAL_MODEL")
    assert cls_decide.active() is False                      # no single model configured


def test_single_model_unset_leaves_active_unchanged(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", "qwen3-coder:30b")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    assert cls_decide.active() is False
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_BACKEND", "systemone")
    monkeypatch.setenv("LLM_ROUTER_DECISION_MODEL", "nimble:9b")
    monkeypatch.delenv("LLM_ROUTER_LOCAL_MODEL")
    assert cls_decide.active() is True


# -- review follow-ups ---------------------------------------------------------------------


def test_a_failed_flipped_step_is_not_retried_local_on_the_next_continuation(tmp_path, monkeypatch):
    from tests.test_proxy_local_mode import Backend, _ollama

    flags(monkeypatch, authority=True)
    fake = FakeClassifier(monkeypatch, tier="local")
    e = pinned_env(tmp_path, backend=Backend(_ollama("", [{"function": {"name": "Nope", "arguments": {}}}])))
    e.post(turn_body(["rename the helper", "and the other"]))                                # flips, fails, pins claude
    e.post(turn_body(["rename the helper", "and the other"], loops=1))                       # same human turn
    flip, cont = e.rows()[-2:]
    assert flip["cls_applied"] is True and flip["decision"] == ledger.DECISION_FALLBACK
    assert cont["local_mode"]["reason"] == "pinned_claude" and cont["cls_applied"] is False
    assert len(e.backend.calls) == 1                                                         # not retried
    # a NEW human turn is classified afresh (and may flip again)
    e.post(turn_body(["rename the helper", "and the other", "third"]))
    assert len(fake.calls) == 2 and e.rows()[-1]["cls_applied"] is True


def test_authority_only_makes_no_call_on_an_already_local_call(env, monkeypatch):
    flags(monkeypatch, authority=True)
    fake = FakeClassifier(monkeypatch, tier="local")
    env.post(turn_body(["rename the helper"]))
    assert env.rows()[-1]["decision"] == ledger.DECISION_SERVED and fake.calls == []


def test_no_call_for_an_exempt_session(tmp_path, monkeypatch):
    flags(monkeypatch, shadow=True, authority=True)
    fake = FakeClassifier(monkeypatch, tier="local")
    monkeypatch.setattr(local_mode, "exempt_reason", lambda *a, **k: (local_mode.EXEMPT_COORDINATOR, None))
    e = Env(tmp_path)
    e.post(turn_body(["rename the helper"]))
    row = e.rows()[-1]
    assert row["local_mode"]["reason"] == "exempt_session" and fake.calls == [] and "cls_elig_source" not in row


def test_assemble_runs_inside_the_budget(env, monkeypatch):
    import time as _t
    flags(monkeypatch, shadow=True, budget=0.2)
    FakeClassifier(monkeypatch, tier="local")

    def slow(*a, **k):
        _t.sleep(1.0)

    monkeypatch.setattr(cls_decide, "assemble", slow)
    env.post(turn_body(["rename the helper"]))
    assert env.rows()[-1]["cls_elig_source"] == "timeout"


_MARKER = getattr(local_mode, "REASON_CLAUDE_MARKER", None)


@pytest.mark.skipif(_MARKER is None or not hasattr(local_mode, "CLAUDE_MARKER"),
                    reason="PR #914 (claude marker) not on this base; activates when it lands")
def test_claude_marker_wins_over_a_local_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_MARKER", "on")
    flags(monkeypatch, shadow=True, authority=True)
    FakeClassifier(monkeypatch, tier="local")
    first = f"rename the helper {local_mode.CLAUDE_MARKER}"        # the marker must be in the FIRST user message
    e = pinned_env(tmp_path, turn_body([first]))
    row = second(e, turn_body([first, "and the other"]))
    assert row["local_mode"]["reason"] == "pinned_claude" and row["cls_applied"] is False
    assert e.backend.calls == []
