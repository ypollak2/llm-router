"""Verifier Authoring Assistant.

The assistant may propose how truth is measured. It must not be able to decide
that its own proposal is true. Most of this file exists to pin that boundary:

  * a proposal starts unproven and cannot approve itself
  * confidence comes from mutation evidence, never from a claim
  * a verifier that passes a broken implementation is UNUSABLE, not MEDIUM
  * only a human can APPROVE, and only after evidence

The adversarial cases are the important ones. A weak verifier that passes
everything is the failure mode that looks like success, so several tests below
construct exactly that and assert it is rejected.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from groundtruth import contract as ct  # noqa: E402
from groundtruth import dataset as ds  # noqa: E402
from groundtruth import mutants as mut  # noqa: E402
from groundtruth import pool as poolmod  # noqa: E402
from groundtruth import propose as pr  # noqa: E402
from groundtruth import verifier_registry as vreg  # noqa: E402
from groundtruth.eligibility import assess  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

CODE_TASK = ("Make the name_contains filter in src/query.py case-insensitive. "
             "The order of results must stay the order of the input rows.")
RETRY_TASK = ("Add a retry with exponential backoff to src/client.py for 5xx "
              "responses, leaving 4xx handling unchanged.")


# ── Part 2: acceptance contract ─────────────────────────────────────────────

def test_contract_separates_required_from_optional() -> None:
    c = ct.derive("t1", "Fix the off-by-one in src/pager.py. "
                        "The implementation should be concise and readable.")
    assert c.required
    assert any("concise" in o.text or "readable" in o.text for o in c.optional)


def test_optional_conditions_never_enter_pass() -> None:
    c = ct.derive("t1", "Fix the crash in src/a.py. Keep the code clean and readable.")
    texts = [x.text for x in c.pass_conditions()]
    assert not any("clean" in t or "readable" in t for t in texts), (
        "taste must not decide PASS")


def test_invariants_are_recognised() -> None:
    c = ct.derive("t1", RETRY_TASK)
    assert c.invariants
    assert c.template == "add-capability"


def test_vague_task_is_flagged_unclear_not_guessed() -> None:
    c = ct.derive("t1", "Improve the error handling appropriately in src/a.py")
    assert c.unclear
    assert not c.is_actionable


def test_contract_with_no_required_condition_is_not_actionable() -> None:
    c = ct.derive("t1", "hmm think about the thing")
    assert not c.is_actionable


def test_template_supplies_invariants_but_not_the_required_criteria() -> None:
    for t in ct.TEMPLATES:
        assert t.invariants, f"{t.name} supplies no invariants"
        assert t.needs_required, (
            f"{t.name} claims to supply required criteria — that would make it "
            "a 'tests pass = success' shortcut")


def test_existing_tests_become_an_invariant() -> None:
    c = ct.derive("t1", CODE_TASK, existing_tests=["tests/test_query.py"],
                  test_command="pytest tests/test_query.py")
    assert any("existing tests still pass" in i.text for i in c.invariants)


# ── Part 3/4: strategy selection and repo search ────────────────────────────

def _candidate(prompt: str, **over) -> poolmod.Candidate:
    e = assess(prompt, has_repo_state=True, envelope_complete=True)
    env = {"prompt": prompt, "complete": True,
           "repo": {"commit": "a" * 40, "dirty": False, "file_hashes": {}}}
    env.update(over.pop("envelope", {}))
    return poolmod.make_candidate(task_id=over.pop("task_id", "gtc-test"),
                                  prompt=prompt, prompt_sha256="h" * 16,
                                  eligibility=e, envelope=env, **over)


def test_repo_search_runs_and_explains_itself() -> None:
    p = pr.propose(_candidate(CODE_TASK), repo_root=REPO)
    assert p.existing_rationale, "a proposal must say why existing tests do or don't cover it"


def test_existing_tests_are_never_assumed_sufficient() -> None:
    p = pr.propose(_candidate(CODE_TASK), repo_root=REPO)
    assert p.existing_sufficient is False, (
        "nothing has shown an existing test asserts the NEW behaviour")


def test_unreconstructable_repo_blocks_a_test_strategy() -> None:
    c = _candidate(CODE_TASK, envelope={"repo": {"commit": None, "dirty": True}})
    p = pr.propose(c, repo_root=REPO)
    assert p.verification_strategy == pr.S_NONE
    assert any("reconstructable" in b for b in p.blockers)


def test_vague_task_falls_back_to_rubric_not_a_test() -> None:
    c = _candidate("Improve the error handling appropriately in src/a.py")
    p = pr.propose(c, repo_root=REPO)
    assert p.verification_strategy in (pr.S_RUBRIC, pr.S_NONE)
    assert p.blockers


def test_strategy_order_puts_judge_last() -> None:
    assert pr.STRATEGY_ORDER[0] == pr.S_EXISTING_TESTS
    assert pr.STRATEGY_ORDER[-2] == pr.S_JUDGE
    assert pr.S_JUDGE not in pr.MECHANICAL_STRATEGIES


def test_schema_task_selects_schema_validation() -> None:
    c = _candidate('Return a JSON object with keys "name" and "year" describing the repo')
    p = pr.propose(c, repo_root=REPO)
    assert p.verification_strategy == pr.S_SCHEMA
    assert p.verifier_snippet and "json_schema" in p.verifier_snippet


# ── Part 6: generation ──────────────────────────────────────────────────────

def test_generated_test_does_not_pass_on_arrival() -> None:
    """A generated test that passes immediately proves nothing."""
    c = _candidate(RETRY_TASK)
    p = pr.propose(c, repo_root=REPO)
    assert p.proposed_files
    body = p.proposed_files[0]["content"]
    assert "pytest.fail" in body, "placeholders must fail until a human fills them in"
    assert "PROPOSED" in body


def test_generated_test_records_the_contract_in_the_file() -> None:
    p = pr.propose(_candidate(RETRY_TASK), repo_root=REPO)
    body = p.proposed_files[0]["content"]
    assert "Acceptance contract" in body
    assert "NOT part of PASS" in body or "REQUIRED" in body


def test_generated_path_follows_repo_test_convention() -> None:
    p = pr.propose(_candidate(RETRY_TASK), repo_root=REPO)
    path = p.proposed_files[0]["path"]
    assert path.startswith("tests/") and Path(path).name.startswith("test_")


# ── Part 7: gaming guards ───────────────────────────────────────────────────

def test_proposal_carries_gaming_guards() -> None:
    p = pr.propose(_candidate(CODE_TASK), repo_root=REPO)
    joined = " ".join(p.gaming_guards)
    assert "trusted checkout" in joined or "no_tests_weakened" in joined
    assert "outside the tree the model can edit" in joined


# ── Part 8: mutation validation, including adversarial verifiers ────────────

GOOD_TARGET = '''
def clamp(value, lo, hi):
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value
'''

STRONG_TEST = '''
import sys
sys.path.insert(0, ".")
from target import clamp

def test_within():
    assert clamp(5, 0, 10) == 5

def test_below():
    assert clamp(-1, 0, 10) == 0

def test_above():
    assert clamp(99, 0, 10) == 10

def test_boundaries():
    assert clamp(0, 0, 10) == 0
    assert clamp(10, 0, 10) == 10
'''

WEAK_TEST = '''
import sys
sys.path.insert(0, ".")
import target

def test_module_imports():
    assert target is not None
'''


def test_strong_verifier_passes_baseline_and_kills_mutants() -> None:
    v = mut.validate_pytest_verifier(
        task_id="t-strong", test_source=STRONG_TEST,
        target_path="target.py", target_source=GOOD_TARGET)
    assert v.executable
    assert v.baseline_passed is True
    assert v.total > 0, "no mutation applied — nothing was demonstrated"
    assert v.detected > 0
    assert v.discriminates


def test_weak_verifier_is_unusable_not_medium() -> None:
    """The adversarial case: a test that passes everything.

    This is the failure mode that looks like success — it is green, it runs,
    and it certifies nothing.
    """
    v = mut.validate_pytest_verifier(
        task_id="t-weak", test_source=WEAK_TEST,
        target_path="target.py", target_source=GOOD_TARGET)
    assert v.baseline_passed is True
    assert v.detected == 0, "the weak test should survive every mutation"
    conf, why = mut.classify(v, strategy_is_mechanical=True, contract_complete=True)
    assert conf == mut.UNUSABLE
    assert "cannot detect failure" in why


def test_verifier_that_rejects_good_code_is_unusable() -> None:
    broken_test = 'def test_impossible():\n    assert False\n'
    v = mut.validate_pytest_verifier(
        task_id="t-bad", test_source=broken_test,
        target_path="target.py", target_source=GOOD_TARGET)
    assert v.baseline_passed is False
    conf, why = mut.classify(v, strategy_is_mechanical=True, contract_complete=True)
    assert conf == mut.UNUSABLE
    assert "known-good" in why


def test_no_baseline_run_is_unusable() -> None:
    v = mut.Validation(task_id="t", executable=True, baseline_passed=None)
    conf, _ = mut.classify(v, strategy_is_mechanical=True, contract_complete=True)
    assert conf == mut.UNUSABLE


def test_partial_detection_is_medium_not_high() -> None:
    v = mut.Validation(task_id="t", executable=True, baseline_passed=True,
                       mutants=[mut.MutantResult("a", "", True, True),
                                mut.MutantResult("b", "", True, False)])
    conf, why = mut.classify(v, strategy_is_mechanical=True, contract_complete=True)
    assert conf == mut.MEDIUM
    assert "1/2" in why


def test_incomplete_contract_caps_confidence_at_medium() -> None:
    v = mut.Validation(task_id="t", executable=True, baseline_passed=True,
                       mutants=[mut.MutantResult("a", "", True, True)])
    high, _ = mut.classify(v, strategy_is_mechanical=True, contract_complete=True)
    capped, _ = mut.classify(v, strategy_is_mechanical=True, contract_complete=False)
    assert high == mut.HIGH and capped == mut.MEDIUM


def test_rubric_strategy_is_low_regardless_of_evidence() -> None:
    v = mut.Validation(task_id="t", executable=True, baseline_passed=True,
                       mutants=[mut.MutantResult("a", "", True, True)])
    conf, _ = mut.classify(v, strategy_is_mechanical=False, contract_complete=True)
    assert conf == mut.LOW


def test_snippet_verifier_validation_discriminates() -> None:
    v = mut.validate_snippet_verifier(
        task_id="t", snippet='json_schema(required=["name"])',
        good_answer='{"name": "x"}',
        bad_answers=['{"other": 1}', "not json"])
    assert v.baseline_passed
    assert v.detected == 2


def test_snippet_verifier_that_accepts_bad_answers_is_caught() -> None:
    v = mut.validate_snippet_verifier(
        task_id="t", snippet="length_between(1, 10000)",
        good_answer="a proper answer",
        bad_answers=["garbage", "also garbage"])
    assert v.detected == 0
    conf, _ = mut.classify(v, strategy_is_mechanical=True, contract_complete=True)
    assert conf == mut.UNUSABLE


# ── Parts 9/10: lifecycle and the approval gate ─────────────────────────────

def _rec(**over) -> vreg.VerifierRecord:
    d = dict(task_id="t1", proposal={"verification_strategy": pr.S_TASK_TEST},
             created_at=time.time())
    d.update(over)
    return vreg.VerifierRecord(**d)


def _good_validation() -> mut.Validation:
    return mut.Validation(task_id="t1", executable=True, baseline_passed=True,
                          mutants=[mut.MutantResult("a", "", True, True),
                                   mut.MutantResult("b", "", True, True)])


def test_proposal_starts_proposed_and_unproven() -> None:
    r = _rec()
    assert r.status == vreg.PROPOSED
    assert r.confidence == mut.UNUSABLE


def test_assistant_cannot_approve() -> None:
    r = _rec()
    r.mark_validated(_good_validation(), contract_complete=True)
    ok, msg = r.approve("assistant")
    # H-10: the guard now names WHY it refused (an automation marker) rather
    # than only that a human is required, and covers every automated actor
    # rather than the single literal "assistant".
    assert not ok
    assert "automated" in msg or "human" in msg, msg


def test_approval_requires_validation_first() -> None:
    r = _rec()
    ok, msg = r.approve("a-person")
    assert not ok
    assert "not been validated" in msg


def test_full_lifecycle_requires_evidence_then_a_human() -> None:
    r = _rec()
    ok, _ = r.mark_validated(_good_validation(), contract_complete=True)
    assert ok and r.status == vreg.VALIDATED and r.confidence == mut.HIGH
    assert r.approve("a-person", "looks right")[0]
    assert r.status == vreg.APPROVED
    assert r.activate("a-person")[0]
    assert r.status == vreg.ACTIVE


def test_failed_validation_auto_rejects() -> None:
    r = _rec()
    bad = mut.Validation(task_id="t1", executable=True, baseline_passed=True,
                         mutants=[mut.MutantResult("a", "", True, False)])
    ok, why = r.mark_validated(bad, contract_complete=True)
    assert not ok
    assert r.status == vreg.REJECTED
    assert r.confidence == mut.UNUSABLE


def test_confidence_is_not_settable_by_the_proposal() -> None:
    """A proposal claiming HIGH must not get it without evidence."""
    r = _rec(proposal={"verification_strategy": pr.S_TASK_TEST,
                       "provisional_confidence": "HIGH"})
    assert r.confidence == mut.UNUSABLE
    weak = mut.Validation(task_id="t1", executable=True, baseline_passed=True,
                          mutants=[mut.MutantResult("a", "", True, False)])
    r.mark_validated(weak, contract_complete=True)
    assert r.confidence == mut.UNUSABLE


def test_every_transition_records_a_reason_and_actor() -> None:
    r = _rec()
    r.mark_validated(_good_validation(), contract_complete=True)
    r.approve("a-person", "checked the contract")
    assert all(t.reason for t in r.history)
    assert r.history[-1].actor == "a-person"


def test_cannot_skip_straight_to_active() -> None:
    r = _rec()
    assert not r.activate("a-person")[0]
    assert r.status == vreg.PROPOSED


# ── Registry persistence, stats, integration ────────────────────────────────

def test_registry_round_trip(tmp_path: Path) -> None:
    reg = vreg.Registry(tmp_path / "v.jsonl")
    r = _rec()
    r.mark_validated(_good_validation(), contract_complete=True)
    reg.save(r)
    again = vreg.Registry(tmp_path / "v.jsonl")
    assert again.get("t1").status == vreg.VALIDATED
    assert again.get("t1").confidence == mut.HIGH


def test_active_for_returns_nothing_until_active(tmp_path: Path) -> None:
    """Part 15's guard: run_matrix must not see a draft verifier."""
    reg = vreg.Registry(tmp_path / "v.jsonl")
    r = _rec()
    r.mark_validated(_good_validation(), contract_complete=True)
    reg.save(r)
    assert reg.active_for("t1") is None, "VALIDATED is not ACTIVE"
    r.approve("a-person")
    reg.save(r)
    assert reg.active_for("t1") is None, "APPROVED is not ACTIVE either"
    r.activate("a-person")
    reg.save(r)
    assert reg.active_for("t1") is not None


