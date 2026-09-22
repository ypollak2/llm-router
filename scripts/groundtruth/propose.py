"""Propose the strongest practical verifier for a Ground Truth candidate.

Proposes. Does not decide. Nothing here may mark a task verified, and the
`confidence` a proposal carries is provisional until `mutants.py` has shown the
verifier can tell a correct implementation from a broken one. A proposal that
has never been challenged is a suggestion, whatever it says about itself.

Order of operations, and each step can veto the next:

    1. intake         — read the candidate and its envelope. Nothing is re-derived.
    2. contract       — what must be true? If unclear, stop and say so.
    3. repo search    — does a test already cover this? Reuse beats authoring.
    4. strategy       — strongest class the captured state can actually support.
    5. generate       — only where the contract is mechanical.

Step 3 exists because the cheapest verifier is one that already exists, and the
brief is explicit that a proposal must explain why existing verification is or
is not sufficient. "I did not look" is not an explanation.
"""

from __future__ import annotations

import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from groundtruth import contract as ct  # noqa: E402
from groundtruth import dataset as ds  # noqa: E402

SCHEMA_VERSION = 1

# ── Strategies, strongest first. Mirrors the brief's preference order and maps
# onto the repo's existing verification classes rather than inventing a second
# vocabulary. ────────────────────────────────────────────────────────────────
S_EXISTING_TESTS = "existing_tests"
S_TASK_TEST = "task_specific_test"
S_INTEGRATION = "integration_test"
S_BUILD = "build_check"
S_ASSERTION = "deterministic_assertion"
S_SCHEMA = "schema_validation"
S_SANDBOX = "sandbox_execution"
S_FILE_STATE = "file_state_assertion"
S_REFERENCE = "frozen_reference"
S_RUBRIC = "human_rubric"
S_JUDGE = "llm_judge"
S_NONE = "no_reliable_verifier"

STRATEGY_ORDER = (
    S_EXISTING_TESTS, S_TASK_TEST, S_INTEGRATION, S_BUILD, S_ASSERTION,
    S_SCHEMA, S_SANDBOX, S_FILE_STATE, S_REFERENCE, S_RUBRIC, S_JUDGE, S_NONE,
)

# Which of the repo's verification classes each strategy reports as.
STRATEGY_CLASS: dict[str, str] = {
    S_EXISTING_TESTS: ds.V_EXISTING,
    S_TASK_TEST: ds.V_MECHANICAL,
    S_INTEGRATION: ds.V_SANDBOX,
    S_BUILD: ds.V_MECHANICAL,
    S_ASSERTION: ds.V_MECHANICAL,
    S_SCHEMA: ds.V_PROGRAMMATIC,
    S_SANDBOX: ds.V_SANDBOX,
    S_FILE_STATE: ds.V_PROGRAMMATIC,
    S_REFERENCE: ds.V_MECHANICAL,
    S_RUBRIC: ds.V_HUMAN,
    S_JUDGE: ds.V_JUDGE,
    S_NONE: "none-known",
}

MECHANICAL_STRATEGIES = frozenset({
    S_EXISTING_TESTS, S_TASK_TEST, S_INTEGRATION, S_BUILD, S_ASSERTION,
    S_SCHEMA, S_SANDBOX, S_FILE_STATE, S_REFERENCE})


@dataclass
class TaskInput:
    """Normalised intake. A view over the candidate, not a copy of it."""

    task_id: str
    task: str
    task_type: str | None = None
    repo_commit: str | None = None
    repo_dirty: bool = False
    repo_reconstructable: bool = False
    named_files: list[str] = field(default_factory=list)
    external_evidence: list[dict] = field(default_factory=list)
    available_tools: list[str] = field(default_factory=list)
    existing_tests: list[str] = field(default_factory=list)
    test_command: str | None = None
    expected_verification_class: str | None = None
    replay_ready: bool = False

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class Proposal:
    task_id: str
    schema_version: int = SCHEMA_VERSION
    verification_strategy: str = S_NONE
    verification_class: str = "none-known"
    acceptance_contract: dict = field(default_factory=dict)

    existing_evidence: list[str] = field(default_factory=list)
    existing_sufficient: bool = False
    existing_rationale: str = ""

    new_verifier_required: bool = True
    proposed_files: list[dict] = field(default_factory=list)   # {path, content}
    verifier_snippet: str | None = None                        # run_verifier form
    commands: list[str] = field(default_factory=list)

    risks: list[str] = field(default_factory=list)
    gaming_guards: list[str] = field(default_factory=list)
    # Provisional only. `mutants.py` replaces this with evidence-based
    # confidence; until then it says what the shape COULD reach, not what it has
    # shown.
    provisional_confidence: str = "unproven"
    human_review_required: bool = True
    blockers: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


