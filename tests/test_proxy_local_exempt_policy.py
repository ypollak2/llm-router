"""D-455: ``LLM_ROUTER_LOCAL_EXEMPT`` picks which exempt classes refuse local serving.

Matrix of policy x {coordinator session, sub-agent thread, organic main thread} x {overrides readable,
missing}, on ``exempt_reason`` and through the combined-mode proxy (ledger field, banner). KPI readers are
not touched. No Ollama, no network (helpers reused from test_proxy_local_with_tiers).
"""

from __future__ import annotations

import pytest

from llm_router import session_kind
from llm_router.proxy import local_mode
from tests.test_proxy_local_with_tiers import (  # noqa: F401  (autouse fixtures and helpers)
    SID, SUBAGENT_SYSTEM, Rig, _moderate, clean_overrides, opus, turn_body, write_overrides,
)

ENV = local_mode.EXEMPT_POLICY_ENV


@pytest.fixture
def overrides(clean_overrides):  # noqa: F811
    return clean_overrides


@pytest.mark.parametrize("raw,expected", [
    (None, ("coordinator,subagent", True, True, None)),
    ("", ("coordinator,subagent", True, True, None)),
    ("   ", ("coordinator,subagent", True, True, None)),
    ("coordinator,subagent", ("coordinator,subagent", True, True, None)),
    (" Subagent , COORDINATOR ", ("coordinator,subagent", True, True, None)),
    ("COORDINATOR", ("coordinator", True, False, None)),
    ("  subagent ", ("subagent", False, True, None)),
    ("None", ("none", False, False, None)),
    ("everything", ("coordinator,subagent", True, True, "everything")),
    ("none,subagent", ("coordinator,subagent", True, True, "none,subagent")),
    ("coordinator,", ("coordinator,subagent", True, True, "coordinator,")),
])
def test_parse(monkeypatch, raw, expected):
    monkeypatch.delenv(ENV, raising=False)
    assert local_mode.exempt_policy(raw) == expected


def test_parse_reads_the_env_per_call(monkeypatch):
    monkeypatch.setenv(ENV, "none")
    assert local_mode.exempt_policy()[0] == "none"
    monkeypatch.setenv(ENV, "subagent")
    assert local_mode.exempt_policy()[0] == "subagent"


def _body(kind):
    body = opus(turn_body(["look into the failing test"]))
    if kind == "subagent":
        body["system"] = list(body["system"]) + SUBAGENT_SYSTEM
    elif kind == "organic":
        body["tools"] = body["tools"] + [{"name": "Agent", "description": "launch",
                                          "input_schema": {"type": "object", "properties": {}}}]
    return body


#: policy -> {session kind: refused detail or None} while the overrides file is readable.
READABLE = {
    "coordinator,subagent": {"coordinator": "coordinator_session", "subagent": "subagent_thread", "organic": None},
    "coordinator": {"coordinator": "coordinator_session", "subagent": None, "organic": None},
    "subagent": {"coordinator": None, "subagent": "subagent_thread", "organic": None},
    "none": {"coordinator": None, "subagent": None, "organic": None},
}


@pytest.mark.parametrize("policy", list(READABLE))
@pytest.mark.parametrize("kind", ["coordinator", "subagent", "organic"])
@pytest.mark.parametrize("readable", [True, False])
def test_matrix(monkeypatch, overrides, policy, kind, readable):
    monkeypatch.setenv(ENV, policy)
    if readable:
        write_overrides({SID: {"kind": "research", "reason": "coordinator"}} if kind == "coordinator" else {})
    else:
        overrides.unlink()
    detail, problem = local_mode.exempt_reason(_body(kind), SID if kind == "coordinator" else "other-session")
    want = READABLE[policy][kind]
    if "coordinator" in policy.split(","):
        if not readable:   # fail closed while coordinators are exempt
            assert (detail, problem) == ("missing", "missing")
            return
    else:                  # the file is not needed for serving
        assert problem is None
    assert detail == want


@pytest.mark.parametrize("policy", ["subagent", "none"])
def test_an_unreadable_file_refuses_nothing_when_coordinators_are_not_exempt(monkeypatch, overrides, policy):
    monkeypatch.setenv(ENV, policy)
    write_overrides("{not json")
    assert local_mode.exempt_reason(_body("organic"), SID) == (None, None)


def test_an_invalid_value_gets_the_default(monkeypatch, overrides):
    monkeypatch.setenv(ENV, "bogus")
    overrides.unlink()
    assert local_mode.exempt_reason(_body("organic"), SID) == ("missing", "missing")
    write_overrides({SID: {"kind": "research", "reason": "c"}})
    assert local_mode.exempt_reason(_body("organic"), SID)[0] == "coordinator_session"


def test_the_ledger_records_the_effective_policy_on_a_served_row(tmp_path, monkeypatch, overrides):
    monkeypatch.setenv(ENV, " Subagent")
    overrides.unlink()                      # not needed with "subagent"
    rig = Rig(tmp_path)
    rig.post(turn_body(["fix the bug"]))
    (row,) = rig.rows()
    assert row["decision"] == "served" and row["local_mode"]["exempt_policy"] == "subagent"
    assert "overrides_unreadable" not in row["local_mode"] and "exempt_policy_invalid" not in row["local_mode"]


def test_a_coordinator_is_served_locally_when_not_exempt(tmp_path, monkeypatch, overrides):
    write_overrides({SID: {"kind": "research", "reason": "coordinator"}})
    monkeypatch.setenv(ENV, "subagent")
    rig = Rig(tmp_path)
    rig.post(turn_body(["fix the bug"]))
    (row,) = rig.rows()
    assert row["decision"] == "served" and len(rig.backend.calls) == 1


def test_a_refused_row_and_the_default_carry_the_policy(tmp_path, monkeypatch, overrides):
    monkeypatch.delenv(ENV, raising=False)
    write_overrides({SID: {"kind": "research", "reason": "coordinator"}})
    rig = Rig(tmp_path)
    rig.post(turn_body(["fix the bug"]))
    (row,) = rig.rows()
    assert row["reason"] == "exempt_session" and row["local_mode"]["detail"] == "coordinator_session"
    assert row["local_mode"]["exempt_policy"] == "coordinator,subagent"


def test_an_invalid_value_is_recorded_in_the_row(tmp_path, monkeypatch, overrides):
    monkeypatch.setenv(ENV, "bogus")
    rig = Rig(tmp_path)
    rig.post(turn_body(["fix the bug"]))
    (row,) = rig.rows()
    assert row["local_mode"]["exempt_policy"] == "coordinator,subagent"
    assert row["local_mode"]["exempt_policy_invalid"] == "bogus"


@pytest.mark.parametrize("raw,text", [(None, "coordinator,subagent"), ("none", ": none"), ("bogus", "invalid value 'bogus'")])
def test_the_banner_states_the_effective_policy(tmp_path, monkeypatch, raw, text):
    if raw is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, raw)
    lines = local_mode.banner_lines(model="ollama/m", num_ctx=None, kill_path=tmp_path / "k", killed=False)
    (line,) = [x for x in lines if "exempt policy" in x]
    assert ENV in line and text in line


def test_the_kpi_reader_ignores_the_policy(monkeypatch, overrides):
    write_overrides({SID: {"kind": "research", "reason": "coordinator"}})
    monkeypatch.setenv(ENV, "none")
    assert session_kind.override_of(SID) == "research"
