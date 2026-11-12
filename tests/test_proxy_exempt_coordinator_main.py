"""PR-D: ``LLM_ROUTER_LOCAL_EXEMPT=coordinator_main`` keeps only a coordinator session's MAIN thread on Claude.

Parsing, the decision on ``exempt_reason`` (main / sub-agent / organic / unknown thread / unreadable overrides
file), the marker still winning in ``decide_local``, and the default staying unchanged. No Ollama, no network.
"""

from __future__ import annotations

import pytest

from llm_router.proxy import local_mode, steps
from tests.test_proxy_local_exempt_policy import _body  # noqa: F401
from tests.test_proxy_local_with_tiers import (  # noqa: F401  (autouse fixtures and helpers)
    SID, clean_overrides, write_overrides,
)

ENV = local_mode.EXEMPT_POLICY_ENV
NEW = local_mode.EXEMPT_COORDINATOR_MAIN


@pytest.fixture
def overrides(clean_overrides):  # noqa: F811
    return clean_overrides


@pytest.mark.parametrize("raw,expected", [
    ("coordinator_main", ("coordinator_main", False, False, None)),
    (" Coordinator_Main ", ("coordinator_main", False, False, None)),
    ("coordinator_main,subagent", ("coordinator_main,subagent", False, True, None)),
    ("subagent,coordinator_main", ("coordinator_main,subagent", False, True, None)),
    # unchanged values
    ("coordinator", ("coordinator", True, False, None)),
    ("none", ("none", False, False, None)),
    (None, ("coordinator,subagent", True, True, None)),
    # invalid: the default applies and the value is recorded
    ("coordinator,coordinator_main", ("coordinator,subagent", True, True, "coordinator,coordinator_main")),
    ("coordinator,coordinator_main,subagent",
     ("coordinator,subagent", True, True, "coordinator,coordinator_main,subagent")),
    ("coordinator_main,", ("coordinator,subagent", True, True, "coordinator_main,")),
    ("none,coordinator_main", ("coordinator,subagent", True, True, "none,coordinator_main")),
    ("coordinator-main", ("coordinator,subagent", True, True, "coordinator-main")),
])
def test_parse(monkeypatch, raw, expected):
    monkeypatch.delenv(ENV, raising=False)
    assert local_mode.exempt_policy(raw) == expected


def test_the_tuple_shape_is_unchanged():
    for raw in (None, "none", "coordinator", "subagent", "coordinator_main", "coordinator_main,subagent", "x"):
        assert len(local_mode.exempt_policy(raw)) == 4


def test_the_new_reason_is_distinct():
    assert NEW == "exempt_coordinator_main"
    assert NEW not in (local_mode.EXEMPT_COORDINATOR, local_mode.EXEMPT_SUBAGENT)


def _coord(monkeypatch, policy="coordinator_main"):
    monkeypatch.setenv(ENV, policy)
    write_overrides({SID: {"kind": "research", "reason": "coordinator"}})


def test_main_call_of_a_coordinator_session_is_exempt_with_the_new_reason(monkeypatch, overrides):
    _coord(monkeypatch)
    assert local_mode.exempt_reason(_body("organic"), SID) == (NEW, None)


def test_sub_agent_call_of_a_coordinator_session_is_not_exempt(monkeypatch, overrides):
    _coord(monkeypatch)
    assert local_mode.exempt_reason(_body("subagent"), SID) == (None, None)
    body = _body("organic")
    body["tools"] = [t for t in body["tools"] if t.get("name") != "Agent"]   # no launcher: not the main thread
    assert steps.is_main_thread(body) is False
    assert local_mode.exempt_reason(body, SID) == (None, None)


def test_with_subagent_the_coordinators_sub_agent_call_takes_the_subagent_reason(monkeypatch, overrides):
    _coord(monkeypatch, "coordinator_main,subagent")
    assert local_mode.exempt_reason(_body("organic"), SID)[0] == NEW
    assert local_mode.exempt_reason(_body("subagent"), SID)[0] == "subagent_thread"


def test_an_organic_session_is_not_exempt(monkeypatch, overrides):
    monkeypatch.setenv(ENV, "coordinator_main")
    write_overrides({})
    assert local_mode.exempt_reason(_body("organic"), "other-session") == (None, None)


def test_an_unknown_thread_counts_as_main_and_stays_exempt(monkeypatch, overrides):
    _coord(monkeypatch)
    monkeypatch.setattr(steps, "is_main_thread", lambda body, parent_main=None: None)
    assert local_mode.exempt_reason(_body("coordinator"), SID) == (NEW, None)


def test_an_evidence_error_counts_as_main_and_stays_exempt(monkeypatch, overrides):
    _coord(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(steps, "is_subagent_request", boom)
    assert local_mode.exempt_reason(_body("subagent"), SID) == (NEW, None)


@pytest.mark.parametrize("policy", ["coordinator", "coordinator_main"])
@pytest.mark.parametrize("how", ["missing", "unreadable"])
def test_an_unreadable_overrides_file_fails_closed_like_coordinator(monkeypatch, overrides, policy, how):
    monkeypatch.setenv(ENV, policy)
    if how == "missing":
        overrides.unlink()
    else:
        write_overrides("{not json")
    detail, problem = local_mode.exempt_reason(_body("organic"), "other-session")
    assert problem == how
    assert detail == ("missing" if how == "missing" else "overrides_unreadable")


def test_a_passed_policy_wins_over_the_env(monkeypatch, overrides):
    monkeypatch.setenv(ENV, "none")
    write_overrides({SID: {"kind": "research", "reason": "coordinator"}})
    policy = local_mode.exempt_policy("coordinator_main")
    assert local_mode.exempt_reason(_body("organic"), SID, None, policy) == (NEW, None)


def test_default_and_existing_values_are_unchanged(monkeypatch, overrides):
    write_overrides({SID: {"kind": "research", "reason": "coordinator"}})
    monkeypatch.delenv(ENV, raising=False)
    assert local_mode.exempt_reason(_body("organic"), SID)[0] == "coordinator_session"
    monkeypatch.setenv(ENV, "coordinator")
    assert local_mode.exempt_reason(_body("subagent"), SID)[0] == "coordinator_session"
    monkeypatch.setenv(ENV, "subagent")
    assert local_mode.exempt_reason(_body("organic"), SID) == (None, None)


def test_the_banner_names_the_policy(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV, "coordinator_main")
    text = "\n".join(local_mode.banner_lines(model="m", num_ctx=4096, kill_path=tmp_path / "k", killed=False))
    assert "exempt policy (LLM_ROUTER_LOCAL_EXEMPT): coordinator_main" in text
    assert "invalid value" not in text


def test_the_marker_still_wins_over_the_new_exemption(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_MARKER", "1")
    body = {"messages": [{"role": "user", "content": f"review this {local_mode.CLAUDE_MARKER}"}]}
    d = local_mode.decide_local(body, local_mode.LocalSession(exempt=NEW))
    assert d.local is False and d.reason == local_mode.REASON_CLAUDE_MARKER
    # the kill switch still comes first
    assert local_mode.decide_local(body, local_mode.LocalSession(exempt=NEW, kill_switch=True)).reason == "kill_switch"
