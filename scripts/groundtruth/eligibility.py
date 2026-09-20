"""Decide, at capture time, whether a task could ever become Ground Truth.

The lesson this module encodes: collecting prompts and asking later whether
they are evaluable produced 121 historical rows and zero labels. The state a
task needs to be replayed exists only while the task is running. Ask then, or
never.

Two independent questions, deliberately kept apart:

    replayable          can this task be RUN again later, from captured state?
    verification_path   if it ran, could anyone decide whether it succeeded?

A task can be replayable and unverifiable ("write me a vision document" against
a frozen repo), or verifiable and unreplayable (a crisp bug fix whose repo
state was never captured). Only a task that is both becomes a candidate.

Detection reuses `classify.py` — the same regexes that categorised the
historical corpus. That is deliberate: Part 14 of the brief asks this gate to
recognise the historical failure modes, and the strongest evidence that it does
is that it runs the very same detectors which found them.

Bias is conservative by construction. `ground_truth_candidate` starts False and
has to be earned; every path that cannot prove eligibility returns a reason.
"""

from __future__ import annotations

import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from groundtruth import classify as cl  # noqa: E402
from groundtruth import dataset as ds  # noqa: E402
from groundtruth import triage as tr  # noqa: E402

SCHEMA_VERSION = 1

# ── Ineligibility reasons. Deliberately the same strings `classify.py` uses for
# the historical corpus, so a rejection here and a rejection there are directly
# comparable and the accumulation report can be read against seed-v2. ────────
R_NON_PROMPT = cl.NON_PROMPT
R_TEMPLATE = cl.TEMPLATE_AUTOMATION
R_SESSION_STATE = cl.SESSION_STATE
R_EXTERNAL_STATE = cl.EXTERNAL_STATE
R_MACHINE_STATE = cl.MACHINE_STATE
R_SUBJECTIVE = cl.SUBJECTIVE
R_NO_VERIFIER = "no-credible-verifier"
R_ENVELOPE_INCOMPLETE = "replay-envelope-incomplete"
R_PRIVACY = "cannot-capture-safely"
R_TOO_SHORT = "degenerate-too-short"

# Verifier classes, reusing the repo's existing preference order rather than a
# parallel vocabulary. `V_NONE` is the honest terminal value.
V_NONE = "none-known"
VERIFIER_CLASSES = ds.VERIFICATION_PREFERENCE + (V_NONE,)


@dataclass
class Eligibility:
    """Why a task is, or is not, a future Ground Truth candidate.

    Every field is recorded even when it is False, so the pool can be queried
    for "what is blocking us" rather than only "what got through".
    """

    schema_version: int = SCHEMA_VERSION

    # Replay
    replayable: bool = False
    requires_session_state: bool = False
    requires_external_state: bool = False
    requires_machine_state: bool = False
    requires_repo_state: bool = False
    requires_tool_state: bool = False

    # Verification
    verification_candidate: bool = False
    verifier_class: str = V_NONE
    subjective: bool = False
    # A factual question is replayable and has a mechanical verifier SHAPE, but
    # nobody knows the right answer yet. Somebody must establish the reference
    # answer from evidence — not from belief — before this can be graded.
    # Such a candidate is admitted (it IS replayable) but never counts as
    # high-confidence until a human supplies the reference.
    needs_reference_answer: bool = False

    # Outcome
    ground_truth_candidate: bool = False
    ineligibility_reasons: list[str] = field(default_factory=list)
    notes: str = ""

    # What the envelope would have to carry for this task to be replayable.
    # Recorded even when ineligible, so a later improvement to capture can be
    # aimed at the states that actually block the most tasks.
    required_state: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


# A question is not automatically verifiable. Validated against the historical
# set: an earlier version admitted 7 rows that hand review had already
# rejected — "How the oracle can be 74.33 if the best score in routerArena is
# 79?", "Why can't you do a full scale evaluation locally?", "Maybe
# gemini-3-flash-preview can act like the router". Each is phrased as a
# question and none has a checkable reference answer.
#
# What separates them from "What is the capital of Portugal?" is grammatical
# person and mood: a verifiable factual question is about the world, in the
# third person, asked indicatively. These two patterns encode that, crudely and
# on purpose — over-rejecting costs a candidate, over-admitting costs the
# dataset's trustworthiness.
_SPECULATIVE = re.compile(
    r"^\s*(maybe|perhaps|could|should|can|can't|cannot|would|might|why|how come"
    r"|what if|is it possible|do you think|wouldn't|shouldn't|couldn't)\b"
    r"|\b(i think|i guess|probably|presumably|seems like|sounds like)\b",
    re.I,
)

