"""Prove a verifier can tell correct from broken — or refuse to trust it.

A verifier that passes both a correct implementation and a broken one is worse
than no verifier: it produces confident labels that mean nothing. So confidence
here is never asserted, it is earned by two demonstrations:

    baseline   the verifier PASSES a known-good implementation
    mutants    the verifier FAILS each deliberately broken one

Both are required. A verifier that fails everything trivially "detects" every
mutant while being useless, which is why the baseline check is not optional and
why `classify()` refuses HIGH without it.

The mutations are ordinary bug shapes — an off-by-one, an inverted condition, a
removed call. They are not adversarial; they are what a model actually gets
wrong. A verifier that misses them would miss the real failures too.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCHEMA_VERSION = 1

# ── Confidence, from evidence only ───────────────────────────────────────────
HIGH = "HIGH"          # executable, passes baseline, kills every mutant
MEDIUM = "MEDIUM"      # executable and discriminating, but incomplete
LOW = "LOW"            # rubric or judge
UNUSABLE = "UNUSABLE"  # cannot distinguish success from failure

CONFIDENCE_ORDER = (UNUSABLE, LOW, MEDIUM, HIGH)


@dataclass
class Mutation:
    name: str
    description: str
    pattern: re.Pattern[str]
    replacement: str

    def apply(self, source: str) -> str | None:
        out, n = self.pattern.subn(self.replacement, source, count=1)
        return out if n else None


# Each is a bug somebody has actually written.
MUTATIONS: tuple[Mutation, ...] = (
    Mutation("off-by-one-lt", "< becomes <=",
             re.compile(r"(?<![<>=!])<(?!=)"), "<="),
    Mutation("off-by-one-range", "range(n) becomes range(n-1)",
             re.compile(r"range\(([A-Za-z_][\w.]*)\)"), r"range(\1 - 1)"),
    Mutation("inverted-condition", "if X becomes if not X",
             re.compile(r"\bif (?!not\b)([A-Za-z_][\w.]*)\s*:"), r"if not \1:"),
    Mutation("negated-equality", "== becomes !=",
             re.compile(r"(?<![!<>=])==(?!=)"), "!="),
    Mutation("disabled-retry", "a retry/loop bound becomes 1",
             re.compile(r"\b(max_retries|retries|attempts|max_attempts)\s*=\s*\d+"),
             r"\1 = 1"),
    Mutation("skipped-call", "a call is removed",
             re.compile(r"^(\s*)([a-z_][\w.]*\([^)]*\))\s*$", re.M), r"\1pass  # \2"),
    Mutation("missing-field", "a dict key is dropped",
             re.compile(r"^(\s*)(['\"][\w-]+['\"]\s*:\s*[^,\n]+,)\s*$", re.M),
             r"\1# \2"),
    Mutation("wrong-type", "int() becomes str()",
             re.compile(r"\bint\("), "str("),
    Mutation("truthy-return", "a return becomes True",
             re.compile(r"^(\s*)return (?!True\b)[^\n]+$", re.M), r"\1return True"),
    Mutation("constant-bool", "a boolean literal is flipped",
             re.compile(r"\bTrue\b"), "False"),
)


@dataclass
class MutantResult:
    name: str
    description: str
    applied: bool
    detected: bool          # the verifier FAILED the mutant, as it should
    note: str = ""

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("description", None)
        d["description"] = self.description
        return d


@dataclass
class Validation:
    task_id: str
    schema_version: int = SCHEMA_VERSION
    executable: bool = False
    baseline_passed: bool | None = None     # None = never run
    mutants: list[MutantResult] = field(default_factory=list)
    confidence: str = UNUSABLE
    rationale: str = ""
    ran_at: float = 0.0
    duration_s: float = 0.0
    # Why the probe set is (or is not) strong enough to support a confidence
    # claim. None for the pytest path, whose mutants come from a fixed library
    # and therefore always meet the floor; set by the snippet path, whose probes
    # are supplied by the operator. See `_snippet_discrimination_floor`.
    weak_probe_set: str | None = None

    @property
    def applied(self) -> list[MutantResult]:
        return [m for m in self.mutants if m.applied]

    @property
    def detected(self) -> int:
        return sum(1 for m in self.applied if m.detected)

    @property
    def total(self) -> int:
        return len(self.applied)

    @property
    def discriminates(self) -> bool:
        """Passes good code AND fails at least one broken version."""
        return bool(self.baseline_passed) and self.detected > 0

    def to_json(self) -> dict:
        return {
            "task_id": self.task_id,
            "schema_version": self.schema_version,
            "executable": self.executable,
            "baseline_passed": self.baseline_passed,
            "mutants_applied": self.total,
            "mutants_detected": self.detected,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "ran_at": self.ran_at,
            "duration_s": round(self.duration_s, 2),
            "results": [m.to_json() for m in self.mutants],
        }


def classify(v: Validation, *, strategy_is_mechanical: bool,
             contract_complete: bool) -> tuple[str, str]:
    """Confidence from evidence. There is no argument for 'the LLM was sure'.

    HIGH is deliberately hard: executable, demonstrably passes a known-good
    implementation, and catches every mutation that could be applied. Anything
    less is MEDIUM at best, and a verifier that cannot pass good code or cannot
    fail bad code is UNUSABLE regardless of how it was produced.
    """
    if not strategy_is_mechanical:
        return LOW, "strategy is rubric- or judge-based; not mechanically checkable"
    if not v.executable:
        return UNUSABLE, "the verifier could not be executed"
    if v.baseline_passed is False:
        return UNUSABLE, ("the verifier rejects a known-good implementation; it is "
                          "measuring something other than the task")
    if v.baseline_passed is None:
        return UNUSABLE, "no baseline was run, so passing is unproven"
    if v.total == 0:
        return MEDIUM, ("no mutation could be applied to this target, so "
                        "discrimination is undemonstrated")
    if v.detected == 0:
        return UNUSABLE, (f"the verifier passed all {v.total} broken implementations; "
                          "it cannot detect failure")
    if v.detected < v.total:
        return MEDIUM, (f"detected {v.detected}/{v.total} mutants — real but "
                        "incomplete behavioural coverage")
    if v.weak_probe_set:
        # Detecting every probe proves nothing when there were too few, or when
        # they were the same probe repeated. `detected == total` is a ratio, and
        # a ratio over a tiny hand-picked denominator is not evidence.
        return MEDIUM, (f"detected {v.detected}/{v.total} bad answers, but the "
                        f"probe set is too thin to support a stronger claim: "
                        f"{v.weak_probe_set}")
    if not contract_complete:
        return MEDIUM, (f"detected {v.detected}/{v.total} mutants, but the "
                        "acceptance contract has unresolved conditions")
    return HIGH, (f"passes a known-good implementation and detects "
                  f"{v.detected}/{v.total} broken ones")


def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd),
                           timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as exc:  # noqa: BLE001
        return 125, f"{type(exc).__name__}: {exc}"


def validate_pytest_verifier(
    *,
    task_id: str,
    test_source: str,
    target_path: str,
    target_source: str,
    command: list[str] | None = None,
    timeout: int = 120,
    max_mutants: int = 6,
) -> Validation:
    """Run a generated pytest file against a good target and mutated copies.

    Everything happens in a throwaway tree, and the mutation is applied to the
    TARGET, never to the test — mutating the test would measure the mutation
    engine rather than the verifier.
    """
    started = time.monotonic()
    v = Validation(task_id=task_id, ran_at=time.time())
    cmd = command or [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        def _write(src: str) -> Path:
            tgt = root / target_path
            tgt.parent.mkdir(parents=True, exist_ok=True)
            tgt.write_text(src, encoding="utf-8")
            tf = root / "test_generated.py"
            tf.write_text(test_source, encoding="utf-8")
            return tf

        tf = _write(target_source)
        code, out = _run(cmd + [str(tf)], root, timeout)
        v.executable = code not in (125,) and "No module named pytest" not in out
        v.baseline_passed = (code == 0)
        if not v.executable:
            v.rationale = f"could not execute: {out[:200]}"
            v.duration_s = time.monotonic() - started
            return v
        if not v.baseline_passed:
            v.rationale = f"baseline failed: {out[:200]}"
            v.duration_s = time.monotonic() - started
            return v

        for mut in MUTATIONS:
            if len(v.applied) >= max_mutants:
                break
            mutated = mut.apply(target_source)
            if mutated is None or mutated == target_source:
                v.mutants.append(MutantResult(mut.name, mut.description,
                                              applied=False, detected=False,
                                              note="pattern not present in target"))
                continue
            tf = _write(mutated)
            mcode, mout = _run(cmd + [str(tf)], root, timeout)
            v.mutants.append(MutantResult(
                mut.name, mut.description, applied=True,
                detected=(mcode != 0),
                note="" if mcode != 0 else "VERIFIER PASSED A BROKEN IMPLEMENTATION"))

    v.duration_s = time.monotonic() - started
    return v


#: The pytest path applies a fixed, hand-authored mutation library, so the
#: operator cannot influence how hard the probes are. The snippet path takes its
#: `bad_answers` from the CLI, ad hoc -- so a rushed operator supplying ONE
#: trivially-wrong answer reached `detected == total` and, with a complete
#: contract, HIGH confidence for a check that barely discriminates.
#:
#: Three is the floor because one proves almost nothing and two can be the same
#: mistake twice. This is a floor on the EVIDENCE, not on the verifier: falling
#: below it caps confidence at MEDIUM rather than rejecting the verifier.
MIN_BAD_ANSWERS = 3


def _snippet_discrimination_floor(good_answer: str, bad_answers: list[str]) -> str | None:
    """Why this probe set cannot support a HIGH claim, or None if it can.

    Deliberately checks the SHAPE of the probes and never their content: judging
    whether a bad answer is "wrong enough" is the operator's job, and a rule that
    tried would be a heuristic pretending to be a measurement.
    """
    probes = [b for b in bad_answers if (b or "").strip()]
    if len(probes) < MIN_BAD_ANSWERS:
        return (f"only {len(probes)} non-empty bad answer(s); "
                f"{MIN_BAD_ANSWERS} are needed to demonstrate discrimination")
    if len({b.strip() for b in probes}) < MIN_BAD_ANSWERS:
        return (f"the bad answers are not distinct ({len({b.strip() for b in probes})} "
                f"unique of {len(probes)}); repeating a probe does not add evidence")
    if any(b.strip() == (good_answer or "").strip() for b in probes):
        return "a bad answer is identical to the good answer"
    return None


def validate_snippet_verifier(
    *, task_id: str, snippet: str, good_answer: str, bad_answers: list[str],
) -> Validation:
    """Validate an answer-shaped verifier against one good and several bad answers.

    The `bad_answers` are this strategy's mutants: a schema check must reject a
    missing key the way a test must fail an off-by-one.

    Unlike the pytest path, these probes are chosen by whoever runs the CLI, so
    the strength of the evidence varies with their care. `weak_probe_set` records
    when it is too thin to support a HIGH claim; `classify` caps confidence
    accordingly rather than silently trusting `detected == total`.
    """
    from groundtruth.verifiers import run_verifier

    started = time.monotonic()
    v = Validation(task_id=task_id, ran_at=time.time(), executable=True)
    v.weak_probe_set = _snippet_discrimination_floor(good_answer, bad_answers)
    ok, why = run_verifier(snippet, good_answer)
    v.baseline_passed = ok
    if not ok:
        v.rationale = f"baseline answer rejected: {why[:160]}"
        v.duration_s = time.monotonic() - started
        return v
    for i, bad in enumerate(bad_answers, start=1):
        rejected, _ = run_verifier(snippet, bad)
        v.mutants.append(MutantResult(
            f"bad-answer-{i}", bad[:60], applied=True, detected=(not rejected),
            note="" if not rejected else "VERIFIER ACCEPTED A BAD ANSWER"))
    v.duration_s = time.monotonic() - started
    return v
