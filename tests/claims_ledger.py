"""K2 — every load-bearing product claim, tagged to the test that proves it.

The audit's phase 2 built a prose claim matrix and found, among other things,
that `llm-router status` — the README's own first "verify it worked" step —
crashed on every clean install with `ModuleNotFoundError: No module named
'rich'`. That was in a document. A document cannot fail CI.

This is the machine-checked half: a claim carries the test node id that proves
it, and `tests/test_k2_claims_are_proven.py` fails when

  * a claim is `UNPROVEN`,
  * its proof test does not exist,
  * or the README makes a quantitative claim with no ledger entry.

WHAT COUNTS AS A PROOF. The test named must actually exercise the claim. A
claim proven by a test that asserts a constant is worse than an unproven one,
because it carries the appearance of evidence — so proofs are the red-checked
tests from the remediation, not incidental coverage.

WHAT TO DO WITH AN UNPROVABLE CLAIM. Delete it. "Shipping without the claim is
always available and is often right" — the alternative is a product that
describes something other than itself, which is the class this ledger exists to
close.
"""

from __future__ import annotations

from dataclasses import dataclass

PROVEN = "PROVEN"
UNPROVEN = "UNPROVEN"
#: The claim was removed from the product rather than proven. Kept as a row so
#: it cannot quietly return.
WITHDRAWN = "WITHDRAWN"
#: True, but only under stated conditions that the claim itself must carry.
SCOPED = "SCOPED"


@dataclass(frozen=True)
class Claim:
    id: str
    text: str
    source: str
    status: str
    #: pytest node id, `path::test_name`. Checked for existence.
    proof: str = ""
    #: Required for SCOPED and WITHDRAWN. What the limit is, or why it went.
    note: str = ""
    #: README line numbers this claim covers. Declared explicitly rather than
    #: matched by keyword — a heuristic that guesses which row covers which
    #: line is a heuristic pretending to be a ledger, and it failed on the
    #: per-host savings table the first time it ran.
    readme_lines: tuple[int, ...] = ()