# First and second person: the question is about us, this system, or this
# conversation — not about a fact anyone could check independently.
_PERSONAL = re.compile(
    r"(?<![\w-])(i|me|my|mine|we|us|our|ours|you|your|yours)(?![\w-])",
    re.I,
)


def _is_checkable_question(prompt: str) -> bool:
    """True only for a third-person, indicative question about stable fact."""
    s = prompt.strip()
    if "?" not in s and not re.match(r"^\s*(what|which|who|when|where|how many|how much)\b",
                                     s, re.I):
        return False
    if _SPECULATIVE.search(s):
        return False
    if _PERSONAL.search(s):
        return False
    return True


# Explicit command invocation, not English that happens to share a word.
#
# An earlier substring check matched "make " and rejected "Make the
# name_contains filter case-insensitive" as needing tool state, while "Add a
# retry to src/client.py" was admitted — two tasks of the same shape, split by
# an imperative verb. `make`, `run` and `build` are ordinary English; they only
# indicate tool state when used as commands.
_TOOL_STATE = re.compile(
    r"`[^`]*`"                                   # anything in backticks
    r"|\b(pytest|npm|pnpm|yarn|cargo|docker|kubectl|terraform|curl|wget)\b"
    r"|\bgit\s+\w+"                              # git <subcommand>
    r"|\bmake\s+(build|test|install|clean|all|check)\b"
    r"|\b(run|execute)\s+(the\s+)?(tests?|suite|build|script|command|pipeline)\b"
    r"|\b(shell|bash|zsh)\s+(command|script)\b"
    r"|\b(test|build)\s+command\b",
    re.I,
)


def _needs_tool_state(prompt: str) -> bool:
    return bool(_TOOL_STATE.search(prompt))