# ── Part 1: intake ───────────────────────────────────────────────────────────

def intake(candidate) -> TaskInput:
    """Read a pool Candidate into the normalised shape. Derives nothing new."""
    env = candidate.envelope or {}
    repo = env.get("repo") or {}
    named = sorted((repo.get("file_hashes") or {}).keys())
    task = env.get("prompt") or ""
    return TaskInput(
        task_id=candidate.task_id,
        task=task,
        task_type=candidate.task_type,
        repo_commit=repo.get("commit"),
        repo_dirty=bool(repo.get("dirty")),
        repo_reconstructable=_reconstructable(repo),
        named_files=named or _files_named_in(task),
        external_evidence=list(env.get("external") or []),
        available_tools=list(env.get("tool_names") or []),
        test_command=env.get("test_command"),
        expected_verification_class=candidate.verifier_class,
        replay_ready=candidate.replay_ready,
    )


def _reconstructable(repo: dict) -> bool:
    if not repo.get("commit"):
        return False
    if not repo.get("dirty"):
        return True
    return bool(repo.get("diff")) and not repo.get("diff_truncated")


_FILE_RE = re.compile(
    r"\b([\w./-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|rb|sh|sql|ya?ml|toml|json))\b")


def _files_named_in(task: str) -> list[str]:
    return sorted({m.group(1) for m in _FILE_RE.finditer(task or "")})


# ── Part 4: search the repository before authoring ──────────────────────────

def find_existing_tests(task: TaskInput, repo_root: Path) -> tuple[list[str], str]:
    """(test files that plausibly cover this, rationale).

    Deliberately shallow: it looks for tests whose name or body references the
    module the task touches. A hit is a candidate for reuse, never proof of
    coverage — whether the existing test actually asserts the NEW behaviour is
    something only running it against the unfixed code can establish, which is
    what `mutants.py` is for.
    """
    tests_dir = repo_root / "tests"
    if not tests_dir.is_dir() or not task.named_files:
        return [], ("no tests/ directory" if not tests_dir.is_dir()
                    else "the task names no source file, so nothing to match on")

    stems = {Path(f).stem for f in task.named_files}
    hits: list[str] = []
    for tf in sorted(tests_dir.glob("test_*.py")):
        name_hit = any(s in tf.stem for s in stems)
        body_hit = False
        if not name_hit:
            try:
                body = tf.read_text(encoding="utf-8", errors="replace")
                body_hit = any(re.search(rf"\b{re.escape(s)}\b", body) for s in stems)
            except OSError:
                continue
        if name_hit or body_hit:
            hits.append(str(tf.relative_to(repo_root)))

    if not hits:
        return [], (f"no test file references {sorted(stems)}; a new test is required")
    return hits, (
        f"{len(hits)} test file(s) reference {sorted(stems)}. They are candidates "
        "for reuse, but none is known to assert the NEW behaviour — that has to "
        "be demonstrated by running them against the unfixed code.")


# ── Part 3: strategy selection ───────────────────────────────────────────────

def _is_reference_checkable(task: TaskInput) -> bool:
    """Does this task have a single stable answer that a frozen reference can check?

    Delegates the QUESTION-SHAPE judgement to `eligibility._is_checkable_question`
    rather than re-deciding it. T-17 was the two modules disagreeing about what
    is verifiable; a second copy of the rule here would guarantee they drift
    apart again.

    Prefers the class eligibility already recorded on the candidate
    (`expected_verification_class`) when there is one, because that value was
    computed from the full envelope, not from the prompt alone.
    """
    if (task.expected_verification_class or "") in (ds.V_MECHANICAL, ds.V_PROGRAMMATIC):
        pass  # eligibility already judged it checkable; fall through to the shape test
    elif task.expected_verification_class:
        return False    # eligibility judged it something else; do not overrule
    try:
        from groundtruth.eligibility import _is_checkable_question
    except Exception:  # noqa: BLE001 — a missing helper must not crash proposal
        return False
    return bool(task.task) and _is_checkable_question(task.task)