LEDGER: tuple[Claim, ...] = (
    # ── Security ────────────────────────────────────────────────────────────
    Claim(
        id="secrets-never-leave",
        text="Secrets never leave your machine. A prompt containing an API key, "
             "token or private material is scrubbed before it is written or sent.",
        source="README.md:298",
        status=PROVEN,
        proof="tests/test_r2_nothing_leaves_unscrubbed.py",
        readme_lines=(298,),
    ),
    Claim(
        id="scrub-before-persistence",
        text="Credentials are redacted before anything reaches disk.",
        source="src/llm_router/attempt_log.py docstring",
        status=PROVEN,
        proof="tests/test_r1_scrub_before_persistence.py",
    ),
    Claim(
        id="allowlist-is-not-containment",
        text="The command allowlist is a typo-and-footgun guardrail, NOT a "
             "containment boundary; an agent with run_command executes "
             "arbitrary code as the operator.",
        source="SECURITY.md",
        status=PROVEN,
        proof="tests/test_r3_allowlist_is_not_containment.py",
        note="This is the corrected claim. The prior one — '10 of 12 refused' "
             "— was accurate and misleading, and is the reason this ledger "
             "records what a claim MEANS rather than only whether it is true.",
    ),
    Claim(
        id="model-commands-inherit-no-env",
        text="A command chosen by a model does not inherit the operator's "
             "environment.",
        source="src/llm_router/hooks/agent_loop.py",
        status=PROVEN,
        proof="tests/test_r4_subprocess_env_allowlist.py",
    ),

    # ── Money ───────────────────────────────────────────────────────────────
    Claim(
        id="savings-are-net-and-labelled",
        text="Every displayed dollar figure carries its baseline model, its "
             "denominator, and whether it is real dollars or baseline-equivalent.",
        source="src/llm_router/savings.py",
        status=SCOPED,
        proof="tests/test_r6_one_savings_number.py",
        note="TRUE of canonical_savings and the 3 migrated surfaces; 17 of 20 "
             "surfaces still compute their own figure and are individually "
             "listed in savings.SURFACES with their divergence.",
    ),
    Claim(
        id="a-loss-is-visible",
        text="Routing that cost more than it saved renders as a loss, never "
             "clamped to zero.",
        source="src/llm_router/savings.py:net_saved",
        status=PROVEN,
        proof="tests/test_r7_a_loss_reaches_the_user.py",
    ),
    Claim(
        id="benchmark-rows-excluded",
        text="Savings figures exclude rows written by tests and benchmarks.",
        source="src/llm_router/cost.py production_only",
        status=PROVEN,
        proof="tests/test_r6_provenance_survives_the_producer_chain.py",
    ),

    # ── Routing quality ─────────────────────────────────────────────────────
    Claim(
        id="preserves-task-success",
        text="Routing to a cheaper model preserves task success.",
        source="README.md (why-route panel)",
        status=UNPROVEN,
        note="THE CENTRAL CLAIM, AND IT IS NOT PROVEN. The only mechanism that "
             "could measure it (Ground Truth capture) was off by default until "
             "R8, so there is no dataset. Neither downgrade-regret nor "
             "upgrade-waste is computed anywhere in the repo. Either measure "
             "it or remove the claim — those were R8's two options and the "
             "measurement half is not done.",
    ),
    Claim(
        id="savings-percentages-per-host",
        text="60–80% savings on Claude Code, 30–50% on Codex CLI, etc.",
        source="README.md (removed 2026-09-24)",
        status=SCOPED,
        note="README already states these are 'single-user observations over "
             "particular workloads' (L394). The scoping sentence IS the proof: "
             "there is no test, and there cannot be one, because the numbers "
             "are observations rather than a property of the code. Remove the "
             "scoping sentence and this becomes UNPROVEN. REMOVED from the "
             "README 2026-09-24: unmeasured, and verified savings on the "
             "maintainer's machine are $0.00 (audit/CRITICAL_2026-09-24_"
             "local_models_do_no_work.md); the row stays so the history reads.",
        readme_lines=(),
    ),
    Claim(
        id="censored-answers-not-counted-as-wins",
        text="A response the provider truncated or filtered is not recorded as "
             "a routing success.",
        source="src/llm_router/router.py",
        status=PROVEN,
        proof="tests/test_r15_finish_reason.py",
    ),

    # ── Observability ───────────────────────────────────────────────────────
    Claim(
        id="every-counter-has-a-reader",
        text="Every instrumentation counter is rendered by a surface an "
             "operator can read.",
        source="src/llm_router/counter_registry.py",
        status=PROVEN,
        proof="tests/test_r12_every_counter_has_a_reader.py",
    ),
    Claim(
        id="hook-death-is-visible",
        text="A hook killed by its timeout is distinguishable from one that "
             "chose not to route.",
        source="src/llm_router/hook_liveness.py",
        status=PROVEN,
        proof="tests/test_r9_hook_death_is_visible.py",
    ),
    Claim(
        id="no-record-loss-under-concurrency",
        text="Concurrent processes do not lose attempt records to rotation.",
        source="src/llm_router/attempt_log.py",
        status=PROVEN,
        proof="tests/reliability/test_r14_attempt_log_rotation.py",
    ),

    # ── Gateway ─────────────────────────────────────────────────────────────
    Claim(
        id="unservable-requests-are-refused",
        text="A request asking for a capability this gateway cannot serve is "
             "refused with a stated reason, never silently degraded.",
        source="src/llm_router/gateway.py",
        status=SCOPED,
        proof="tests/test_r10_refuse_what_cannot_be_served.py",
        note="TRUE for tool/function-calling on all five completion endpoints. "
             "Vision (`images`), structured output (`format`/`response_format`) "
             "and context-window overflow are still silently accepted.",
    ),

    # ── Ground Truth ────────────────────────────────────────────────────────
    Claim(
        id="capture-requires-consent",
        text="Prompt capture is enabled only after explicit consent, and never "
             "in a non-interactive install.",
        source="src/llm_router/ground_truth_consent.py",
        status=PROVEN,
        proof="tests/test_r8_capture_requires_consent.py",
    ),
    Claim(
        id="ground-truth-covers-state-free-only",
        text="Ground Truth measures state-free prompts only; tasks needing repo "
             "state are excluded until a replayer exists.",
        source="src/llm_router/prompt_capture.py docstring",
        status=PROVEN,
        proof="tests/test_r16_capture_can_see_the_repo.py",
    ),
)
