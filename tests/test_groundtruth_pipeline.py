"""The label and discrimination logic, pinned on synthetic matrices.

Ground truth derived from real models costs money and is stochastic, so the
*logic* is tested here against hand-built matrices where the right answer is
known by construction. The point is that each check fails when it should:
a discrimination test that passes every dataset detects nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from groundtruth import dataset as ds  # noqa: E402
from groundtruth.discriminate import policy_score  # noqa: E402
from groundtruth.label import derive  # noqa: E402

TIER_ORDER = ["local", "cheap", "mid", "premium"]


def cell(**accepted: bool) -> dict:
    return {t: {"model": f"m/{t}", "accepted": accepted.get(t, False),
                "passes": int(accepted.get(t, False)), "samples": 1,
                "cost_usd": {"local": 0.0, "cheap": 0.001,
                             "mid": 0.01, "premium": 0.05}[t],
                "reason": ""}
            for t in TIER_ORDER}


# ── label.derive ─────────────────────────────────────────────────────────────

def test_cheapest_acceptable_is_the_cheapest_that_passed() -> None:
    matrix = {"t1": cell(cheap=True, mid=True, premium=True)}
    labels, _ = derive(matrix, TIER_ORDER)
    assert labels["t1"]["cheapest_acceptable_model"] == "cheap"
    assert labels["t1"]["accepted_tiers"] == ["cheap", "mid", "premium"]


def test_local_wins_when_local_passes() -> None:
    matrix = {"t1": cell(local=True, cheap=True, mid=True, premium=True)}
    labels, diag = derive(matrix, TIER_ORDER)
    assert labels["t1"]["cheapest_acceptable_model"] == "local"
    assert "t1" in diag["all_acceptable"]


def test_none_acceptable_gets_no_label() -> None:
    matrix = {"t1": cell()}
    labels, diag = derive(matrix, TIER_ORDER)
    assert labels["t1"]["cheapest_acceptable_model"] is None
    assert labels["t1"]["status"] == "none-acceptable"
    assert diag["none_acceptable"] == ["t1"]


def test_premium_only() -> None:
    matrix = {"t1": cell(premium=True)}
    labels, _ = derive(matrix, TIER_ORDER)
    assert labels["t1"]["cheapest_acceptable_model"] == "premium"


def test_non_monotonic_is_flagged_not_smoothed() -> None:
    # cheap passes, mid fails, premium passes — physically odd, must be visible.
    matrix = {"t1": cell(cheap=True, premium=True)}
    labels, diag = derive(matrix, TIER_ORDER)
    assert labels["t1"]["cheapest_acceptable_model"] == "cheap"
    assert labels["t1"]["monotonic"] is False
    assert diag["non_monotonic"] == ["t1"]


def test_monotonic_case_is_not_flagged() -> None:
    matrix = {"t1": cell(mid=True, premium=True)}
    labels, diag = derive(matrix, TIER_ORDER)
    assert labels["t1"]["monotonic"] is True
    assert diag["non_monotonic"] == []


def test_label_ignores_what_the_router_chose() -> None:
    """A router field in the cell must not influence the label."""
    matrix = {"t1": cell(mid=True, premium=True)}
    matrix["t1"]["local"]["router_selected"] = True  # noqa: the label must not care
    labels, _ = derive(matrix, TIER_ORDER)
    assert labels["t1"]["cheapest_acceptable_model"] == "mid"


# ── discriminate.policy_score ────────────────────────────────────────────────

def _matrix_that_separates(n: int = 40) -> dict:
    """Half the tasks need premium, half are satisfied by cheap."""
    m = {}
    for i in range(n):
        if i % 2:
            m[f"t{i}"] = cell(cheap=True, mid=True, premium=True)
        else:
            m[f"t{i}"] = cell(premium=True)
    return m


def test_separating_matrix_shows_a_gap() -> None:
    m = _matrix_that_separates()
    cheap_rate, _, n = policy_score(m, lambda p, _c: p[0], TIER_ORDER)
    prem_rate, _, _ = policy_score(m, lambda p, _c: p[-1], TIER_ORDER)
    assert n == 40
    assert cheap_rate == 0.0, "always-local should fail everything here"
    assert prem_rate == 1.0
    assert prem_rate - cheap_rate >= 0.10


def test_all_easy_matrix_shows_no_gap() -> None:
    """Every tier passes everything: the dataset cannot measure routing."""
    m = {f"t{i}": cell(local=True, cheap=True, mid=True, premium=True)
         for i in range(20)}
    cheap_rate, _, _ = policy_score(m, lambda p, _c: p[0], TIER_ORDER)
    prem_rate, _, _ = policy_score(m, lambda p, _c: p[-1], TIER_ORDER)
    assert cheap_rate == prem_rate == 1.0
    assert prem_rate - cheap_rate == 0.0, "this must be detected as non-discriminating"


def test_all_impossible_matrix_shows_no_gap() -> None:
    m = {f"t{i}": cell() for i in range(20)}
    cheap_rate, _, _ = policy_score(m, lambda p, _c: p[0], TIER_ORDER)
    prem_rate, _, _ = policy_score(m, lambda p, _c: p[-1], TIER_ORDER)
    assert cheap_rate == prem_rate == 0.0


def test_oracle_is_the_ceiling_and_cheaper_than_always_premium() -> None:
    m = _matrix_that_separates()
    oracle_rate, oracle_cost, _ = policy_score(
        m, lambda p, c: next((t for t in p if c[t]["accepted"]), p[-1]), TIER_ORDER)
    prem_rate, prem_cost, _ = policy_score(m, lambda p, _c: p[-1], TIER_ORDER)
    assert oracle_rate == prem_rate == 1.0
    assert oracle_cost < prem_cost, "oracle must reach the same quality for less"


# ── dataset: schema and split ────────────────────────────────────────────────

def test_mechanical_task_requires_a_verifier() -> None:
    t = ds.Task(task_id="x", prompt="do a real thing here", kind=ds.QA,
                verifier_kind=ds.MECHANICAL, verifier=None)
    assert any("no verifier" in e for e in t.validate())


def test_subjective_task_requires_a_reason() -> None:
    t = ds.Task(task_id="x", prompt="do a real thing here", kind=ds.QA,
                verifier_kind=ds.SUBJECTIVE, verifier=None, ambiguity_reason="")
    assert any("must say WHY" in e for e in t.validate())


def test_subjective_task_may_not_carry_a_verifier() -> None:
    t = ds.Task(task_id="x", prompt="do a real thing here", kind=ds.QA,
                verifier_kind=ds.SUBJECTIVE, verifier='words("a")',
                ambiguity_reason="open-ended")
    assert any("carries a verifier" in e for e in t.validate())


def test_edit_task_requires_a_sandbox() -> None:
    t = ds.Task(task_id="x", prompt="change the thing in the file", kind=ds.EDIT,
                verifier_kind=ds.MECHANICAL, verifier="run('assert True')")
    assert any("needs a sandbox" in e for e in t.validate())


def test_duplicate_task_ids_are_rejected() -> None:
    mk = lambda: ds.Task(task_id="dup", prompt="a valid prompt here", kind=ds.QA,  # noqa: E731
                         verifier_kind=ds.MECHANICAL, verifier='words("a")')
    assert any("duplicate task_id" in e for e in ds.validate_all([mk(), mk()]))


def test_split_is_deterministic_and_disjoint() -> None:
    tasks = [ds.Task(task_id=f"t{i:03d}", prompt="a valid prompt here", kind=ds.QA,
                     verifier_kind=ds.MECHANICAL, verifier='words("a")',
                     triage="standalone") for i in range(40)]
    a1, b1 = ds.split_tasks(tasks, test_fraction=0.5, seed=7)
    a2, b2 = ds.split_tasks(tasks, test_fraction=0.5, seed=7)
    assert [t.task_id for t in a1] == [t.task_id for t in a2]
    assert [t.task_id for t in b1] == [t.task_id for t in b2]
    assert not ({t.task_id for t in a1} & {t.task_id for t in b1})
    assert len(a1) + len(b1) == 40


def test_split_stratifies_across_triage_kinds() -> None:
    tasks = ([ds.Task(task_id=f"s{i}", prompt="a valid prompt here", kind=ds.QA,
                      verifier_kind=ds.MECHANICAL, verifier='words("a")',
                      triage="standalone") for i in range(10)]
             + [ds.Task(task_id=f"r{i}", prompt="a valid prompt here", kind=ds.QA,
                        verifier_kind=ds.MECHANICAL, verifier='words("a")',
                        triage="repo-bound") for i in range(10)])
    tune, test = ds.split_tasks(tasks, test_fraction=0.5, seed=1)
    for split in (tune, test):
        kinds = {t.triage for t in split}
        assert kinds == {"standalone", "repo-bound"}, (
            "both strata must appear on both sides")


def test_reading_test_split_requires_a_reason(tmp_path: Path) -> None:
    (tmp_path / "test.jsonl").write_text(json.dumps(ds.TEST_SENTINEL) + "\n")
    with pytest.raises(ValueError, match="explicit reason"):
        ds.load_split(tmp_path, "test")


def test_reading_test_split_is_logged(tmp_path: Path) -> None:
    (tmp_path / "test.jsonl").write_text(json.dumps(ds.TEST_SENTINEL) + "\n")
    ds.load_split(tmp_path, "test", reason="final measurement")
    log = (tmp_path / "test_access.log").read_text()
    assert "final measurement" in log


def test_tune_split_needs_no_reason_and_is_not_logged(tmp_path: Path) -> None:
    (tmp_path / "tune.jsonl").write_text("")
    ds.load_split(tmp_path, "tune")
    assert not (tmp_path / "test_access.log").exists()


def test_sentinel_line_is_not_loaded_as_a_task(tmp_path: Path) -> None:
    task = ds.Task(task_id="t1", prompt="a valid prompt here", kind=ds.QA,
                   verifier_kind=ds.MECHANICAL, verifier='words("a")')
    (tmp_path / "test.jsonl").write_text(
        json.dumps(ds.TEST_SENTINEL) + "\n"
        + json.dumps(task.to_json()) + "\n")
    loaded = ds.load_split(tmp_path, "test", reason="unit test")
    assert [t.task_id for t in loaded] == ["t1"]


def test_content_hash_changes_with_content(tmp_path: Path) -> None:
    a = tmp_path / "a.jsonl"
    a.write_text("one\n")
    h1 = ds.content_hash([a])
    a.write_text("two\n")
    assert ds.content_hash([a]) != h1


# ── Three-state outcomes: PASS / FAIL / AMBIGUOUS ────────────────────────────

def cell3(**outcomes: str) -> dict:
    """Build a cell with explicit three-state outcomes and provenance."""
    return {t: {"model": f"m/{t}",
                "outcome": outcomes.get(t, ds.FAIL),
                "verification_type": ds.V_MECHANICAL,
                "verifier": 'words("x")',
                "confidence": ds.CONFIDENCE_HIGH,
                "cost_usd": {"local": 0.0, "cheap": 0.001,
                             "mid": 0.01, "premium": 0.05}[t]}
            for t in TIER_ORDER}


def test_ambiguous_is_not_acceptance() -> None:
    m = {"t1": cell3(local=ds.AMBIGUOUS, cheap=ds.AMBIGUOUS,
                     mid=ds.AMBIGUOUS, premium=ds.AMBIGUOUS)}
    labels, diag = derive(m, TIER_ORDER)
    assert labels["t1"]["cheapest_acceptable_model"] is None
    assert "t1" in diag["none_acceptable"]


def test_ambiguous_below_cheapest_pass_blocks_the_label() -> None:
    """local unknown, cheap passes -> the true label might be local."""
    m = {"t1": cell3(local=ds.AMBIGUOUS, cheap=ds.PASS,
                     mid=ds.PASS, premium=ds.PASS)}
    labels, diag = derive(m, TIER_ORDER)
    assert labels["t1"]["cheapest_acceptable_model"] is None
    assert "t1" in diag["ambiguous_below_pass"]
    assert "local" in labels["t1"]["note"]


def test_ambiguous_above_cheapest_pass_does_not_block() -> None:
    """cheap passes; an unknown at a COSTLIER tier cannot lower the label."""
    m = {"t1": cell3(local=ds.FAIL, cheap=ds.PASS,
                     mid=ds.AMBIGUOUS, premium=ds.PASS)}
    labels, diag = derive(m, TIER_ORDER)
    assert labels["t1"]["cheapest_acceptable_model"] == "cheap"
    assert "t1" not in diag["ambiguous_below_pass"]
    assert labels["t1"]["ambiguous_tiers"] == ["mid"]


def test_label_carries_its_verification_provenance() -> None:
    m = {"t1": cell3(cheap=ds.PASS, mid=ds.PASS, premium=ds.PASS)}
    labels, _ = derive(m, TIER_ORDER)
    assert labels["t1"]["verification_type"] == ds.V_MECHANICAL
    assert labels["t1"]["verifier"] == 'words("x")'
    assert labels["t1"]["confidence"] == ds.CONFIDENCE_HIGH


def test_mixed_deterministic_and_judge_is_flagged() -> None:
    m = {"t1": cell3(cheap=ds.PASS, mid=ds.PASS, premium=ds.PASS)}
    m["t1"]["premium"]["verification_type"] = ds.V_JUDGE
    _, diag = derive(m, TIER_ORDER)
    assert "t1" in diag["mixed_verification_methods"]


def test_uniform_deterministic_is_not_flagged() -> None:
    m = {"t1": cell3(cheap=ds.PASS, mid=ds.PASS, premium=ds.PASS)}
    _, diag = derive(m, TIER_ORDER)
    assert diag["mixed_verification_methods"] == []


def test_boolean_matrix_still_reads(tmp_path) -> None:
    """Older matrices used `accepted: bool`; they must not silently misread."""
    m = {"t1": cell(cheap=True, mid=True, premium=True)}
    labels, _ = derive(m, TIER_ORDER)
    assert labels["t1"]["cheapest_acceptable_model"] == "cheap"


# ── Outcome record validation ────────────────────────────────────────────────

def test_outcome_requires_verification_provenance() -> None:
    o = ds.Outcome(task_id="t1", model="m", tier="cheap", outcome=ds.PASS,
                   verification_type=ds.V_MECHANICAL, verifier="")
    assert any("verifier identifier is required" in e for e in o.validate())


def test_outcome_rejects_unknown_verification_type() -> None:
    o = ds.Outcome(task_id="t1", model="m", tier="cheap", outcome=ds.PASS,
                   verification_type="vibes", verifier="x")
    assert any("unknown verification_type" in e for e in o.validate())


def test_outcome_rejects_unknown_outcome() -> None:
    o = ds.Outcome(task_id="t1", model="m", tier="cheap", outcome="maybe",
                   verification_type=ds.V_MECHANICAL, verifier="x")
    assert any("outcome must be one of" in e for e in o.validate())


def test_ambiguous_outcome_must_explain_itself() -> None:
    o = ds.Outcome(task_id="t1", model="m", tier="cheap", outcome=ds.AMBIGUOUS,
                   verification_type=ds.V_MECHANICAL, verifier="x", reason="")
    assert any("must say why" in e for e in o.validate())
    ok = ds.Outcome(task_id="t1", model="m", tier="cheap", outcome=ds.AMBIGUOUS,
                    verification_type=ds.V_MECHANICAL, verifier="x",
                    reason="verifier timed out")
    assert ok.validate() == []


def test_judge_is_not_deterministic() -> None:
    judge = ds.Outcome(task_id="t", model="m", tier="cheap", outcome=ds.PASS,
                       verification_type=ds.V_JUDGE, verifier="judge:haiku")
    mech = ds.Outcome(task_id="t", model="m", tier="cheap", outcome=ds.PASS,
                      verification_type=ds.V_MECHANICAL, verifier="pytest")
    assert not judge.is_deterministic
    assert mech.is_deterministic


def test_verification_preference_order_is_strongest_first() -> None:
    order = ds.VERIFICATION_PREFERENCE
    assert order[0] == ds.V_MECHANICAL
    assert order[-1] == ds.V_JUDGE, "LLM judge must rank last"
    assert order.index(ds.V_SANDBOX) < order.index(ds.V_HUMAN)


# ── Sampling for a future Ground Truth v1 ────────────────────────────────────

from dataclasses import dataclass as _dc  # noqa: E402

from groundtruth import sampling as sp  # noqa: E402


@_dc
class FakeUnit:
    task_type: str
    complexity: str
    route_kind: str = "completion"
    tool_execution_attempted: bool = False


def _mix() -> list:
    """Mirrors the audited non-test traffic mix: code 49%, query 45%, analyze 0.4%."""
    out = []
    for tt, n in [("code", 490), ("query", 450), ("research", 26),
                  ("analyze", 4), ("generate", 1)]:
        for i in range(n):
            out.append(FakeUnit(tt, "moderate",
                                "delegate" if i % 100 == 0 else "completion",
                                i % 90 == 0))
    return out


def test_taxonomy_comes_from_the_repo_not_invented() -> None:
    assert sp.TAXONOMY_SOURCE == "llm_router.types"
    assert "code" in sp.TASK_TYPES and "analyze" in sp.TASK_TYPES
    # The one I would have missed by assuming three levels.
    assert "deep_reasoning" in sp.COMPLEXITIES


def test_plan_hits_the_target_exactly() -> None:
    for floor in (0, 1, 3, 5):
        plan = sp.plan_sample(_mix(), target_n=200, min_per_stratum=floor)
        assert plan.planned_n == 200, f"floor={floor} gave {plan.planned_n}"


def test_plan_never_exceeds_population() -> None:
    tiny = [FakeUnit("code", "simple")] * 2
    plan = sp.plan_sample(tiny, target_n=400, min_per_stratum=3)
    assert plan.planned_n == 2


def test_rare_stratum_survives_with_a_floor() -> None:
    units = _mix()
    with_floor = sp.plan_sample(units, target_n=200, min_per_stratum=3)
    drawn = sp.draw(units, with_floor)
    kinds = {u.task_type for u in drawn}
    assert "analyze" in kinds, "a 0.4% stratum must not be sampled away"
    assert "generate" in kinds


def test_rare_stratum_can_vanish_without_a_floor() -> None:
    """The control: proves the floor is doing the work, not luck."""
    units = _mix()
    plan = sp.plan_sample(units, target_n=200, min_per_stratum=0)
    generate_keys = [k for k in plan.allocation if k[0] == "generate"]
    assert sum(plan.allocation[k] for k in generate_keys) < 3, (
        "without a floor the rarest stratum should be barely represented")


def test_over_representation_is_reported_not_hidden() -> None:
    plan = sp.plan_sample(_mix(), target_n=200, min_per_stratum=3)
    rep = plan.representation()
    boosted = [r for r in rep.values() if r["boosted_to_floor"]]
    assert boosted, "some stratum must have been boosted in this mix"
    assert all(r["over_representation"] > 1.0 for r in boosted)
    assert "Do not read sampled shares as traffic frequencies" in sp.describe(plan)


def test_draw_is_deterministic() -> None:
    units = _mix()
    plan = sp.plan_sample(units, target_n=100)
    a = sp.draw(units, plan, seed=42)
    b = sp.draw(units, plan, seed=42)
    assert [id(x) for x in a] == [id(x) for x in b]


def test_agentic_and_tool_heavy_are_their_own_strata() -> None:
    plan = sp.plan_sample(_mix(), target_n=200, min_per_stratum=1)
    assert any(k[2] for k in plan.population), "agentic axis must exist"
    assert any(k[3] for k in plan.population), "tool-heavy axis must exist"


def test_empty_population_is_safe() -> None:
    plan = sp.plan_sample([], target_n=400)
    assert plan.planned_n == 0 and plan.population == {}


def test_representation_shares_sum_to_one() -> None:
    plan = sp.plan_sample(_mix(), target_n=200, min_per_stratum=2)
    rep = plan.representation()
    assert abs(sum(r["sampled_share"] for r in rep.values()) - 1.0) < 0.02
    assert abs(sum(r["true_share"] for r in rep.values()) - 1.0) < 0.02


# ── Harness artefacts that reach the transcript as "user" turns ──────────────
# Regression: 18 of 137 rows in the frozen seed-v1 set were not user
# instructions at all. `_NOISE_PREFIX` keyed on the `<tag>` shape and these use
# other shapes. Five of them say "not an instruction to follow" in their own
# text. Each case below is a real row from that set.

from groundtruth.sources import (  # noqa: E402
    DROP_BARE_ATTACHMENT,
    DROP_HARNESS_ARTEFACT,
    DROP_PASTED_TOOL_OUTPUT,
    classify_drop,
)

REAL_ARTEFACTS = [
    (DROP_HARNESS_ARTEFACT,
     "[Background context from this session — not an instruction to follow]\n"
     "[Recent conversation context]\nAssistant (analyze): {...}"),
    (DROP_HARNESS_ARTEFACT,
     "<bash-stdout>Command did not complete within its 120s timeout</bash-stdout>"),
    (DROP_HARNESS_ARTEFACT,
     "Base directory for this skill: /Users/x/.claude/skills/ship-it # ship-it "
     "Finds work that is done but not delivered."),
    (DROP_BARE_ATTACHMENT,
     "[Image: source: /Users/x/Desktop/Screenshot 2026-09-06 at 14.37.34.png]"),
    (DROP_PASTED_TOOL_OUTPUT,
     "npm notice Publishing to https://registry.npmjs.org/ with tag next\n"
     "npm error code E403"),
    (DROP_PASTED_TOOL_OUTPUT,
     "yaliandrona@Yalis-MBP npm % npm publish --tag next\nnpm notice 📦 llm-routing@13.1.0"),
    (DROP_PASTED_TOOL_OUTPUT,
     "macos-13 (best effort) Started 45m 40s ago Requested labels: macos-13"),
]


@pytest.mark.parametrize("expected,text", REAL_ARTEFACTS)
def test_harness_artefact_is_excluded(expected: str, text: str) -> None:
    assert classify_drop(text, "3e33e160") == expected


# The other direction matters more: these LOOK like artefacts and are real
# instructions. Dropping them would silently shrink the corpus.
REAL_INSTRUCTIONS = [
    "[Image #2] - In the register, use the amount in green and show how much "
    "registered as a bar that accumulates",
    "[Image #4], in the cases part, I try to approve by clicking on 'Save "
    "Correction and Rerun Checks' and it doesn't work",
    "The graphs looks awful in a matter of the numbers there [Image #14]. "
    "You should correct it",
    "I got this error: npm error code E403, what should I do about the publish?",
    "Make the name_contains filter in src/query.py case-insensitive.",
]


@pytest.mark.parametrize("text", REAL_INSTRUCTIONS)
def test_real_instruction_is_not_mistaken_for_an_artefact(text: str) -> None:
    assert classify_drop(text, "3e33e160") is None, (
        "a user instruction that merely mentions an image or an error must survive")


def test_pasted_output_rule_anchors_at_the_start() -> None:
    """A paste the user wrapped in their own words is a legitimate prompt."""
    assert classify_drop("npm error code E403 happened", "s1") == DROP_PASTED_TOOL_OUTPUT
    assert classify_drop("why did I get npm error code E403?", "s1") is None
