"""D-649: ``LLM_ROUTER_LOCAL_EXEMPT=main`` keeps EVERY session's main thread on Claude; sub-agents may go local.

Parsing, the decision on ``exempt_reason`` (main / sub-agent / unknown thread / evidence error), combinations,
old values unchanged, ledger detail an enum string. No Ollama, no network.
"""

from __future__ import annotations

import pytest

from llm_router.proxy import local_mode, steps
from llm_router.proxy.cache_cost import conversation_key
from tests.test_proxy_local_exempt_policy import _body  # noqa: F401
from tests.test_proxy_local_with_tiers import (  # noqa: F401  (autouse fixtures and helpers)
    SID, clean_overrides, write_overrides,
)

ENV = local_mode.EXEMPT_POLICY_ENV
NEW = local_mode.EXEMPT_MAIN


@pytest.fixture
def overrides(clean_overrides):  # noqa: F811
    return clean_overrides


@pytest.mark.parametrize("raw,expected", [
    ("main", ("main", False, False, None)),
    (" Main ", ("main", False, False, None)),
    ("main,subagent", ("main,subagent", False, True, None)),
    ("subagent,main", ("main,subagent", False, True, None)),
    ("coordinator,main", ("coordinator,main", True, False, None)),
    # redundant pair: allowed, main wins
    ("main,coordinator_main", ("main", False, False, None)),
    ("coordinator_main,main,subagent", ("main,subagent", False, True, None)),
    # unchanged values
    ("coordinator_main", ("coordinator_main", False, False, None)),
    ("coordinator", ("coordinator", True, False, None)),
    ("subagent", ("subagent", False, True, None)),
    ("none", ("none", False, False, None)),
    (None, ("coordinator,subagent", True, True, None)),
    # invalid: the default applies and the value is recorded
    ("none,main", ("coordinator,subagent", True, True, "none,main")),
    ("coordinator,coordinator_main,main", ("coordinator,subagent", True, True, "coordinator,coordinator_main,main")),
    ("mainthread", ("coordinator,subagent", True, True, "mainthread")),
])
def test_parse(monkeypatch, raw, expected):
    monkeypatch.delenv(ENV, raising=False)
    assert local_mode.exempt_policy(raw) == expected


def test_the_reason_is_a_distinct_enum_string():
    assert NEW == "exempt_main_thread" and isinstance(NEW, str)
    assert NEW not in (local_mode.EXEMPT_COORDINATOR, local_mode.EXEMPT_SUBAGENT, local_mode.EXEMPT_COORDINATOR_MAIN)


def test_main_thread_is_refused_even_with_no_overrides(monkeypatch, overrides):
    monkeypatch.setenv(ENV, "main")
    write_overrides({})
    assert local_mode.exempt_reason(_body("organic"), "other-session") == (NEW, None)
    overrides.unlink()   # no overrides file is read, so nothing fails closed
    assert local_mode.exempt_reason(_body("organic"), "other-session") == (NEW, None)


def test_sub_agent_call_is_served(monkeypatch, overrides):
    monkeypatch.setenv(ENV, "main")
    write_overrides({SID: {"kind": "research", "reason": "coordinator"}})
    assert local_mode.exempt_reason(_body("subagent"), SID) == (None, None)
    assert local_mode.exempt_reason(_body("subagent"), "other-session") == (None, None)


def test_with_subagent_the_sub_agent_call_is_refused_too(monkeypatch, overrides):
    monkeypatch.setenv(ENV, "main,subagent")
    write_overrides({})
    assert local_mode.exempt_reason(_body("organic"), "s")[0] == NEW
    assert local_mode.exempt_reason(_body("subagent"), "s")[0] == "subagent_thread"


def test_main_with_coordinator_still_refuses_a_coordinators_sub_agent(monkeypatch, overrides):
    monkeypatch.setenv(ENV, "coordinator,main")
    write_overrides({SID: {"kind": "research", "reason": "coordinator"}})
    assert local_mode.exempt_reason(_body("subagent"), SID)[0] == "coordinator_session"
    assert local_mode.exempt_reason(_body("organic"), "other-session")[0] == NEW


def test_main_wins_over_coordinator_main(monkeypatch, overrides):
    monkeypatch.setenv(ENV, "main,coordinator_main")
    write_overrides({SID: {"kind": "research", "reason": "coordinator"}})
    assert local_mode.exempt_reason(_body("organic"), SID)[0] == NEW
    assert local_mode.exempt_reason(_body("subagent"), SID) == (None, None)


