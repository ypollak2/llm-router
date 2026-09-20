"""Ground-truth task schema, split guard, and manifest.

A corpus row is a *prompt*. A task is a prompt plus an acceptance assertion,
and the assertion has to be written by a person.

That is not a limitation of this module, it is what the repo's verifiers are:
every one of `bench_backend_quality.py`, `bench_agent_tasks.py`,
`bench_brutal_suite.py` and `bench_edit_format.py` pairs a hand-authored
prompt with a hand-authored check, and none can derive a check from a prompt.
`bench_grounding.py` looks like an exception but is not — it only handles
"which file defines X", where `git grep` supplies the answer.

So a task carries `verifier_kind`:

  mechanical  an assertion that runs and exits 0/non-0. Ground truth.
  subjective  no assertion can be written. Kept, labelled, and reported
              SEPARATELY — never merged into the mechanical population.

The two never mix in a reported number. `label.py` refuses to emit a
`cheapest_acceptable_model` for a subjective task.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCHEMA_VERSION = 2

MECHANICAL = "mechanical"
SUBJECTIVE = "subjective"

# ── Outcomes ─────────────────────────────────────────────────────────────────
# Three states, not two. A binary pass/fail has nowhere to put "the verifier
# could not decide", so an inconclusive run silently becomes a FAIL and the
# model is blamed for the harness. AMBIGUOUS keeps those separate: it is
# excluded from labelling and reported on its own, rather than quietly moving
# `cheapest_acceptable_model` one tier up.
PASS = "pass"
FAIL = "fail"
AMBIGUOUS = "ambiguous"
OUTCOMES = (PASS, FAIL, AMBIGUOUS)

# ── Verification methods, strongest first ────────────────────────────────────
# The order is the preference order: never reach for a weaker method when a
# stronger one can decide the same task. `confidence` travels with the outcome
# so a record never has to be interpreted by guessing how it was graded.
V_MECHANICAL = "mechanical"      # deterministic assertion, exit 0/non-0
V_SANDBOX = "sandbox"            # executed in an isolated tree, behaviour asserted
V_PROGRAMMATIC = "programmatic"  # structural check on the output (schema, length)
V_ASSERTION = "task_assertion"   # hand-authored, task-specific
V_EXISTING = "existing_verifier"  # one of the repo's bench_* suites
V_HUMAN = "human"                # reviewed by a person
V_JUDGE = "llm_judge"            # last resort; never mixed with the above

VERIFICATION_PREFERENCE = (
    V_MECHANICAL, V_SANDBOX, V_PROGRAMMATIC, V_ASSERTION,
    V_EXISTING, V_HUMAN, V_JUDGE,
)

# Methods whose verdicts may be aggregated into one reported number. A judge
# verdict is a measurement of the judge as much as of the model, so it is kept
# out until its agreement with human labels has itself been measured.
DETERMINISTIC_METHODS = frozenset(
    {V_MECHANICAL, V_SANDBOX, V_PROGRAMMATIC, V_ASSERTION, V_EXISTING})
SUBJECTIVE_METHODS = frozenset({V_HUMAN, V_JUDGE})

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"


@dataclass
class Outcome:
    """One (task, model) cell, carrying how it was decided.

    `verification_type` and `verifier` are required, not optional: an outcome
    whose provenance is unknown cannot be pooled with one whose provenance is
    known, and making them mandatory is what stops the two populations merging
    by accident.
    """

    task_id: str
    model: str
    tier: str
    outcome: str                      # PASS | FAIL | AMBIGUOUS
    verification_type: str            # one of VERIFICATION_PREFERENCE
    verifier: str                     # concrete identifier: "pytest", 'words("x")'
    confidence: str = CONFIDENCE_HIGH
    reason: str = ""
    passes: int = 0
    samples: int = 1
    cost_usd: float = 0.0
    latency_ms: float | None = None

    @property
    def is_deterministic(self) -> bool:
        return self.verification_type in DETERMINISTIC_METHODS

    def validate(self) -> list[str]:
        errs: list[str] = []
        if self.outcome not in OUTCOMES:
            errs.append(f"{self.task_id}/{self.model}: outcome must be one of {OUTCOMES}")
        if self.verification_type not in VERIFICATION_PREFERENCE:
            errs.append(f"{self.task_id}/{self.model}: unknown verification_type "
                        f"{self.verification_type!r}")
        if not self.verifier:
            errs.append(f"{self.task_id}/{self.model}: verifier identifier is required")
        if self.outcome == AMBIGUOUS and not self.reason:
            errs.append(f"{self.task_id}/{self.model}: an ambiguous outcome must "
                        "say why it could not be decided")
        return errs

    def to_json(self) -> dict:
        return asdict(self)

QA = "qa"      # answer a question; blast radius empty
EDIT = "edit"  # change files in a sandbox; verifier imports and asserts

# Written into the test split file as its first line. Any reader that does not
# understand it should stop rather than silently treat test data as tune data.
TEST_SENTINEL = {
    "__sentinel__": "TEST-SPLIT-DO-NOT-TUNE",
    "warning": (
        "This is the held-out split. Reading it during router development "
        "invalidates every number measured on it. Access is logged to "
        "test_access.log."
    ),
}


@dataclass
class Task:
    task_id: str
    prompt: str
    kind: str                     # QA | EDIT
    verifier_kind: str            # MECHANICAL | SUBJECTIVE
    verifier: str | None          # Python snippet; None iff SUBJECTIVE
    ambiguity_reason: str | None = None   # required iff SUBJECTIVE
    sandbox: str | None = None    # fixture tree name for EDIT/repo-bound tasks
    allowed_files: list[str] = field(default_factory=list)
    origin_sha: str | None = None  # content_sha of the corpus row it came from
    origin_file: str | None = None
    origin_index: int | None = None
    triage: str | None = None
    # Set when a task is graded by a verifier adopted from the registry, so the
    # outcome row records which verification class actually decided it.
    verification_type: str | None = None
    authored_by: str = "human"
    authored_at: str = ""
    notes: str = ""

    def validate(self) -> list[str]:
        """Return a list of problems. Empty means the task is well-formed."""
        errs: list[str] = []
        if not self.task_id:
            errs.append("task_id is empty")
        if not self.prompt or len(self.prompt.split()) < 3:
            errs.append(f"{self.task_id}: prompt too short to be a task")
        if self.kind not in (QA, EDIT):
            errs.append(f"{self.task_id}: kind must be {QA!r} or {EDIT!r}")
        if self.verifier_kind not in (MECHANICAL, SUBJECTIVE):
            errs.append(f"{self.task_id}: verifier_kind must be "
                        f"{MECHANICAL!r} or {SUBJECTIVE!r}")
        if self.verifier_kind == MECHANICAL:
            if not (self.verifier or "").strip():
                errs.append(f"{self.task_id}: mechanical task has no verifier")
            if self.ambiguity_reason:
                errs.append(f"{self.task_id}: mechanical task carries an "
                            "ambiguity_reason; it belongs in the subjective set")
        else:
            if self.verifier:
                errs.append(f"{self.task_id}: subjective task carries a verifier")
            if not (self.ambiguity_reason or "").strip():
                errs.append(f"{self.task_id}: subjective task must say WHY "
                            "mechanical verification is insufficient")
        if self.kind == EDIT and not self.sandbox:
            errs.append(f"{self.task_id}: edit task needs a sandbox fixture")
        return errs

    def to_json(self) -> dict:
        return asdict(self)


def load_tasks(path: Path) -> list[Task]:
    tasks: list[Task] = []
    if not path.exists():
        return tasks
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        obj = json.loads(line)
        if "__sentinel__" in obj:
            continue
        tasks.append(Task(**{k: v for k, v in obj.items()
                             if k in Task.__dataclass_fields__}))
    return tasks


def validate_all(tasks: list[Task]) -> list[str]:
    errs: list[str] = []
    seen: set[str] = set()
    for t in tasks:
        errs.extend(t.validate())
        if t.task_id in seen:
            errs.append(f"duplicate task_id {t.task_id!r}")
        seen.add(t.task_id)
    return errs


# ── Splitting ────────────────────────────────────────────────────────────────

def split_tasks(tasks: list[Task], *, test_fraction: float = 0.5,
                seed: int = 20260920) -> tuple[list[Task], list[Task]]:
    """Deterministic split, stratified by (triage, kind, verifier_kind).

    Stratifying matters more than usual here: the mechanical population is
    small, and an unstratified shuffle can leave one side with almost no
    mechanically-verified tasks, which would make the held-out number
    unmeasurable rather than merely noisy.
    """
    rng = random.Random(seed)
    strata: dict[tuple, list[Task]] = {}
    for t in tasks:
        strata.setdefault((t.triage, t.kind, t.verifier_kind), []).append(t)

    tune: list[Task] = []
    test: list[Task] = []
    for key in sorted(strata, key=str):
        group = sorted(strata[key], key=lambda t: t.task_id)
        rng.shuffle(group)
        cut = round(len(group) * test_fraction)
        test.extend(group[:cut])
        tune.extend(group[cut:])
    return (sorted(tune, key=lambda t: t.task_id),
            sorted(test, key=lambda t: t.task_id))


def note_test_access(root: Path, reason: str) -> None:
    """Record every read of the held-out split.

    There is no way to make a file unreadable to its owner, so the guard is
    visibility rather than prevention: if a tuning run ever touches the test
    set, the log says so and the resulting number can be discarded.
    """
    log = root / "test_access.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "reason": reason,
            "pid": os.getpid(),
            "argv": " ".join(os.sys.argv[:4]),
        }) + "\n")


def load_split(root: Path, split: str, *, reason: str = "") -> list[Task]:
    """Load 'tune' or 'test'. Reading 'test' requires a stated reason."""
    if split == "test":
        if not reason:
            raise ValueError(
                "Reading the held-out split requires an explicit reason. "
                "If you are tuning the router, use the tune split instead.")
        note_test_access(root, reason)
    return load_tasks(root / f"{split}.jsonl")


# ── Manifest ─────────────────────────────────────────────────────────────────

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def content_hash(paths: list[Path]) -> str:
    """Hash over the *contents* of the frozen files, in a stable order.

    Re-running the extractor on a different day must produce a different hash
    if and only if the data changed, so the hash covers file bytes and not
    mtimes or paths.
    """
    h = hashlib.sha256()
    for p in sorted(paths, key=lambda x: x.name):
        h.update(p.name.encode())
        h.update(sha256_file(p).encode())
    return h.hexdigest()