def assess(
    prompt: str,
    *,
    task_type: str | None = None,
    duplicate_count: int = 1,
    has_repo_state: bool = False,
    has_external_evidence: bool = False,
    scrub_safe: bool = True,
    envelope_complete: bool | None = None,
) -> Eligibility:
    """Classify one task. Pure: no I/O, no clock, no network.

    `has_repo_state` / `has_external_evidence` are what the CALLER managed to
    capture. The gate does not assume either — a task that needs repo state and
    did not get it is not replayable, and saying so is the entire point.

    `envelope_complete` is checked last: a task can pass every content test and
    still fail because the state it needs was not actually preserved.
    """
    e = Eligibility()
    s = " ".join((prompt or "").split())

    if not s or len(s.split()) < 5:
        e.ineligibility_reasons.append(R_TOO_SHORT)
        return e

    if not scrub_safe:
        # Privacy wins over evaluation, always. A task whose required state
        # cannot be stored safely is ineligible; it is never a reason to
        # weaken scrubbing.
        e.ineligibility_reasons.append(R_PRIVACY)
        return e

    verdict = cl.classify("live", s, duplicate_count=duplicate_count)
    cat = verdict.category

    # ── Hard stops: not a task at all ────────────────────────────────────────
    if cat == cl.NON_PROMPT:
        e.ineligibility_reasons.append(R_NON_PROMPT)
        e.notes = verdict.reason
        return e
    if cat == cl.TEMPLATE_AUTOMATION:
        e.ineligibility_reasons.append(R_TEMPLATE)
        e.notes = verdict.reason
        return e

    # ── State requirements ───────────────────────────────────────────────────
    e.requires_session_state = cat == cl.SESSION_STATE
    e.requires_machine_state = cat == cl.MACHINE_STATE
    e.requires_external_state = cat == cl.EXTERNAL_STATE
    # `triage._REPO_BOUND` is the repo-reference detector used to build the
    # historical corpus; reusing it keeps one definition of "names a local
    # artefact" rather than a second that could drift from it.
    e.requires_repo_state = bool(tr._REPO_BOUND.search(s))
    e.requires_tool_state = _needs_tool_state(s)
    e.subjective = cat == cl.SUBJECTIVE

    for flag, name in ((e.requires_session_state, "session"),
                       (e.requires_external_state, "external-evidence"),
                       (e.requires_machine_state, "machine"),
                       (e.requires_repo_state, "repo"),
                       (e.requires_tool_state, "tool")):
        if flag:
            e.required_state.append(name)

    # ── Replayability ────────────────────────────────────────────────────────
    # Session state is the one requirement nothing can satisfy after the fact:
    # the referent of "continue what we were doing" is a conversation, and even
    # capturing the transcript would not make the instruction well-defined.
    if e.requires_session_state:
        e.ineligibility_reasons.append(R_SESSION_STATE)
    # Machine state ("which models are loaded right now") is reproducible in
    # principle but nothing here captures it, so it is treated as unavailable.
    if e.requires_machine_state:
        e.ineligibility_reasons.append(R_MACHINE_STATE)
    # External state IS satisfiable — but only if the evidence was frozen at
    # task time with provenance. Assuming the URL still says the same thing is
    # exactly the assumption the brief forbids.
    if e.requires_external_state and not has_external_evidence:
        e.ineligibility_reasons.append(R_EXTERNAL_STATE)
    # Repo state is satisfiable by an immutable commit reference.
    if e.requires_repo_state and not has_repo_state:
        e.ineligibility_reasons.append(R_ENVELOPE_INCOMPLETE)

    e.replayable = not e.ineligibility_reasons

    # ── Verification path ────────────────────────────────────────────────────
    # A credible path, not an authored verifier. Ordered strongest-first, and
    # the strongest one the task shape can support is the one recorded.
    if cat == cl.STRUCTURED:
        e.verifier_class = ds.V_PROGRAMMATIC
    elif cat == cl.CODE_EDIT:
        e.verifier_class = ds.V_SANDBOX if e.requires_repo_state else ds.V_MECHANICAL
    elif cat == cl.FACTUAL:
        # Only a third-person, indicative question about stable fact. A
        # speculative or first/second-person question has no reference answer,
        # however confidently it is phrased.
        if _is_checkable_question(s):
            e.verifier_class = ds.V_MECHANICAL
            e.needs_reference_answer = True
        else:
            e.verifier_class = V_NONE
    elif e.requires_tool_state and e.requires_repo_state:
        e.verifier_class = ds.V_SANDBOX
    elif e.subjective:
        e.verifier_class = V_NONE
    else:
        e.verifier_class = V_NONE

    e.verification_candidate = e.verifier_class != V_NONE
    if not e.verification_candidate:
        e.ineligibility_reasons.append(
            R_SUBJECTIVE if e.subjective else R_NO_VERIFIER)

    # ── Envelope completeness, checked last ──────────────────────────────────
    if envelope_complete is False:
        if R_ENVELOPE_INCOMPLETE not in e.ineligibility_reasons:
            e.ineligibility_reasons.append(R_ENVELOPE_INCOMPLETE)

    e.ground_truth_candidate = (
        e.replayable and e.verification_candidate and not e.ineligibility_reasons)
    if not e.notes:
        e.notes = verdict.reason
    return e


def summarise(assessments: list[Eligibility]) -> dict:
    """Counts for the accumulation report. Every task lands in exactly one of
    `candidates` or one rejection bucket, and the totals are asserted to
    balance — a funnel that does not add up cannot be trusted to say what is
    blocking accumulation."""
    out: dict[str, int] = {
        "captured": len(assessments),
        "candidates": 0,
        "replayable": 0,
        "verification_candidate": 0,
    }
    rejected: dict[str, int] = {}
    for a in assessments:
        if a.replayable:
            out["replayable"] += 1
        if a.verification_candidate:
            out["verification_candidate"] += 1
        if a.ground_truth_candidate:
            out["candidates"] += 1
        else:
            # First reason only, so the buckets partition the rejections.
            reason = a.ineligibility_reasons[0] if a.ineligibility_reasons else "unknown"
            rejected[reason] = rejected.get(reason, 0) + 1
    out["rejected"] = rejected
    assert out["candidates"] + sum(rejected.values()) == out["captured"], (
        "rejection buckets must partition the non-candidates")
    return out