def test_stats_report_shape(tmp_path: Path) -> None:
    reg = vreg.Registry(tmp_path / "v.jsonl")
    r = _rec()
    r.mark_validated(_good_validation(), contract_complete=True)
    reg.save(r)
    s = reg.stats(candidate_total=10)
    assert s["candidates"] == 10
    assert s["by_status"][vreg.VALIDATED] == 1
    assert s["by_confidence"][mut.HIGH] == 1
    assert 0.0 <= s["mechanical_coverage"] <= 1.0


# ── Part 12: prioritisation ─────────────────────────────────────────────────

def test_replay_ready_mechanical_outranks_reference_needing(tmp_path: Path) -> None:
    reg = vreg.Registry(tmp_path / "v.jsonl")
    good = _candidate(CODE_TASK, task_id="a")
    good.replay_ready = True
    good.verifier_class = "sandbox"
    weak = _candidate("What is the capital of Portugal?", task_id="b")
    weak.needs_reference_answer = True
    ranked = vreg.rank([weak, good], reg)
    assert ranked[0][1].task_id == "a"


def test_already_authored_candidate_sinks(tmp_path: Path) -> None:
    reg = vreg.Registry(tmp_path / "v.jsonl")
    c = _candidate(CODE_TASK, task_id="a")
    c.replay_ready = True
    before = vreg.priority(c, reg)[0]
    reg.save(_rec(task_id="a"))
    after = vreg.priority(c, reg)[0]
    assert after < before


