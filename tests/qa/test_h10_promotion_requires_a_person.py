"""H-10 — the human-in-the-loop requirement was weaker than documented.

    approve()    rejected exactly one string: `actor == "assistant"`.
                 `--by bot`, `--by ci`, `--by x` all passed.
    activate()   the APPROVED -> ACTIVE transition, checked NOTHING at all.

`activate` is the transition that matters: it is what makes `run_matrix` execute
a verifier, so its verdicts become Ground Truth labels. An unguarded `activate`
made `approve`'s guard decorative — anything APPROVED could be promoted by any
caller, so an autonomous agent scripting the CLI with any other `--by` value
could promote its own verifiers to ACTIVE.

That matters more here than it would elsewhere. The subsystem's own design says a
verifier can be demonstrably discriminating and still check the wrong thing, and
that only a human catches that. The gate enforcing it is therefore load-bearing
for the whole "zero-human operation is unsafe" conclusion.

**What this check is not.** It is not authentication. Nothing offline can prove a
human typed a string, and pretending otherwise would be worse than the gap it
replaces — it would move an honest weakness into a false assurance. It is a
guardrail sized to the real failure mode: an agent or CI job driving the CLI with
no person in the loop. A determined human can still type a fake name; that is
their decision rather than one the system made for them.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def reg():
    sys.path.insert(0, str(ROOT / "scripts"))
    from groundtruth import verifier_registry as vr

    return vr


AUTOMATED = [
    "assistant", "bot", "claude-code", "gpt-4o", "ci", "ci-runner",
    "automation-runner", "system", "cron", "pipeline", "workflow",
    "auto-approver", "test-user", "unknown", "", "   ", "x", "12345",
]

HUMANS = ["Ada Lovelace", "grace.hopper", "j.smith", "kwame-osei", "Mei Tanaka"]


@pytest.mark.parametrize("actor", AUTOMATED)
def test_automated_actors_are_refused(reg, actor):
    ok, why = reg.require_human_actor(actor)
    assert ok is False, f"{actor!r} was accepted as a person"
    assert why, "refusal must say why"


@pytest.mark.parametrize("actor", HUMANS)
def test_plausible_people_are_accepted(reg, actor):
    """Anti-over-correction. A gate nobody can pass is not a gate."""
    ok, why = reg.require_human_actor(actor)
    assert ok is True, f"{actor!r} was refused: {why}"


def test_the_denylist_is_not_vacuous(reg):
    """The check must be capable of both answers, over a non-trivial set."""
    assert len(AUTOMATED) >= 10 and len(HUMANS) >= 4
    assert reg.require_human_actor("assistant")[0] is False
    assert reg.require_human_actor("Grace Hopper")[0] is True


def _validated(reg):
    """A verifier sitting at VALIDATED, ready to be approved."""
    v = reg.VerifierRecord(task_id="gt-0001")
    v.status = reg.VALIDATED
    return v


def test_approve_refuses_an_automated_actor(reg):
    v = _validated(reg)
    ok, why = v.approve("bot")
    assert ok is False and v.status == reg.VALIDATED, why


def test_approve_accepts_a_person(reg):
    v = _validated(reg)
    ok, why = v.approve("Ada Lovelace", "reviewed the contract")
    assert ok is True, why
    assert v.status == reg.APPROVED
    assert v.approved_by == "Ada Lovelace"


def test_activate_refuses_an_automated_actor(reg):
    """The transition that was completely unguarded.

    Approving as a person and then activating as a bot must not work — that is
    exactly the shape an agent would reach for.
    """
    v = _validated(reg)
    assert v.approve("Ada Lovelace")[0] is True

    ok, why = v.activate("assistant")
    assert ok is False, "an automated actor activated an approved verifier"
    assert v.status == reg.APPROVED, "status changed despite the refusal"
    assert why


def test_activate_records_who_did_it(reg):
    """Auditability: `approved_by` existed, `activated_by` did not."""
    v = _validated(reg)
    v.approve("Ada Lovelace")
    ok, why = v.activate("Grace Hopper")
    assert ok is True, why
    assert v.status == reg.ACTIVE
    assert v.activated_by == "Grace Hopper"


def test_approval_still_requires_evidence_first(reg):
    """The pre-existing guard must survive: no shortcut from PROPOSED."""
    v = reg.VerifierRecord(task_id="gt-0002")
    v.status = reg.PROPOSED
    ok, why = v.approve("Ada Lovelace")
    assert ok is False and "validated" in why.lower(), why


def test_both_transitions_share_one_definition(reg):
    """Pin the mechanism: two copies of the rule is how they diverged."""
    src = (ROOT / "scripts" / "groundtruth" / "verifier_registry.py").read_text(encoding="utf-8")
    assert src.count("require_human_actor(") >= 3, (
        "approve and activate must both call the shared guard (plus its "
        "definition); a second inline check would drift"
    )