def select_strategy(task: TaskInput, contract: ct.Contract,
                    existing: list[str]) -> tuple[str, list[str]]:
    """(strategy, blockers). Strongest the captured state can actually support."""
    blockers: list[str] = []

    # T-17 (audit 2026-09-22). This MUST come before the actionability gate.
    #
    # A FACTUAL checkable question — "What is the capital of Portugal?" — is the
    # shape `eligibility` is PROUDEST of admitting: it sets
    # `verifier_class = V_MECHANICAL` and `needs_reference_answer = True`, and
    # the corpus work found these to be the most gradable population in real
    # traffic. `select_strategy` then returned `no_reliable_verifier` for every
    # one of them, so the two modules disagreed about the same task.
    #
    # The reason is one level up from where it looks. `contract.is_actionable`
    # is False for a bare question — no required condition can be derived from
    # "What is the capital of Portugal?" — and the gate returned S_NONE before
    # any strategy branch ran. But for THIS class the required condition is not
    # in the task text at all: it is "the answer matches the frozen reference".
    # Asking the prompt to contain its own acceptance criterion is the wrong
    # question for a question.
    #
    # `S_REFERENCE` already IS this strategy; it was simply unreachable except
    # via `external_evidence`. A missing reference answer is a BLOCKER —
    # something to capture — not a different strategy, and losing that
    # distinction is what turned "we need an answer key" into "ungradable".
    if _is_reference_checkable(task):
        blockers.append(
            "reference answer not yet captured — freeze one before this verifier "
            "can be validated")
        return S_REFERENCE, blockers

    if not contract.is_actionable:
        blockers.extend(contract.unclear or ["contract has no required condition"])
        return S_RUBRIC if contract.conditions else S_NONE, blockers

    tmpl = ct.pick_template(task.task)
    hinted = tmpl.strategy if tmpl else None

    if hinted == "schema_validation":
        return S_SCHEMA, blockers
    if hinted == "build_check":
        if not task.test_command and not task.available_tools:
            blockers.append("build/test command was not captured")
        return S_BUILD, blockers
    if hinted == "file_state_assertion":
        return S_FILE_STATE, blockers

    # Code work needs a tree to run against.
    if task.named_files or task.repo_commit:
        if not task.repo_reconstructable:
            blockers.append("repo state is not reconstructable; a test cannot be run")
            return S_NONE, blockers
        if existing and task.test_command:
            # Existing tests are the strongest class, but only become the
            # strategy once shown to discriminate; until then the proposal
            # pairs them with a new test.
            return S_TASK_TEST, blockers
        return S_TASK_TEST, blockers

    if task.external_evidence:
        return S_REFERENCE, blockers

    blockers.append("nothing in the envelope supports an executable check")
    return S_NONE, blockers


# ── Part 6: generation ───────────────────────────────────────────────────────

_TEST_HEADER = '''"""Ground Truth verifier for {task_id}.

PROPOSED — generated by the verifier authoring assistant, not yet trusted.
It becomes trusted only after mutation validation shows it distinguishes a
correct implementation from a broken one, and a human approves it.

Task:
{task_block}

Acceptance contract:
{contract_block}
"""

from __future__ import annotations

import pytest


'''


def _block(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + ln for ln in (text or "").splitlines() or [prefix])


def generate_test(task: TaskInput, contract: ct.Contract) -> tuple[str, str]:
    """(path, content) for a task-specific pytest file.

    The generated file is a SKELETON with failing placeholders, never a test
    that passes on arrival. A generated test that passes immediately proves
    nothing and would sail through validation; `pytest.fail` forces the author
    to supply the assertion the contract describes.
    """
    stem = Path(task.named_files[0]).stem if task.named_files else "task"
    path = f"tests/groundtruth/test_gt_{task.task_id.replace('-', '_')}_{stem}.py"

    lines: list[str] = []
    con = []
    for c in contract.required:
        con.append(f"  REQUIRED : {c.text}")
    for c in contract.invariants:
        con.append(f"  INVARIANT: {c.text}")
    for c in contract.optional:
        con.append(f"  optional : {c.text}   (NOT part of PASS)")

    lines.append(_TEST_HEADER.format(
        task_id=task.task_id,
        task_block=_block(task.task),
        contract_block="\n".join(con) or "    (none derived)"))

    for i, c in enumerate(contract.required, start=1):
        lines.append(f"def test_required_{i}_{_slug(c.text)}() -> None:\n"
                     f'    """{c.text}"""\n'
                     f"    pytest.fail(\n"
                     f'        "UNIMPLEMENTED: assert that {c.text!r}. "\n'
                     f'        "A generated test must not pass until it checks something."\n'
                     f"    )\n\n")
    for i, c in enumerate(contract.invariants, start=1):
        lines.append(f"def test_invariant_{i}_{_slug(c.text)}() -> None:\n"
                     f'    """{c.text}"""\n'
                     f"    pytest.fail(\n"
                     f'        "UNIMPLEMENTED: assert the invariant {c.text!r}."\n'
                     f"    )\n\n")
    return path, "".join(lines)