def test_ranking_is_deterministic(tmp_path: Path) -> None:
    reg = vreg.Registry(tmp_path / "v.jsonl")
    cands = [_candidate(CODE_TASK, task_id=f"t{i}") for i in range(5)]
    assert [c.task_id for _, c, _ in vreg.rank(cands, reg)] == \
           [c.task_id for _, c, _ in vreg.rank(cands, reg)]


# ── Part 13: templates ──────────────────────────────────────────────────────

@pytest.mark.parametrize("task,expected", [
    ("Fix the off-by-one bug in src/pager.py", "bug-fix-with-existing-tests"),
    ("Add a retry with backoff to src/client.py", "add-capability"),
    ("Return a JSON object with the results", "structured-output"),
    ("Bump the dependency version in requirements.txt", "dependency-upgrade"),
    ("Write tests for the parser module", "test-writing"),
])
def test_templates_match_their_shapes(task: str, expected: str) -> None:
    t = ct.pick_template(task)
    assert t is not None and t.name == expected


def test_unmatched_task_gets_no_template_and_says_so() -> None:
    c = ct.derive("t", "zzz qqq wibble frobnicate")
    assert c.template is None
    assert any("no template matched" in u for u in c.unclear)


# ── Part 16: the assistant cannot define truth ──────────────────────────────

