"""North Star P2-S3 (audit R3 — verification gaming): the planner must reject
TRIVIAL acceptance checks.

A weak planner (a cheap model) writes its own acceptance checks. If it emits a
check that passes without any real work — `echo done`, a generic canary marker,
or a diff check asserting nothing — a milestone freezes as DONE for free and the
whole 'objective verification' guarantee is hollow. build_acceptance() fails
closed (PlanRejected) on those.
"""
from __future__ import annotations

import pytest

from llm_router.agentic.planner import PlanRejected, build_acceptance, plan_to_milestones


@pytest.mark.parametrize("spec", [
    {"type": "cmd", "command": ["echo", "done"]},
    {"type": "cmd", "command": ["true"]},
    {"type": "cmd", "command": ["exit", "0"]},
    {"type": "cmd", "command": []},
    {"type": "cmd"},                                     # no command at all
    {"type": "canary", "marker": "ok"},                  # too short
    {"type": "canary", "marker": "done"},                # generic
    {"type": "canary", "marker": "success"},             # generic
    {"type": "diff"},                                    # asserts nothing
    {"type": "diff", "files": [], "symbols": []},
])
def test_trivial_checks_are_rejected(spec):
    with pytest.raises(PlanRejected):
        build_acceptance(spec)


@pytest.mark.parametrize("spec", [
    {"type": "cmd", "command": ["pytest", "-q"]},
    {"type": "cmd", "command": ["sh", "-c", "grep -q FOO out.txt"]},
    {"type": "canary", "marker": "PROVIDER_CODEX_CANARY"},
    {"type": "diff", "files": ["x.py"]},
    {"type": "diff", "symbols": ["def foo"]},
])
def test_real_checks_are_accepted(spec):
    build_acceptance(spec)  # must not raise


def test_plan_with_a_trivial_check_is_rejected():
    plan = [{"id": "M1", "description": "impl",
             "acceptance": {"type": "cmd", "command": ["echo", "hi"]}}]
    with pytest.raises(PlanRejected):
        plan_to_milestones(plan)