def _slug(text: str, n: int = 6) -> str:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return "_".join(words[:n]) or "condition"


def generate_snippet(task: TaskInput, contract: ct.Contract,
                     strategy: str) -> str | None:
    """A `run_verifier`-compatible snippet for answer-shaped strategies.

    Uses the helpers that already exist in `verifiers.py`, so a snippet
    authored here runs in the same sandbox as every other verifier in the repo.
    """
    if strategy == S_SCHEMA:
        keys = _schema_keys(task.task)
        if keys:
            req = ", ".join(f'"{k}"' for k in keys)
            return f"json_schema(required=[{req}])"
        return "json_schema()"
    if strategy == S_FILE_STATE:
        if task.named_files:
            return (f'assert read("{task.named_files[0]}").strip(), '
                    f'"{task.named_files[0]} is missing or empty"\n'
                    "# TODO: assert the specific configured value the task names.")
    return None


_KEY_RE = re.compile(r"['\"`]([A-Za-z_][A-Za-z0-9_]{1,30})['\"`]")


def _schema_keys(task: str) -> list[str]:
    return sorted({m.group(1) for m in _KEY_RE.finditer(task or "")})[:8]


# ── Part 7: gaming guards ────────────────────────────────────────────────────

def gaming_guards(task: TaskInput, existing: list[str]) -> list[str]:
    """Checks that make the obvious shortcuts fail.

    Paired with the positive check, never instead of it: a guard alone proves
    only that nothing was vandalised.
    """
    guards: list[str] = []
    for t in existing[:3]:
        guards.append(f'no_tests_weakened("{t}", min_assertions=1)')
    if existing:
        guards.append("run the existing suite from a trusted checkout, not from "
                      "files the evaluated model produced")
    for f in task.named_files[:2]:
        guards.append(f"assert the change lands in {f}, not in the test")
    guards.append("verifier assets live outside the tree the model can edit")
    return guards


# ── The proposal ─────────────────────────────────────────────────────────────

def propose(candidate, *, repo_root: Path | None = None) -> Proposal:
    """Build a complete, reviewable proposal for one candidate."""
    root = repo_root or Path(__file__).resolve().parents[2]
    task = intake(candidate)
    existing, rationale = find_existing_tests(task, root)
    task.existing_tests = existing

    contract = ct.derive(task.task_id, task.task, existing_tests=existing,
                         test_command=task.test_command)
    strategy, blockers = select_strategy(task, contract, existing)

    p = Proposal(
        task_id=task.task_id,
        verification_strategy=strategy,
        verification_class=STRATEGY_CLASS[strategy],
        acceptance_contract=contract.to_json(),
        existing_evidence=existing,
        existing_rationale=rationale,
        # Existing tests are never assumed sufficient: nothing has shown they
        # assert the new behaviour, and assuming it is how a verifier that
        # passes everything gets adopted.
        existing_sufficient=False,
        new_verifier_required=strategy in MECHANICAL_STRATEGIES,
        blockers=blockers,
        gaming_guards=gaming_guards(task, existing),
        human_review_required=True,
    )

    if strategy in (S_TASK_TEST, S_INTEGRATION):
        path, content = generate_test(task, contract)
        p.proposed_files = [{"path": path, "content": content}]
        p.commands = [f"pytest {path} -q"]
        if task.test_command:
            p.commands.append(task.test_command)
    else:
        p.verifier_snippet = generate_snippet(task, contract, strategy)
        if task.test_command:
            p.commands = [task.test_command]

    if not contract.required:
        p.risks.append("no required condition — the verifier would assert nothing")
    if contract.optional:
        p.risks.append(f"{len(contract.optional)} optional condition(s) present; "
                       "they are excluded from PASS by construction")
    if strategy == S_JUDGE:
        p.risks.append("LLM judge: the verdict measures the judge as much as the model")
    if not task.repo_reconstructable and task.named_files:
        p.risks.append("repo not reconstructable — the test could not be re-run later")

    p.provisional_confidence = "unproven"
    return p