def test_an_unknown_thread_counts_as_main_and_is_refused(monkeypatch, overrides):
    monkeypatch.setenv(ENV, "main")
    monkeypatch.setattr(steps, "is_main_thread", lambda body, parent_main=None: None)
    assert local_mode.exempt_reason(_body("coordinator"), "s") == (NEW, None)


def test_an_evidence_error_counts_as_main_and_is_refused(monkeypatch, overrides):
    monkeypatch.setenv(ENV, "main")

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(steps, "is_subagent_request", boom)
    assert local_mode.exempt_reason(_body("subagent"), "s") == (NEW, None)


def test_a_passed_policy_wins_over_the_env(monkeypatch, overrides):
    monkeypatch.setenv(ENV, "none")
    write_overrides({})
    assert local_mode.exempt_reason(_body("organic"), "s", None, local_mode.exempt_policy("main")) == (NEW, None)


def test_default_and_existing_values_are_unchanged(monkeypatch, overrides):
    write_overrides({SID: {"kind": "research", "reason": "coordinator"}})
    monkeypatch.delenv(ENV, raising=False)
    assert local_mode.exempt_reason(_body("organic"), SID)[0] == "coordinator_session"
    assert local_mode.exempt_reason(_body("organic"), "other") == (None, None)
    assert local_mode.exempt_reason(_body("subagent"), "other")[0] == "subagent_thread"
    monkeypatch.setenv(ENV, "coordinator_main")
    assert local_mode.exempt_reason(_body("organic"), SID)[0] == "exempt_coordinator_main"
    monkeypatch.setenv(ENV, "none")
    assert local_mode.exempt_reason(_body("organic"), SID) == (None, None)


def test_the_refusal_reaches_the_ledger_as_the_detail_only():
    d = local_mode.decide_local({"messages": [{"role": "user", "content": "hi"}]},
                                local_mode.LocalSession(exempt=NEW))
    assert d.local is False and d.reason == local_mode.REASON_EXEMPT_SESSION and d.detail == "exempt_main_thread"


def test_the_banner_names_the_policy(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV, "main")
    text = "\n".join(local_mode.banner_lines(model="m", num_ctx=4096, kill_path=tmp_path / "k", killed=False))
    assert "exempt policy (LLM_ROUTER_LOCAL_EXEMPT): main" in text and "invalid value" not in text


# Review S2 (coord/design/REVIEW-EXEMPT-MAIN-20261127.md): a main-thread refusal pins only the main thread's key.
def _pin_msg(body, text):
    body = dict(body)
    body["messages"] = [{"role": "user", "content": text}]
    return body


def _pin_run(monkeypatch, main_text, sub_text):
    monkeypatch.setenv(ENV, "main")
    lm = local_mode.LocalModeState()
    sid = "S"
    main, sub = _pin_msg(_body("organic"), main_text), _pin_msg(_body("subagent"), sub_text)
    km, ks = conversation_key(main, sid), conversation_key(sub, sid)
    ex_m, _ = local_mode.exempt_reason(main, sid)
    assert ex_m == NEW
    dm = local_mode.decide_local(main, local_mode.LocalSession(key=km, pinned=lm.pin_of(km), exempt=ex_m))
    assert not dm.local
    lm.set_pin(km, local_mode.PIN_CLAUDE, None)   # what server.py does on a first refusal
    ex_s, _ = local_mode.exempt_reason(sub, sid)
    assert ex_s is None
    return km, ks, local_mode.decide_local(sub, local_mode.LocalSession(
        key=ks, pinned=lm.pin_of(ks), exempt=ex_s, backend_healthy=True, breaker_closed=True,
        pin_reason=lm.pin_reason_of(ks)))


def test_distinct_first_messages_sub_agent_not_pinned(monkeypatch, overrides):
    km, ks, d = _pin_run(monkeypatch, "fix the bug", "Brief: look at tests")
    assert km != ks and d.reason != local_mode.REASON_PINNED_CLAUDE


def test_identical_first_message_shares_pin(monkeypatch, overrides):   # documents the residual (fork sub-agent)
    km, ks, d = _pin_run(monkeypatch, "same text", "same text")
    assert km == ks and d.reason == local_mode.REASON_PINNED_CLAUDE