def test_proposal_always_requires_human_review() -> None:
    for task in (CODE_TASK, RETRY_TASK, "Return JSON with a name key"):
        p = pr.propose(_candidate(task), repo_root=REPO)
        assert p.human_review_required is True
        assert p.provisional_confidence == "unproven"


# ── Classifier defects that blocked authoring ───────────────────────────────
# Found by running the assistant over ten realistic coding tasks: four were
# sent to `subjective` and could never receive a verifier. Each case below is
# one of those, with a counterpart that must stay rejected.

from groundtruth import classify as cl  # noqa: E402


@pytest.mark.parametrize("task,category", [
    ('Return a JSON object with keys "name" and "year" describing the package.',
     cl.STRUCTURED),
    ("Bump the requests dependency in requirements.txt to 2.32.", cl.CODE_EDIT),
    ("Write tests for the date parser in src/dates.py", cl.CODE_EDIT),
    ("Set the default timeout option in src/config.py to 30 seconds.", cl.CODE_EDIT),
    ("Pin the version in uv.lock to the released one", cl.CODE_EDIT),
])
def test_concrete_tasks_are_not_filed_as_subjective(task: str, category: str) -> None:
    assert cl.classify("x", task).category == category


@pytest.mark.parametrize("task", [
    "Improve the error handling appropriately in src/api.py",
    "Write me a vision document for where the product should go",
    "Create a thorough plan for the audit findings",
])
def test_genuinely_subjective_tasks_stay_subjective(task: str) -> None:
    assert cl.classify("x", task).category == cl.SUBJECTIVE


def test_structured_category_is_reachable() -> None:
    """It carried a verifier mapping from the start and nothing returned it."""
    assert cl.BEST_VERIFIER[cl.STRUCTURED] == ds.V_PROGRAMMATIC
    assert cl.classify("x", "Reply with valid JSON containing the results").category \
        == cl.STRUCTURED


def test_in_repo_text_file_is_not_external_state() -> None:
    assert cl.classify("x", "Bump the dep in requirements.txt").category != cl.EXTERNAL_STATE


def test_attached_document_is_still_external_state() -> None:
    assert cl.classify("x", "Summarise ~/Downloads/report.md for me").category \
        == cl.EXTERNAL_STATE


def test_incidental_subjective_word_does_not_veto_a_concrete_change() -> None:
    """'describing' must not disqualify a structured-output task."""
    a = cl.classify("x", 'Return JSON describing the package').category
    assert a == cl.STRUCTURED
